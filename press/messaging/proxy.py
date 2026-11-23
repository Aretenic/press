"""The messaging proxy sites call, and the callbacks Twilio and SES make (Aretenic ADR 094 §2).

Served as a page renderer under `/messaging/`, like the geocoding proxy (ADR 053): the hostname
`messaging.aretenic.com` points at Press. Site calls carry the site's bearer token and must come
from the site's own server. Twilio's callbacks are checked with the subaccount's signature, SES's
events with SNS's. No message text, number or address is stored or logged: only counts.

    POST /messaging/v1/texts             send one text
    POST /messaging/v1/registrations     register a number
    GET  /messaging/v1/registrations     where each registration stands
    GET  /messaging/v1/usage             this month against the allowance
    POST /messaging/v1/settings          the school's choices
    POST /messaging/twilio/status/<account>    delivery receipts
    POST /messaging/twilio/inbound/<account>   texts the school's numbers receive
    POST /messaging/ses/events                 SES events through SNS
"""

from __future__ import annotations

import contextlib
import json
import re

import frappe
import phonenumbers
from phonenumbers import shortnumberinfo
from werkzeug.wrappers import Response

from press import messaging as m
from press.geocoding.proxy import ip_matches
from press.messaging import forward, registration, segments, sns, twilio, usage
from press.messaging.provision import site_for_token

PREFIX = "messaging/"
MAX_BODY = 1600
MAX_REQUEST = 16384
HELP_WORDS = {"HELP", "INFO"}
FORWARDED_STATUSES = {"sent", "delivered", "undelivered", "failed", "read"}
ACCOUNT_NAME = re.compile(r"^[A-Za-z0-9._-]{1,140}$")


class Refused(Exception):
	def __init__(self, status: int, code: str, message: str = "", final: bool = True):
		super().__init__(message or code)
		self.status, self.code, self.message, self.final = status, code, message or code, final


def is_proxy_path(path: str) -> bool:
	return path.strip("/").startswith(PREFIX)


def before_request():
	"""As geocoding's: the bearer token is not a Frappe login, and Twilio and SNS never log in."""
	if frappe.request and is_proxy_path(frappe.request.path):
		frappe.local.messaging_authorization = frappe.request.environ.pop("HTTP_AUTHORIZATION", "")


def _json(body, status=200) -> Response:
	return Response(
		json.dumps(body),
		status=status,
		content_type="application/json",
		headers={"Cache-Control": "no-store"},
	)


def _refuse(refusal: Refused) -> Response:
	return _json({"error": refusal.code, "final": refusal.final, "message": refusal.message}, refusal.status)


class MessagingProxyRenderer:
	def __init__(self, path, status_code=None):
		self.path = path.strip("/")

	def can_render(self):
		return is_proxy_path(self.path)

	def render(self):
		if not m.enabled():
			return _refuse(Refused(503, "unavailable", "Hosted messaging is off", final=False))
		route = self.path.removeprefix(PREFIX)
		method = frappe.request.method
		try:
			if route.startswith("twilio/"):
				return twilio_callback(route)
			if route == "ses/events" and method == "POST":
				return ses_events()
			handler = SITE_CALLS.get((method, route))
			if not handler:
				raise Refused(404, "not_found")
			if (frappe.request.content_length or 0) > MAX_REQUEST:
				raise Refused(413, "too_large")
			return _json(handler(authenticate()))
		except Refused as refusal:
			return _refuse(refusal)


def request_json() -> dict:
	try:
		body = json.loads(frappe.request.get_data(cache=False)[:MAX_REQUEST] or b"{}")
	except ValueError:
		raise Refused(400, "malformed") from None
	if not isinstance(body, dict):
		raise Refused(400, "malformed")
	return body


def authenticate():
	header = getattr(frappe.local, "messaging_authorization", None) or ""
	scheme, _, token = header.partition(" ")
	site = site_for_token(token.strip()) if scheme.lower() == "bearer" else None
	if not site:
		raise Refused(401, "revoked", "Invalid token")
	server = frappe.db.get_value("Site", site, "server")
	ip, ipv6 = frappe.db.get_value("Server", server, ["ip", "ipv6"]) or (None, None)
	if not ip_matches(frappe.local.request_ip, ip, ipv6):
		raise Refused(403, "revoked", "This token is not valid from this address")
	return frappe.get_doc(m.ACCOUNT, {"site": site, "status": "Active"})


# ---------------------------------------------------------------------------------- texts


