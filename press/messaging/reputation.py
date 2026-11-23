"""Pausing one school's email before SES pauses everyone's (Aretenic ADR 094 §7).

SES reviews an account at a 5% bounce rate and may pause it at 10%; complaints at 0.1% and 0.5%.
Every hour the school's rates over its recent sends are compared with the far lower limits in
Messaging Settings, and a school over them has both configuration sets stopped. A new school also
starts with a daily email limit that doubles while its rates stay healthy. A school paused for its
rates stays paused until we lift it; a daily-limit pause lifts itself the next day.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_days, date_diff, nowdate

from press import messaging as m
from press.messaging import forward, ses, usage

DAILY_LIMIT = "Daily limit"
WINDOW_DAYS = 14
MAX_DAILY_LIMIT = 100_000


def rates(site: str) -> tuple[int, float, float]:
	"""Sends and the bounce and complaint rates (percent) over the last two weeks."""
	used = usage.totals(site, add_days(nowdate(), -WINDOW_DAYS), nowdate())
	sends = used["emails"]
	if not sends:
		return 0, 0.0, 0.0
	return sends, 100.0 * used["bounces"] / sends, 100.0 * used["complaints"] / sends


def verdict(sends: int, bounce_rate: float, complaint_rate: float, s) -> str | None:
	if sends < (s.rate_window_sends or 500):
		return None
	if bounce_rate >= (s.pause_bounce_rate or 3):
		return f"{bounce_rate:.1f}% of the school's recent emails bounced."
	if complaint_rate >= (s.pause_complaint_rate or 0.08):
		return f"{complaint_rate:.2f}% of the school's recent emails were marked as spam."
	return None


def check_all() -> None:
	"""Hourly."""
	s = m.settings()
	for account in frappe.get_all(
		m.ACCOUNT,
		filters={"status": "Active", "ses_tenant": ("is", "set")},
		fields=["name", "site", "email_paused", "email_paused_reason", "email_daily_limit"],
	):
		if account.email_paused and not (account.email_paused_reason or "").startswith(DAILY_LIMIT):
			continue
		reason = verdict(*rates(account.site), s)
		if reason:
			pause(account.name, reason)
		elif (
			not account.email_paused
			and account.email_daily_limit
			and usage.emails_today(account.site) >= account.email_daily_limit
		):
			pause(account.name, f"{DAILY_LIMIT} of {account.email_daily_limit} emails reached for today.")


def pause(account: str, reason: str) -> None:
	doc = frappe.get_doc(m.ACCOUNT, account)
	ses.set_sending(doc, False)
	doc.update({"email_paused": 1, "email_paused_reason": reason})
	doc.save(ignore_permissions=True)
	forward.site_event(doc.site, {"kind": "email_pause", "paused": True, "reason": reason})
	if not reason.startswith(DAILY_LIMIT):
		m.alert(f"Email paused for {doc.site}", reason)


@frappe.whitelist()
def resume(account: str) -> None:
	frappe.only_for("System Manager")
	_resume(account)


def _resume(account: str) -> None:
	doc = frappe.get_doc(m.ACCOUNT, account)
	ses.set_sending(doc, True)
	doc.update({"email_paused": 0, "email_paused_reason": None})
	doc.save(ignore_permissions=True)
	forward.site_event(doc.site, {"kind": "email_pause", "paused": False})


def daily() -> None:
	"""Lift yesterday's daily-limit pauses and let healthy schools send more."""
	s = m.settings()
	growth = s.daily_limit_growth or 7
	for account in frappe.get_all(
		m.ACCOUNT,
		filters={"status": "Active", "ses_tenant": ("is", "set")},
		fields=[
			"name",
			"site",
			"email_paused",
			"email_paused_reason",
			"email_daily_limit",
			"token_issued_on",
		],
	):
		if account.email_paused and (account.email_paused_reason or "").startswith(DAILY_LIMIT):
			_resume(account.name)
		if not account.email_daily_limit or not account.token_issued_on:
			continue
		age = date_diff(nowdate(), account.token_issued_on)
		if age and age % growth == 0 and not verdict(*rates(account.site), s):
			# Past MAX_DAILY_LIMIT the school has proven itself: no daily limit at all.
			grown = account.email_daily_limit * 2
			frappe.db.set_value(
				m.ACCOUNT, account.name, "email_daily_limit", 0 if grown > MAX_DAILY_LIMIT else grown
			)
