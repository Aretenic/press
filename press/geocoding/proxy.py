"""The geocoding proxy seminary sites call instead of Google (ADR 053 §1, §2, §4, §6, §8).

Served as a page renderer, like `press.metrics`, so it answers on whatever hostname reaches the
Press site. It relays exactly three calls, rebuilt from checked fields, with Press's Google key and
field masks. It is never a general relay: anything else is refused before a token is looked at.

Its record is the `Geocoding Usage` counter. No address, place id or query text is stored or
logged, here or in an error.
"""

from __future__ import annotations

import ipaddress
import json
import re

import frappe
import requests
from frappe.utils import get_first_day, nowdate
from werkzeug.wrappers import Response

from press.geocoding.tokens import google_key, site_for_token

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
PLACES_URL = "https://places.googleapis.com/v1"
# The fields seminary reads. Set here, never taken from the caller: Google bills by field.
AUTOCOMPLETE_MASK = "suggestions.placePrediction.placeId,suggestions.placePrediction.text"
DETAILS_MASK = "addressComponents,location,formattedAddress"

MAX_BODY = 2048
MAX_ADDRESS = 500
TIMEOUT = 10
PLACE_ID = re.compile(r"^[A-Za-z0-9_-]{1,300}$")
SESSION_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

GEOCODE, AUTOCOMPLETE, DETAILS = "geocode", "autocomplete", "details"


class Refused(Exception):
	def __init__(self, status: int, reason: str, google_status: str):
		super().__init__(reason)
		self.status, self.reason, self.google_status = status, reason, google_status


def is_proxy_path(path: str) -> bool:
	path = path.strip("/")
	return path == "geocode" or path.startswith("v1/places")


def before_request():
	"""Take the site token out of the request before Frappe authenticates it.

	Frappe treats any two-part Authorization header as a login and ends the request with 401 when
	it names no user. The proxy's bearer token is not a login, so on the proxy's paths the header is
	moved aside for `authenticate` to read, and the request stays a guest's.
	"""
	if frappe.request and is_proxy_path(frappe.request.path):
		frappe.local.geocoding_authorization = frappe.request.environ.pop("HTTP_AUTHORIZATION", "")


def route(method: str, path: str) -> tuple[str, str | None] | None:
	"""(call, place id) for one of the three relayed calls, or None for anything else."""
	path = path.strip("/")
	if path == "geocode" and method == "GET":
		return GEOCODE, None
	if path == "v1/places:autocomplete" and method == "POST":
		return AUTOCOMPLETE, None
	if path.startswith("v1/places/") and method == "GET":
		place_id = path.removeprefix("v1/places/")
		if PLACE_ID.match(place_id):
			return DETAILS, place_id
	return None


def autocomplete_body(raw: bytes) -> dict:
	"""The caller's request cut down to the input and the session token, both checked."""
	if len(raw) > MAX_BODY:
		raise Refused(413, "Request too large", "INVALID_ARGUMENT")
	try:
		body = json.loads(raw or b"{}")
	except ValueError:
		raise Refused(400, "Malformed JSON", "INVALID_ARGUMENT") from None
	text = body.get("input") if isinstance(body, dict) else None
	if not isinstance(text, str) or not 0 < len(text) <= MAX_ADDRESS:
		raise Refused(400, "input is required", "INVALID_ARGUMENT")
	out = {"input": text}
	session = body.get("sessionToken")
	if session is not None:
		out["sessionToken"] = checked_session(session)
	return out


def checked_session(session) -> str:
	if not isinstance(session, str) or not SESSION_TOKEN.match(session):
		raise Refused(400, "Malformed sessionToken", "INVALID_ARGUMENT")
	return session


def ip_matches(ip: str | None, server_ip: str | None, server_ipv6: str | None) -> bool:
	"""Whether the caller is the site's own server: its IPv4, or an address in its IPv6 /64."""
	try:
		caller = ipaddress.ip_address(ip or "")
	except ValueError:
		return False
	if caller.version == 4:
		return bool(server_ip) and str(caller) == server_ip
	if not server_ipv6:
		return False
	try:
		network = ipaddress.ip_network(f"{server_ipv6.split('/')[0]}/64", strict=False)
	except ValueError:
		return False
	return caller in network


def over_quota(today: int, month: int, daily_limit: int, monthly_limit: int) -> bool:
	"""A daily limit of 0 allows nothing; a monthly limit of 0 adds no ceiling of its own."""
	return today >= daily_limit or (monthly_limit > 0 and month >= monthly_limit)


