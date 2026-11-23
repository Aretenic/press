"""The Twilio calls hosted messaging makes, as plain REST (Aretenic ADR 094 §3, §9).

Every school call runs as that school's subaccount, so a refusal, a suspension or a spam report
stays with it. Only creating the subaccount uses the parent account.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

import frappe
import requests

from press import messaging as m

API = "https://api.twilio.com/2010-04-01"
MESSAGING_API = "https://messaging.twilio.com/v1"
PRICING_API = "https://pricing.twilio.com/v1"
TIMEOUT = 20

# Twilio error codes that sending again will not fix, as the site's codes (094 §6).
SEND_ERRORS = {
	21610: "opted_out",  # the number texted STOP to the carrier
	21614: "landline",  # not a mobile number
	21211: "blocked_number",  # not a valid number
	21612: "blocked_number",  # cannot be reached from this number
	21408: "country_disabled",  # geo permission off for that country
	30007: "blocked_number",
}


class TwilioError(Exception):
	def __init__(self, message: str, code: int | None = None, status: int | None = None):
		super().__init__(message)
		self.code, self.status = code, status


def parent_auth() -> tuple[str, str]:
	sid = m.settings().twilio_account_sid
	token = m.secret("twilio_auth_token")
	if not (sid and token):
		raise TwilioError("Twilio is not configured in Messaging Settings")
	return sid, token


def account_auth(account) -> tuple[str, str]:
	sid = account.twilio_subaccount_sid
	token = m.secret("twilio_subaccount_token", m.ACCOUNT, account.name)
	if not (sid and token):
		raise TwilioError(f"{account.name} has no Twilio subaccount")
	return sid, token


def _request(method: str, url: str, auth: tuple[str, str], data: dict | None = None) -> dict:
	try:
		response = requests.request(method, url, auth=auth, data=data, timeout=TIMEOUT)
	except requests.RequestException as e:
		raise TwilioError(type(e).__name__) from e
	try:
		body = response.json()
	except ValueError:
		body = {}
	if response.status_code >= 400:
		raise TwilioError(
			body.get("message") or f"HTTP {response.status_code}", body.get("code"), response.status_code
		)
	return body


# ---------------------------------------------------------------------------------- set-up


def create_subaccount(friendly_name: str) -> tuple[str, str]:
	body = _request("POST", f"{API}/Accounts.json", parent_auth(), {"FriendlyName": friendly_name[:64]})
	return body["sid"], body["auth_token"]


def create_messaging_service(account, status_url: str, inbound_url: str) -> str:
	body = _request(
		"POST",
		f"{MESSAGING_API}/Services",
		account_auth(account),
		{
			"FriendlyName": account.site[:64],
			"StatusCallback": status_url,
			"InboundRequestUrl": inbound_url,
			"InboundMethod": "POST",
			"UseInboundWebhookOnNumber": "false",
			"StickySender": "true",
			"SmartEncoding": "true",
		},
	)
	return body["sid"]


def buy_toll_free(account) -> tuple[str, str]:
	"""The first toll-free number Twilio offers, bought into the school's subaccount."""
	sid, _ = auth = account_auth(account)
	available = _request(
		"GET", f"{API}/Accounts/{sid}/AvailablePhoneNumbers/US/TollFree.json?SmsEnabled=true&PageSize=1", auth
	)
	numbers = available.get("available_phone_numbers") or []
	if not numbers:
		raise TwilioError("No toll-free number is available")
	bought = _request(
		"POST",
		f"{API}/Accounts/{sid}/IncomingPhoneNumbers.json",
		auth,
		{"PhoneNumber": numbers[0]["phone_number"]},
	)
	_request(
		"POST",
		f"{MESSAGING_API}/Services/{account.messaging_service_sid}/PhoneNumbers",
		auth,
		{"PhoneNumberSid": bought["sid"]},
	)
	return bought["phone_number"], bought["sid"]


def submit_toll_free_verification(account, number, brand: dict, opt_in_url: str) -> str:
	samples = [s for s in (number.sample_messages or "").splitlines() if s.strip()]
	contact = (brand.get("contact_name") or "").split(" ", 1)
	body = _request(
		"POST",
		f"{MESSAGING_API}/Tollfree/Verifications",
		account_auth(account),
		{
			"TollfreePhoneNumberSid": number.phone_number_sid,
			"BusinessName": brand.get("legal_name"),
			"BusinessWebsite": brand.get("website"),
			"BusinessRegistrationNumber": brand.get("registration_number"),
			"BusinessStreetAddress": brand.get("address"),
			"BusinessContactFirstName": contact[0],
			"BusinessContactLastName": contact[1] if len(contact) > 1 else contact[0],
			"BusinessContactEmail": brand.get("contact_email"),
			"BusinessContactPhone": brand.get("contact_phone"),
			"NotificationEmail": brand.get("contact_email"),
			"UseCaseCategories": "EDUCATION" if number.purpose == "General" else "MARKETING",
			"UseCaseSummary": number.use_case,
			"ProductionMessageSample": samples[0] if samples else number.use_case,
			"OptInType": "WEB_FORM",
			"OptInImageUrls": opt_in_url,
			"MessageVolume": str(number.monthly_volume or 1000),
			"AdditionalInformation": "Opt-in is confirmed with a code texted to the number. Terms: "
			+ opt_in_url.replace("/texting", "/texting-terms"),
		},
	)
	return body["sid"]


TOLL_FREE_STATUS = {
	"PENDING_REVIEW": "submitted",
	"IN_REVIEW": "in_review",
	"TWILIO_APPROVED": "approved",
	"TWILIO_REJECTED": "rejected",
}


def toll_free_status(account, verification_sid: str) -> tuple[str, str | None]:
	body = _request(
		"GET", f"{MESSAGING_API}/Tollfree/Verifications/{verification_sid}", account_auth(account)
	)
	return TOLL_FREE_STATUS.get(body.get("status") or "", "in_review"), body.get("rejection_reason")


# ---------------------------------------------------------------------------------- sending


def send(account, *, to: str, body: str, from_number: str | None, whatsapp: bool, status_url: str) -> dict:
	sid, _ = auth = account_auth(account)
	data = {"Body": body, "StatusCallback": status_url}
	if whatsapp:
		data["To"] = f"whatsapp:{to}"
		data["From"] = f"whatsapp:{account.whatsapp_number}"
	else:
		data["To"] = to
		if from_number:
			data["From"] = from_number
		else:
			data["MessagingServiceSid"] = account.messaging_service_sid
	return _request("POST", f"{API}/Accounts/{sid}/Messages.json", auth, data)


def country_price(country: str) -> float:
	"""Twilio's highest outbound price per text to a country, cached for a day (094 §9). The
	highest carrier's, since the carrier is only known after sending."""
	key = f"press:messaging:price:{country}"
	cached = frappe.cache.get_value(key)
	if cached is not None:
		return float(cached)
	body = _request("GET", f"{PRICING_API}/Messaging/Countries/{country}", parent_auth())
	prices = [
		float(p.get("current_price") or 0)
		for carrier in body.get("outbound_sms_prices") or []
		for p in carrier.get("prices") or []
	]
	price = max(prices) if prices else 0.0
	frappe.cache.set_value(key, price, expires_in_sec=86400)
	return price


def valid_signature(url: str, params: dict, signature: str | None, token: str | None) -> bool:
	"""Twilio's X-Twilio-Signature: HMAC-SHA1 of the URL followed by the sorted form fields."""
	if not (signature and token):
		return False
	payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
	expected = base64.b64encode(hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()).decode()
	return hmac.compare_digest(expected, signature)