def checked_number(raw) -> tuple[str, str]:
	"""(E.164, region), refusing emergency and short numbers (094 §10)."""
	text = str(raw or "").strip()
	digits = re.sub(r"\D", "", text)
	if len(digits) <= 6:
		for region in m.ALLOWANCE_REGIONS:
			if shortnumberinfo.is_emergency_number(digits, region):
				raise Refused(422, "emergency_number", "Texts cannot reach emergency services")
		raise Refused(422, "blocked_number", "Short numbers are not texted")
	try:
		parsed = phonenumbers.parse(text, None)
	except phonenumbers.NumberParseException:
		raise Refused(422, "blocked_number", "Not a phone number") from None
	region = phonenumbers.region_code_for_number(parsed)
	national = str(parsed.national_number)
	if region and shortnumberinfo.is_emergency_number(national, region):
		raise Refused(422, "emergency_number", "Texts cannot reach emergency services")
	if not phonenumbers.is_valid_number(parsed):
		raise Refused(422, "blocked_number", "Not a valid number")
	return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164), region


def approved_number(account, purpose: str) -> str | None:
	return frappe.db.get_value(
		m.NUMBER, {"account": account.name, "purpose": purpose, "status": "approved"}, "number"
	)


def send_text(account, body: dict) -> dict:
	channel, text, to, region = checked_request(body)
	whatsapp = channel == "whatsapp"
	domestic = region in m.ALLOWANCE_REGIONS and not whatsapp
	texts = segments.count(text)
	check_sender(account, whatsapp, domestic, region)
	if (
		domestic
		and not body.get("verification")
		and account.overuse != "keep"
		and usage.texts_left(account.site) < texts
	):
		usage.count(account.site, "refused")
		raise Refused(422, "over_allowance", "This month's texts are used up")
	from_number = None
	if not whatsapp and body.get("purpose") == "Promotional":
		from_number = approved_number(account, "Promotional")
	try:
		sent = twilio.send(
			account,
			to=to,
			body=text,
			from_number=from_number,
			whatsapp=whatsapp,
			status_url=f"{m.base_url()}/twilio/status/{account.name}",
		)
	except twilio.TwilioError as e:
		usage.count(account.site, "refused")
		code = twilio.SEND_ERRORS.get(e.code or 0)
		if code:
			raise Refused(422, code, str(e)) from None
		raise Refused(502, "provider_error", "Twilio refused the text", final=False) from None
	texts = int(sent.get("num_segments") or texts)
	record(account, region, whatsapp, domestic, texts)
	return {"id": sent.get("sid"), "texts": texts}


def checked_request(body: dict) -> tuple[str, str, str, str]:
	channel = body.get("channel") if body.get("channel") in ("sms", "whatsapp") else None
	if not channel:
		raise Refused(400, "malformed", "channel must be sms or whatsapp")
	text = body.get("body")
	if not isinstance(text, str) or not 0 < len(text) <= MAX_BODY:
		raise Refused(400, "malformed", "body is required")
	to, region = checked_number(body.get("to"))
	return channel, text, to, region


def check_sender(account, whatsapp: bool, domestic: bool, region: str) -> None:
	if whatsapp:
		if not (account.whatsapp and account.whatsapp_ready and account.whatsapp_number):
			raise Refused(422, "whatsapp_disabled")
		return
	if not approved_number(account, "General"):
		raise Refused(422, "not_registered", "The school's number is not approved yet")
	if not domestic and region not in countries(account):
		raise Refused(422, "country_disabled", f"Texts to {region} are not turned on")


def countries(account) -> set[str]:
	return {c.strip().upper() for c in (account.international_countries or "").split(",") if c.strip()}


def record(account, region: str, whatsapp: bool, domestic: bool, texts: int) -> None:
	s = m.settings()
	margin = 1 + (s.margin_percent or 0) / 100
	if whatsapp:
		usage.count(account.site, "whatsapp_messages")
		usage.count(account.site, "whatsapp_cost", round((s.whatsapp_cost_per_message or 0) * margin, 4))
		return
	if not domestic:
		usage.count(account.site, "international_texts", texts)
		usage.count(
			account.site, "international_cost", round(texts * twilio.country_price(region) * margin, 4)
		)
		return
	before = usage.texts_left(account.site)
	usage.count(account.site, "texts", texts)
	if before > 0 >= before - texts:
		forward.site_event(account.site, {"kind": "texts_spent"})
		m.alert(f"{account.site} used its texts", "The month's included texts are used up.")


def save_settings(account, body: dict) -> dict:
	codes = [str(c).upper()[:2] for c in body.get("international_countries") or [] if str(c).isalpha()]
	account.update(
		{
			"overuse": "keep" if body.get("overuse") == "keep" else "email",
			"international_countries": ",".join(sorted(set(codes))),
			"whatsapp": 1 if body.get("whatsapp") else 0,
			"help_text": str(body.get("help_text") or "")[:300],
		}
	)
	account.save(ignore_permissions=True)
	return {"ok": True}