class GeocodingProxyRenderer:
	def __init__(self, path, status_code=None):
		self.path = path

	def can_render(self):
		return is_proxy_path(self.path)

	def render(self):
		call = route(frappe.request.method, self.path)
		if not call:
			return self.refuse(None, Refused(404, "Not found", "NOT_FOUND"))
		kind, place_id = call
		try:
			site = self.authenticate()
			request = self.build(kind, place_id)
			self.check_quota(site, kind)
		except Refused as refusal:
			return self.refuse(kind, refusal)
		return self.relay(kind, request)

	def authenticate(self) -> str:
		header = getattr(frappe.local, "geocoding_authorization", None) or ""
		scheme, _, token = header.partition(" ")
		site = site_for_token(token.strip()) if scheme.lower() == "bearer" else None
		if not site:
			raise Refused(401, "Invalid token", "REQUEST_DENIED")
		server = frappe.db.get_value("Site", site, "server")
		ip, ipv6 = frappe.db.get_value("Server", server, ["ip", "ipv6"]) or (None, None)
		if not ip_matches(frappe.local.request_ip, ip, ipv6):
			raise Refused(403, "This token is not valid from this address", "REQUEST_DENIED")
		return site

	def build(self, kind: str, place_id: str | None) -> dict:
		key = google_key()
		if not key:
			raise Refused(503, "Geocoding is not configured", "UNAVAILABLE")
		if kind == GEOCODE:
			address = (frappe.request.args.get("address") or "").strip()
			if not 0 < len(address) <= MAX_ADDRESS:
				raise Refused(400, "address is required", "INVALID_REQUEST")
			return {"method": "GET", "url": GEOCODE_URL, "params": {"address": address, "key": key}}
		headers = {"X-Goog-Api-Key": key}
		if kind == AUTOCOMPLETE:
			if (frappe.request.content_length or 0) > MAX_BODY:
				raise Refused(413, "Request too large", "INVALID_ARGUMENT")
			headers["X-Goog-FieldMask"] = AUTOCOMPLETE_MASK
			body = autocomplete_body(frappe.request.get_data(cache=False)[: MAX_BODY + 1])
			return {
				"method": "POST",
				"url": f"{PLACES_URL}/places:autocomplete",
				"json": body,
				"headers": headers,
			}
		headers["X-Goog-FieldMask"] = DETAILS_MASK
		params = {}
		if frappe.request.args.get("sessionToken"):
			params["sessionToken"] = checked_session(frappe.request.args["sessionToken"])
		return {
			"method": "GET",
			"url": f"{PLACES_URL}/places/{place_id}",
			"params": params,
			"headers": headers,
		}

	def check_quota(self, site: str, kind: str) -> None:
		plan = frappe.db.get_value("Site", site, "plan")
		daily, monthly = frappe.db.get_value(
			"Site Plan", plan, ["geocoding_daily_limit", "geocoding_monthly_limit"]
		) or (0, 0)
		today = nowdate()
		used_today = usage(site, today, today)
		used_month = usage(site, str(get_first_day(today)), today)
		if over_quota(used_today, used_month, daily or 0, monthly or 0):
			count(site, "refused")
			raise Refused(429, "This site's geocoding quota is used up", "OVER_DAILY_LIMIT")
		count(site, kind)

	def relay(self, kind: str, request: dict) -> Response:
		try:
			upstream = requests.request(timeout=TIMEOUT, **request)
		except requests.RequestException as exc:
			# The exception text carries the URL, and with it the address and the key
			frappe.log_error(title="Geocoding proxy: upstream unreachable", message=type(exc).__name__)
			return self.refuse(kind, Refused(502, "Upstream unreachable", "UNKNOWN_ERROR"))
		return Response(
			upstream.content,
			status=upstream.status_code,
			content_type="application/json",
			headers={"Cache-Control": "no-store"},
		)

	def refuse(self, kind: str | None, refusal: Refused) -> Response:
		if kind == GEOCODE:
			# Google's Geocoding API answers 200 with a status, and seminary reads that status
			status = 200 if refusal.google_status == "OVER_DAILY_LIMIT" else refusal.status
			body = {"status": refusal.google_status, "error_message": refusal.reason}
		else:
			status = refusal.status
			body = {
				"error": {"code": refusal.status, "status": refusal.google_status, "message": refusal.reason}
			}
		return Response(
			json.dumps(body),
			status=status,
			content_type="application/json",
			headers={"Cache-Control": "no-store"},
		)


def usage(site: str, start: str, end: str) -> int:
	return int(
		frappe.db.sql(
			"select coalesce(sum(calls), 0) from `tabGeocoding Usage` where site = %s and date between %s and %s",
			(site, start, end),
		)[0][0]
	)


def count(site: str, column: str) -> None:
	"""One more call on today's row, created on first use. Committed at once, so a failure later in
	the request still counts: the quota protects the bill, and Google bills the attempt."""
	assert column in ("geocode", "autocomplete", "details", "refused")
	today = nowdate()
	calls = 0 if column == "refused" else 1
	frappe.db.sql(
		f"""insert into `tabGeocoding Usage`
			(name, site, date, calls, `{column}`, creation, modified, owner, modified_by, docstatus, idx)
		values (%(name)s, %(site)s, %(date)s, %(calls)s, 1, now(6), now(6), 'Administrator', 'Administrator', 0, 0)
		on duplicate key update calls = calls + %(calls)s, `{column}` = `{column}` + 1, modified = now(6)""",
		{"name": f"{site}|{today}", "site": site, "date": today, "calls": calls},
	)
	frappe.db.commit()
