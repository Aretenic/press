"""Checking that an SES event really came from our SNS topic (Aretenic ADR 094 §7).

SNS signs each message with a certificate it serves from an `sns.<region>.amazonaws.com` HTTPS
URL. The certificate URL is checked before it is fetched, the signature against the fields SNS
documents for each message type, and the topic against the one in Messaging Settings.
"""

from __future__ import annotations

import base64
import re
from urllib.parse import urlparse

import frappe
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from press import messaging as m

CERT_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$")
FIELDS = {
	"Notification": ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"),
	"SubscriptionConfirmation": (
		"Message",
		"MessageId",
		"SubscribeURL",
		"Timestamp",
		"Token",
		"TopicArn",
		"Type",
	),
	"UnsubscribeConfirmation": (
		"Message",
		"MessageId",
		"SubscribeURL",
		"Timestamp",
		"Token",
		"TopicArn",
		"Type",
	),
}


def string_to_sign(message: dict) -> bytes | None:
	fields = FIELDS.get(message.get("Type") or "")
	if not fields:
		return None
	out = ""
	for field in fields:
		if field == "Subject" and message.get(field) is None:
			continue
		if message.get(field) is None:
			return None
		out += f"{field}\n{message[field]}\n"
	return out.encode()


def cert_url_ok(url: str | None) -> bool:
	parsed = urlparse(url or "")
	return (
		parsed.scheme == "https"
		and bool(CERT_HOST.match(parsed.hostname or ""))
		and parsed.path.endswith(".pem")
	)


def certificate(url: str):
	key = f"press:messaging:sns-cert:{url}"
	pem = frappe.cache.get_value(key)
	if not pem:
		pem = requests.get(url, timeout=10).content
		frappe.cache.set_value(key, pem, expires_in_sec=86400)
	return x509.load_pem_x509_certificate(pem)


def verify(message: dict) -> bool:
	if message.get("TopicArn") != m.settings().ses_sns_topic_arn:
		return False
	if not cert_url_ok(message.get("SigningCertURL")):
		return False
	data = string_to_sign(message)
	if data is None:
		return False
	algorithm = hashes.SHA256() if str(message.get("SignatureVersion")) == "2" else hashes.SHA1()
	try:
		certificate(message["SigningCertURL"]).public_key().verify(
			base64.b64decode(message.get("Signature") or ""), data, padding.PKCS1v15(), algorithm
		)
	except Exception:
		return False
	return True