SITE_CALLS = {
	("POST", "v1/texts"): lambda account: send_text(account, request_json()),
	("POST", "v1/registrations"): lambda account: registration.submit(account, request_json()),
	("GET", "v1/registrations"): lambda account: {"registrations": registration.rows(account)},
	("GET", "v1/usage"): lambda account: usage.report(account.site),
	("POST", "v1/settings"): lambda account: save_settings(account, request_json()),
}


# ---------------------------------------------------------------------------------- Twilio


def twilio_callback(route: str) -> Response:
	kind, _, account_name = route.removeprefix("twilio/").partition("/")
	if (
		frappe.request.method != "POST"
		or kind not in ("status", "inbound")
		or not ACCOUNT_NAME.match(account_name)
	):
		raise Refused(404, "not_found")
	if not frappe.db.exists(m.ACCOUNT, {"name": account_name, "status": "Active"}):
		raise Refused(404, "not_found")
	account = frappe.get_doc(m.ACCOUNT, account_name)
	params = frappe.request.form.to_dict()
	url = f"{m.base_url()}/twilio/{kind}/{account_name}"
	token = m.secret("twilio_subaccount_token", m.ACCOUNT, account.name)
	if not twilio.valid_signature(url, params, frappe.request.headers.get("X-Twilio-Signature"), token):
		raise Refused(403, "bad_signature")
	if kind == "status":
		on_status(account, params)
		return Response("", status=204)
	on_inbound(account, params)
	return Response("<Response/>", content_type="text/xml")


def _channel(address: str) -> tuple[str, str]:
	if (address or "").startswith("whatsapp:"):
		return "whatsapp", address.removeprefix("whatsapp:")
	return "sms", address or ""


def on_status(account, params: dict) -> None:
	status = (params.get("MessageStatus") or "").lower()
	if status not in FORWARDED_STATUSES:
		return
	channel, to = _channel(params.get("To") or "")
	payload = {"kind": "status", "provider_message_id": params.get("MessageSid"), "status": status}
	if str(params.get("ErrorCode")) == "21610":
		payload.update({"error": "opted_out", "to": to})
	forward.message_event(account.site, channel, payload)


def on_inbound(account, params: dict) -> None:
	channel, sender = _channel(params.get("From") or "")
	body = (params.get("Body") or "").strip()
	if channel == "sms" and body.upper().rstrip(".!") in HELP_WORDS and account.help_text:
		try:
			twilio.send(
				account,
				to=sender,
				body=f"{account.help_text} Reply STOP to opt out.",
				from_number=params.get("To"),
				whatsapp=False,
				status_url=f"{m.base_url()}/twilio/status/{account.name}",
			)
			usage.count(account.site, "texts")
		except twilio.TwilioError:
			pass
	forward.message_event(
		account.site,
		channel,
		{
			"kind": "inbound",
			"from_address": sender,
			"body": body,
			"provider_message_id": params.get("MessageSid"),
		},
	)


# ---------------------------------------------------------------------------------- SES


def ses_events() -> Response:
	try:
		message = json.loads(frappe.request.get_data(cache=False) or b"{}")
	except ValueError:
		raise Refused(400, "malformed") from None
	if not isinstance(message, dict) or not sns.verify(message):
		raise Refused(403, "bad_signature")
	if message.get("Type") == "SubscriptionConfirmation":
		import requests

		requests.get(message["SubscribeURL"], timeout=10)
		return Response("", status=204)
	if message.get("Type") == "Notification":
		with contextlib.suppress(ValueError):
			on_ses_event(json.loads(message.get("Message") or "{}"))
	return Response("", status=204)


def on_ses_event(event: dict) -> None:
	tags = (event.get("mail") or {}).get("tags") or {}
	config_set = (tags.get("ses:configuration-set") or [None])[0]
	if not config_set:
		return
	site = frappe.db.get_value(
		m.ACCOUNT,
		{"ses_configuration_set": config_set},
		"site",
	) or frappe.db.get_value(m.ACCOUNT, {"ses_promotional_configuration_set": config_set}, "site")
	if not site:
		return
	kind = event.get("eventType") or event.get("notificationType")
	if kind == "Send":
		usage.count(site, "emails", len((event.get("mail") or {}).get("destination") or []) or 1)
	elif kind == "Bounce":
		bounce = event.get("bounce") or {}
		permanent = bounce.get("bounceType") == "Permanent"
		recipients = bounce.get("bouncedRecipients") or []
		usage.count(site, "bounces", len(recipients) or 1)
		for r in recipients:
			forward.site_event(
				site,
				{"kind": "email", "event": "bounce", "email": r.get("emailAddress"), "permanent": permanent},
			)
	elif kind == "Complaint":
		recipients = (event.get("complaint") or {}).get("complainedRecipients") or []
		usage.count(site, "complaints", len(recipients) or 1)
		for r in recipients:
			forward.site_event(site, {"kind": "email", "event": "complaint", "email": r.get("emailAddress")})
