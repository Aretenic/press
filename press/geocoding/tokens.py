"""One geocoding proxy token per seminary site (ADR 053 §3, §5, §10).

A token is minted when Press installs seminary on a site and pushed into its site config. The
`Geocoding Token` row keeps its SHA-256, which the proxy looks the caller up by; the token itself
is in the site's configuration, which Press holds as it holds every site secret, under a Password
key the dashboard masks and an internal one it does not list.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import TYPE_CHECKING

import frappe
from frappe.utils import now_datetime
from frappe.utils.password import get_decrypted_password

if TYPE_CHECKING:
	from press.press.doctype.site.site import Site

APP = "seminary"
TOKEN_KEY = "geocoding_token"
BASE_URL_KEY = "geocoding_base_url"


def hash_token(token: str) -> str:
	return hashlib.sha256(token.encode()).hexdigest()


def base_url() -> str:
	return (frappe.db.get_single_value("Press Settings", "geocoding_base_url") or "").rstrip("/")


def google_key() -> str | None:
	return get_decrypted_password(
		"Press Settings", "Press Settings", "geocoding_api_key", raise_exception=False
	)


def is_enabled() -> bool:
	return bool(base_url() and google_key())


def has_seminary(site: Site) -> bool:
	return any(row.app == APP for row in site.apps)


def site_name(site: Site) -> str:
	# In Site.before_insert the document has no name yet: Frappe names it after before_insert
	return site.name or site._get_site_name(site.subdomain)


def ensure_site_geocoding(site: Site) -> None:
	"""Give a seminary site a token unless it already has one. Idempotent."""
	if not is_enabled() or not has_seminary(site):
		return
	if not site.is_new() and frappe.db.exists("Geocoding Token", {"site": site.name, "status": "Active"}):
		return
	provision(site)


def provision(site: Site, reason: str = "Replaced") -> None:
	"""Mint a token, revoke any other, and write it into the site's config.

	On a site not yet inserted the config is set on the document and travels with the New Site job;
	on an existing one it is pushed with Update Site Configuration. Revoking first leaves a gap of
	one agent job in which lookups fail, which seminary retries.
	"""
	name = site_name(site)
	revoke(name, reason)
	token = secrets.token_urlsafe(32)
	row = frappe.get_doc({"doctype": "Geocoding Token", "site": name, "token_hash": hash_token(token)})
	# A new site's row is linked before the Site itself is inserted, in the same transaction
	row.flags.ignore_links = site.is_new()
	row.insert(ignore_permissions=True)

	ensure_internal_config_keys()
	config = {TOKEN_KEY: token, BASE_URL_KEY: base_url()}
	if site.is_new():
		site._update_configuration(config, save=False)
	else:
		site.update_site_config(config)


def revoke(site: str, reason: str) -> None:
	for token in frappe.get_all("Geocoding Token", {"site": site, "status": "Active"}, pluck="name"):
		frappe.db.set_value(
			"Geocoding Token",
			token,
			{"status": "Revoked", "revoked_on": now_datetime(), "revoke_reason": reason},
		)


def site_for_token(token: str) -> str | None:
	if not token:
		return None
	return frappe.db.get_value(
		"Geocoding Token", {"token_hash": hash_token(token), "status": "Active"}, "site"
	)


def ensure_internal_config_keys() -> None:
	"""Hide both keys from the dashboard's Config tab, so a school neither sees nor edits them."""
	for key, kind in ((TOKEN_KEY, "Password"), (BASE_URL_KEY, "String")):
		if frappe.db.exists("Site Config Key", key):
			if not frappe.db.get_value("Site Config Key", key, "internal"):
				frappe.db.set_value("Site Config Key", key, "internal", 1)
			continue
		frappe.get_doc(
			{
				"doctype": "Site Config Key",
				"key": key,
				"type": kind,
				"title": key.replace("_", " ").title(),
				"description": "Seminary geocoding proxy (ADR 053). Set by Press.",
				"internal": 1,
			}
		).insert(ignore_permissions=True)


def backfill() -> list[str]:
	"""Give every active seminary site without a token one (§10). Run once from the console."""
	done = []
	sites = frappe.get_all("Site App", {"app": APP, "parenttype": "Site"}, pluck="parent")
	for name in frappe.get_all("Site", {"name": ("in", sites), "status": "Active"}, pluck="name"):
		if frappe.db.exists("Geocoding Token", {"site": name, "status": "Active"}):
			continue
		provision(frappe.get_doc("Site", name), reason="Backfill")
		done.append(name)
	return done
