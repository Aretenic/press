"""Telling the site what happened (Aretenic ADR 094 §2).

Delivery receipts and inbound texts go to seminary's comms webhook for the site's hosted account;
everything else to aretenic's event endpoint. Each body is signed with HMAC-SHA256 keyed with the
site token's SHA-256, the one form of the token Press keeps; the site computes the same key from
the token in its config.
"""

from __future__ import annotations

import hmac
import json
import time
from urllib.parse import quote

import frappe
import requests

from press import messaging as m

SIGNATURE_HEADER = "X-Aretenic-Signature"
COMMS_WEBHOOK = "/api/method/seminary.seminary.comms.webhook"
EVENT_WEBHOOK = "/api/method/aretenic.messaging.webhook.event"
# The site's Channel Provider Accounts for the hosted provider (aretenic.messaging).
SITE_ACCOUNTS = {"sms": "Hosted Texts", "whatsapp": "Hosted WhatsApp"}
ATTEMPTS = 3
TIMEOUT = 15


def site_url(site: str) -> str:
	host = frappe.db.get_value("Site", site, "host_name") or site
	return f"https://{host}"


def signature(token_hash: str, body: bytes) -> str:
	return hmac.new(token_hash.encode(), body, "sha256").hexdigest()


def message_event(site: str, channel: str, payload: dict) -> None:
	path = f"{COMMS_WEBHOOK}?account={quote(SITE_ACCOUNTS[channel])}"
	_enqueue(site, path, payload)


def site_event(site: str, payload: dict) -> None:
	_enqueue(site, EVENT_WEBHOOK, payload)


def _enqueue(site: str, path: str, payload: dict) -> None:
	frappe.enqueue(
		"press.messaging.forward.deliver",
		queue="short",
		site=site,
		path=path,
		payload=payload,
		enqueue_after_commit=True,
	)


def deliver(site: str, path: str, payload: dict) -> bool:
	token_hash = frappe.db.get_value(m.ACCOUNT, {"site": site, "status": "Active"}, "token_hash")
	if not token_hash:
		return False
	body = json.dumps(payload, separators=(",", ":")).encode()
	headers = {"Content-Type": "application/json", SIGNATURE_HEADER: signature(token_hash, body)}
	for attempt in range(ATTEMPTS):
		try:
			response = requests.post(site_url(site) + path, data=body, headers=headers, timeout=TIMEOUT)
			if response.status_code < 500:
				return response.ok
		except requests.RequestException:
			pass
		time.sleep(2**attempt)
	# The kind only: an inbound body or a number never goes into a log.
	m.alert(f"Could not reach {site}", f"{payload.get('kind')} event not delivered after {ATTEMPTS} attempts")
	return False
