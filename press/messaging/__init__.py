"""Hosted messaging for Aretenic schools (Aretenic ADR 094).

One Twilio parent account and one AWS account serve every school, each school isolated inside
them: a Twilio subaccount with its own Messaging Service and numbers, and an SES tenant with its
own sending domain, configuration sets and suppression list. A site reaches all of it through the
proxy in `proxy`, with one bearer token per site, as geocoding does (ADR 053).

- `provision` gives a site its account: subaccount, SES tenant, token, site config.
- `proxy` answers the site's calls and Twilio's and SES's callbacks, and keeps no message text.
- `usage` counts per site and day, checks the allowance, and writes the monthly statement.
- `registration` buys numbers and registers them with the carriers.
- `reputation` pauses a school's email before SES would pause the whole account.
- `forward` posts what happened back to the site, signed with its token.
"""

from __future__ import annotations

import frappe
from frappe.utils.password import get_decrypted_password

SETTINGS = "Messaging Settings"
ACCOUNT = "Messaging Account"
NUMBER = "Messaging Number"
USAGE = "Messaging Usage"
ALLOWANCE = "Messaging Allowance"
STATEMENT = "Messaging Statement"
ALLOWANCE_REGIONS = ("US", "CA")
# What a plan without a Messaging Allowance row gets: Populi's figures (094 §8).
DEFAULT_ALLOWANCE = {"emails_included": 50000, "texts_included": 5000, "numbers_included": 1}


def settings():
	return frappe.get_cached_doc(SETTINGS)


def enabled() -> bool:
	return bool(settings().enabled)


def secret(fieldname: str, doctype: str = SETTINGS, name: str | None = None) -> str | None:
	return get_decrypted_password(doctype, name or doctype, fieldname, raise_exception=False)


def base_url() -> str:
	return (settings().base_url or "").rstrip("/")


def account_for_site(site: str) -> str | None:
	return frappe.db.get_value(ACCOUNT, {"site": site, "status": "Active"})


def allowance(site: str) -> dict:
	plan = frappe.db.get_value("Site", site, "plan")
	row = frappe.db.get_value(ALLOWANCE, plan, list(DEFAULT_ALLOWANCE), as_dict=True) if plan else None
	return dict(row) if row else dict(DEFAULT_ALLOWANCE)


def alert(subject: str, message: str) -> None:
	"""Tell us. Never the school: the site tells its own administrators."""
	to = settings().alerts_email
	if to and not frappe.flags.in_test:
		frappe.sendmail(recipients=[to], subject=f"[Messaging] {subject}", message=message, now=False)
	frappe.log_error(title=f"Messaging: {subject}", message=message)
