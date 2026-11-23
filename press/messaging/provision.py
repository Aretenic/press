"""Giving a site hosted messaging, and taking it away (Aretenic ADR 094 §2 to §3, §7).

Staff create a Messaging Account for the site and press Provision. Nothing is set up at site
creation: a subaccount and an SES tenant are real resources, and a school gets them when it buys
the feature. Tokens work as geocoding's do (ADR 053 §3 to §5), with a token of their own.
"""

from __future__ import annotations

import hashlib
import re
import secrets

import frappe
from frappe.utils import now_datetime

from press import messaging as m
from press.messaging import ses, twilio

TOKEN_KEY = "messaging_token"
BASE_URL_KEY = "messaging_base_url"
# Keys the school must neither see nor edit on the dashboard's Config tab.
INTERNAL_KEYS = {
	TOKEN_KEY: "Password",
	BASE_URL_KEY: "String",
	"mail_server": "String",
	"mail_port": "Number",
	"use_tls": "Number",
	"mail_login": "String",
	"mail_password": "Password",  # pragma: allowlist secret (a field type)
	"auto_email_id": "String",
	"always_use_account_email_id_as_sender": "Number",
	"email_sender_name": "String",
	"ses_tenant": "String",
	"ses_configuration_set": "String",
	"ses_promotional_configuration_set": "String",
}


def hash_token(token: str) -> str:
	return hashlib.sha256(token.encode()).hexdigest()


def slug_for(site: str) -> str:
	return re.sub(r"[^a-z0-9]+", "-", site.lower()).strip("-")[:40]


def site_for_token(token: str) -> str | None:
	if not token:
		return None
	return frappe.db.get_value(m.ACCOUNT, {"token_hash": hash_token(token), "status": "Active"}, "site")


@frappe.whitelist()
def provision(account: str) -> str:
	"""Subaccount, Messaging Service, SES tenant and token, then the site config."""
	frappe.only_for("System Manager")
	if not m.enabled():
		frappe.throw("Hosted messaging is off in Messaging Settings.")
	doc = frappe.get_doc(m.ACCOUNT, account)
	if doc.status == "Active":
		frappe.throw("This account is already active.")
	base = m.base_url()
	if not doc.twilio_subaccount_sid:
		sid, token = twilio.create_subaccount(doc.site)
		doc.twilio_subaccount_sid = sid
		doc.twilio_subaccount_token = token
		doc.save(ignore_permissions=True)
	if not doc.messaging_service_sid:
		doc.messaging_service_sid = twilio.create_messaging_service(
			doc, f"{base}/twilio/status/{doc.name}", f"{base}/twilio/inbound/{doc.name}"
		)
		doc.save(ignore_permissions=True)
	config = {}
	if not doc.ses_tenant:
		config.update(ses.provision(doc, slug_for(doc.site)))
	token = secrets.token_urlsafe(32)
	doc.update({"token_hash": hash_token(token), "token_issued_on": now_datetime(), "status": "Active"})
	doc.save(ignore_permissions=True)
	config.update({TOKEN_KEY: token, BASE_URL_KEY: base})
	push_config(doc.site, config)
	return doc.status


@frappe.whitelist()
def rotate_token(account: str) -> None:
	"""A new token, written to the site; the old one stops at once (053 §5)."""
	frappe.only_for("System Manager")
	doc = frappe.get_doc(m.ACCOUNT, account)
	token = secrets.token_urlsafe(32)
	doc.update({"token_hash": hash_token(token), "token_issued_on": now_datetime()})
	doc.save(ignore_permissions=True)
	push_config(doc.site, {TOKEN_KEY: token, BASE_URL_KEY: m.base_url()})


def push_config(site: str, config: dict) -> None:
	ensure_internal_config_keys()
	frappe.get_doc("Site", site).update_site_config(config)


def revoke(site: str, reason: str) -> None:
	"""Archiving a site ends its messaging: no token works, SES stops sending as it."""
	name = frappe.db.get_value(m.ACCOUNT, {"site": site, "status": "Active"})
	if not name:
		return
	doc = frappe.get_doc(m.ACCOUNT, name)
	doc.update({"status": "Revoked", "token_hash": None})
	doc.add_comment("Info", f"Revoked: {reason}")
	doc.save(ignore_permissions=True)
	try:
		ses.set_sending(doc, False)
		ses.revoke_login(doc)
	except Exception:
		m.alert(f"Could not close SES for {site}", frappe.get_traceback())


def on_site_update(doc, method=None) -> None:
	"""Site doc_event: archiving revokes."""
	if doc.status == "Archived" and doc.has_value_changed("status"):
		revoke(doc.name, "Site archived")


def ensure_internal_config_keys() -> None:
	for key, kind in INTERNAL_KEYS.items():
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
				"description": "Hosted messaging (Aretenic ADR 094). Set by Press.",
				"internal": 1,
			}
		).insert(ignore_permissions=True)
