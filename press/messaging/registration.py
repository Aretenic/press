"""Buying a school's numbers and registering them with the carriers (Aretenic ADR 094 §3 to §4).

A toll-free number is bought into the school's subaccount, added to its Messaging Service and
sent for toll-free verification, all through Twilio's API; an hourly check follows the review. A
10DLC brand and campaign go through Twilio's console for now: the number waits In Review and we
are told to register it.
"""

from __future__ import annotations

import json

import frappe
from frappe.utils import nowdate

from press import messaging as m
from press.messaging import forward, twilio

OPEN = ("submitted", "in_review")


def submit(account, payload: dict) -> dict:
	purpose = payload.get("purpose") if payload.get("purpose") in ("General", "Promotional") else "General"
	kind = payload.get("kind") if payload.get("kind") in ("Toll-free", "10DLC") else "Toll-free"
	samples = payload.get("sample_messages") or []
	number = frappe.get_doc(
		{
			"doctype": m.NUMBER,
			"account": account.name,
			"purpose": purpose,
			"kind": kind,
			"status": "submitted",
			"use_case": str(payload.get("use_case") or "")[:2000],
			"sample_messages": "\n".join(str(s)[:500] for s in samples[:5]),
			"monthly_volume": int(payload.get("monthly_volume") or 0),
			"brand": json.dumps(payload.get("brand") or {}),
		}
	)
	number.flags.quiet = True
	number.insert(ignore_permissions=True)
	if kind == "Toll-free":
		phone, phone_sid = twilio.buy_toll_free(account)
		number.update({"number": phone, "phone_number_sid": phone_sid})
		number.verification_sid = twilio.submit_toll_free_verification(
			account, number, payload.get("brand") or {}, payload.get("opt_in_url") or ""
		)
	else:
		number.status = "in_review"
		m.alert(
			f"Register a 10DLC number for {account.site}",
			f"Messaging Number {number.name}: register the brand and campaign in the school's Twilio "
			"subaccount, then set the number and mark it approved.",
		)
	number.flags.quiet = True
	number.save(ignore_permissions=True)
	return {"id": number.name, "status": number.status}


def rows(account) -> list[dict]:
	return [
		{"id": n.name, "status": n.status, "number": n.number, "reason": n.reason}
		for n in frappe.get_all(
			m.NUMBER, filters={"account": account.name}, fields=["name", "status", "number", "reason"]
		)
	]


def on_number_update(doc) -> None:
	"""Messaging Number on_update: tell the site when its registration moves."""
	if doc.flags.quiet or not doc.has_value_changed("status"):
		return
	if doc.status == "approved" and not doc.billed_from:
		doc.db_set("billed_from", nowdate())
	site = frappe.db.get_value(m.ACCOUNT, doc.account, "site")
	forward.site_event(
		site,
		{
			"kind": "registration",
			"id": doc.name,
			"status": doc.status,
			"number": doc.number,
			"reason": doc.reason,
		},
	)


def poll() -> None:
	"""Hourly: follow each toll-free verification still under review."""
	for row in frappe.get_all(
		m.NUMBER,
		filters={"kind": "Toll-free", "status": ("in", OPEN), "verification_sid": ("is", "set")},
		fields=["name", "account", "verification_sid"],
	):
		account = frappe.get_doc(m.ACCOUNT, row.account)
		try:
			status, reason = twilio.toll_free_status(account, row.verification_sid)
		except twilio.TwilioError:
			continue
		doc = frappe.get_doc(m.NUMBER, row.name)
		if status != doc.status:
			doc.update({"status": status, "reason": reason})
			doc.save(ignore_permissions=True)
