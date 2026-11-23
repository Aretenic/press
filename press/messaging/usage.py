"""Counting, the allowance and the monthly statement (Aretenic ADR 094 §8 to §9, 045b §5).

Counts go on one `Messaging Usage` row per site and day, created on first use and committed at
once, as geocoding's are: a failure later in the request still counts, because Twilio and SES bill
the attempt. The statement is written on the first of each month for the month before, for
aretenic.com to invoice in arrears; Press holds no prices for schools (045b §6).
"""

from __future__ import annotations

import frappe
from frappe.utils import add_months, get_first_day, get_last_day, getdate, nowdate

from press import messaging as m

COLUMNS = (
	"texts",
	"international_texts",
	"international_cost",
	"whatsapp_messages",
	"whatsapp_cost",
	"emails",
	"bounces",
	"complaints",
	"refused",
)


def count(site: str, column: str, amount: float = 1, day: str | None = None) -> None:
	assert column in COLUMNS
	day = day or nowdate()
	frappe.db.sql(
		f"""insert into `tabMessaging Usage`
			(name, site, date, `{column}`, creation, modified, owner, modified_by, docstatus, idx)
		values (%(name)s, %(site)s, %(date)s, %(amount)s, now(6), now(6), 'Administrator', 'Administrator', 0, 0)
		on duplicate key update `{column}` = `{column}` + %(amount)s, modified = now(6)""",
		{"name": f"{site}|{day}", "site": site, "date": day, "amount": amount},
	)
	if not frappe.flags.in_test:
		frappe.db.commit()  # nosemgrep: counted even if the request fails later (module docstring)


def totals(site: str, start, end) -> dict:
	row = frappe.db.sql(
		f"""select {", ".join(f"coalesce(sum(`{c}`), 0) as `{c}`" for c in COLUMNS)}
		from `tabMessaging Usage` where site = %s and date between %s and %s""",
		(site, start, end),
		as_dict=True,
	)[0]
	return {c: float(row[c]) if c.endswith("_cost") else int(row[c]) for c in COLUMNS}


def this_month(site: str) -> dict:
	today = nowdate()
	return totals(site, get_first_day(today), today)


def texts_left(site: str) -> int:
	return m.allowance(site)["texts_included"] - this_month(site)["texts"]


def report(site: str) -> dict:
	"""GET /usage."""
	used = this_month(site)
	included = m.allowance(site)
	return {
		"period": nowdate()[:7],
		"emails": used["emails"],
		"emails_included": included["emails_included"],
		"texts": used["texts"],
		"texts_included": included["texts_included"],
		"international": {
			"texts": used["international_texts"],
			"cost": used["international_cost"],
			"whatsapp_messages": used["whatsapp_messages"],
			"whatsapp_cost": used["whatsapp_cost"],
		},
		"numbers": frappe.db.count(m.NUMBER, {"account": m.account_for_site(site), "status": "approved"}),
	}


def emails_today(site: str) -> int:
	today = nowdate()
	return totals(site, today, today)["emails"]


# ---------------------------------------------------------------------------------- statement


def statement(site: str, period: str) -> str:
	"""Write (or rewrite, while Draft) the site's statement for `period` (YYYY-MM)."""
	start = getdate(f"{period}-01")
	end = get_last_day(start)
	used = totals(site, start, end)
	included = m.allowance(site)
	account = frappe.db.get_value(m.ACCOUNT, {"site": site})
	numbers = frappe.get_all(
		m.NUMBER,
		filters={"account": account, "status": "approved", "billed_from": ("<=", end)},
		fields=["registration_fees", "billed_from"],
	)
	fees = sum(
		n.registration_fees or 0 for n in numbers if n.billed_from and start <= getdate(n.billed_from) <= end
	)
	values = {
		"site": site,
		"period": period,
		"site_plan": frappe.db.get_value("Site", site, "plan"),
		"emails": used["emails"],
		"emails_included": included["emails_included"],
		"emails_over": max(used["emails"] - included["emails_included"], 0),
		"texts": used["texts"],
		"texts_included": included["texts_included"],
		"texts_over": max(used["texts"] - included["texts_included"], 0),
		"numbers": len(numbers),
		"numbers_included": included["numbers_included"],
		"extra_numbers": max(len(numbers) - included["numbers_included"], 0),
		"international_texts": used["international_texts"],
		"international_cost": used["international_cost"],
		"whatsapp_messages": used["whatsapp_messages"],
		"whatsapp_cost": used["whatsapp_cost"],
		"registration_fees": fees,
	}
	name = frappe.db.get_value(m.STATEMENT, {"site": site, "period": period})
	if name:
		doc = frappe.get_doc(m.STATEMENT, name)
		if doc.status == "Posted":
			return name
		doc.update(values)
		doc.save(ignore_permissions=True)
		return name
	return frappe.get_doc({"doctype": m.STATEMENT, **values}).insert(ignore_permissions=True).name


def monthly_statements() -> list[str]:
	"""Scheduled on the first of the month: last month, for every site that had an account."""
	period = str(add_months(get_first_day(nowdate()), -1))[:7]
	return [statement(site, period) for site in frappe.get_all(m.ACCOUNT, pluck="site")]
