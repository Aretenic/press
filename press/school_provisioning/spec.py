"""The onboarding spec agora sends (Aretenic ADR 044 §3): checking it, and what the steps derive from it.

Pure functions only, so they are tested without a database. The spec is a snapshot: Press never
edits it, and refuses a `spec_version` it does not know.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

SPEC_VERSIONS = (1,)

#: ADR 040: Press only checks the label's shape. Sites are created only through this pipeline, so
#: this is the one place that also refuses server-shaped and reserved names.
SUBDOMAIN_SHAPE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
SERVER_SHAPE = re.compile(r"^(n|f|m|c|p|e|r|u|t|nfs|fs|nat)[0-9]+-")
RESERVED_SUBDOMAINS = frozenset({"press", "www", "api", "admin", "mail", "status", "docs"})
FISCAL_YEAR_START = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")

#: Installed only after the Setup Wizard; its before_install refuses an incomplete wizard.
AFTER_WIZARD_APPS = ("oikonomos",)

REQUIRED = {
	"infra": ("subdomain", "cluster", "release_group", "apps", "plan"),
	"site": ("company", "abbreviation", "country", "language", "currency", "time_zone", "fiscal_year_start"),
	"administrator": ("first_name", "last_name", "email"),
}


def spec_errors(spec: dict) -> list[str]:  # noqa: C901
	"""Why Press cannot accept this spec; empty when it can (the record-level checks are separate)."""
	if not isinstance(spec, dict):
		return ["The spec is not an object."]
	if spec.get("spec_version") not in SPEC_VERSIONS:
		return [f"Spec version {spec.get('spec_version')!r} is not supported."]

	errors = [f"{key} is missing." for key in ("onboarding", "provisioning_token") if not spec.get(key)]
	infra, site = spec.get("infra") or {}, spec.get("site") or {}
	admin = site.get("administrator") or {}
	for section, values in (("infra", infra), ("site", site), ("administrator", admin)):
		errors += [f"{section}.{key} is missing." for key in REQUIRED[section] if not values.get(key)]

	errors += subdomain_errors(infra.get("subdomain") or "")
	apps = infra.get("apps") or []
	if apps and "frappe" not in apps:
		errors.append("infra.apps must include frappe.")
	if "erpnext" in apps and not site.get("chart_of_accounts"):
		errors.append("site.chart_of_accounts is required with erpnext.")
	if any(app in apps for app in AFTER_WIZARD_APPS) and "erpnext" not in apps:
		errors.append("oikonomos needs erpnext.")
	if site.get("fiscal_year_start") and not FISCAL_YEAR_START.match(site["fiscal_year_start"]):
		errors.append("site.fiscal_year_start must be MM-DD.")
	for dataset in spec.get("datasets") or []:
		if not (dataset.get("dataset_type") and dataset.get("url") and dataset.get("sha256")):
			errors.append("Each dataset needs dataset_type, url and sha256.")
			break
	return errors


def subdomain_errors(subdomain: str) -> list[str]:
	if not subdomain:
		return []
	if len(subdomain) > 63 or not SUBDOMAIN_SHAPE.match(subdomain):
		return [f"Subdomain {subdomain!r} may use only lowercase letters, digits and inner hyphens."]
	if subdomain in RESERVED_SUBDOMAINS:
		return [f"Subdomain {subdomain!r} is reserved."]
	if SERVER_SHAPE.match(subdomain):
		return [f"Subdomain {subdomain!r} looks like a server name."]
	return []


def new_site_apps(spec: dict) -> list[str]:
	"""The apps the site is created with: frappe first, and nothing that must wait for the wizard."""
	apps = [a for a in spec["infra"]["apps"] if a not in AFTER_WIZARD_APPS]
	return ["frappe", *[a for a in apps if a != "frappe"]]


def after_wizard_apps(spec: dict) -> list[str]:
	return [a for a in spec["infra"]["apps"] if a in AFTER_WIZARD_APPS]


def fiscal_year(start: str, today: date) -> tuple[date, date]:
	"""The fiscal year containing today, for a year starting on MM-DD."""
	month, day = (int(part) for part in start.split("-"))
	begins = date(today.year, month, day)
	if begins > today:
		begins = date(today.year - 1, month, day)
	ends = date(begins.year + 1, month, day) - timedelta(days=1)
	return begins, ends


def setup_wizard_data(spec: dict, language_name: str, today: date) -> dict:
	"""The Complete Setup Wizard job's arguments. No password: the administrator is invited (§5)."""
	site, admin = spec["site"], spec["site"]["administrator"]
	data = {
		"language": language_name,
		"lang": language_name,
		"country": site["country"],
		"timezone": site["time_zone"],
		"currency": site["currency"],
		"full_name": f"{admin['first_name']} {admin['last_name']}".strip(),
		"email": admin["email"],
		"setup_demo": 0,
		"enable_telemetry": 0,
		"allow_recording_first_session": 0,
	}
	if "erpnext" in spec["infra"]["apps"]:
		begins, ends = fiscal_year(site["fiscal_year_start"], today)
		data.update(
			{
				"company_name": site["company"],
				"company_abbr": site["abbreviation"],
				"chart_of_accounts": site["chart_of_accounts"],
				"fy_start_date": begins.isoformat(),
				"fy_end_date": ends.isoformat(),
			}
		)
	return data
