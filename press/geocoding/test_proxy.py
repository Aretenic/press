"""The proxy's refusals (ADR 053 §2, §4, §6). Pure checks, no database and no Google."""

import json
import unittest

import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from press.geocoding import proxy
from press.geocoding.tokens import hash_token


class TestRoute(unittest.TestCase):
	def test_the_three_calls_are_relayed(self):
		self.assertEqual(proxy.route("GET", "/geocode"), (proxy.GEOCODE, None))
		self.assertEqual(proxy.route("POST", "v1/places:autocomplete"), (proxy.AUTOCOMPLETE, None))
		self.assertEqual(proxy.route("GET", "v1/places/ChIJ_abc-123"), (proxy.DETAILS, "ChIJ_abc-123"))

	def test_anything_else_is_not(self):
		for method, path in (
			("POST", "geocode"),
			("GET", "v1/places:autocomplete"),
			("GET", "v1/places:searchText"),
			("GET", "v1/places/abc/photos"),
			("GET", "v1/places/../../maps/api/directions/json"),
			("GET", "v1/places/"),
			("DELETE", "v1/places/abc"),
		):
			self.assertIsNone(proxy.route(method, path), (method, path))


class TestAutocompleteBody(unittest.TestCase):
	def test_only_the_input_and_session_pass(self):
		body = json.dumps({"input": "Rua da Aurora", "sessionToken": "a1-b2", "includedPrimaryTypes": ["x"]})
		self.assertEqual(
			proxy.autocomplete_body(body.encode()), {"input": "Rua da Aurora", "sessionToken": "a1-b2"}
		)

	def test_malformed_requests_are_refused(self):
		for raw in (b"not json", b"[]", b"{}", json.dumps({"input": "x" * 501}).encode()):
			with self.assertRaises(proxy.Refused):
				proxy.autocomplete_body(raw)
		with self.assertRaises(proxy.Refused) as refused:
			proxy.autocomplete_body(b"{" + b" " * proxy.MAX_BODY + b"}")
		self.assertEqual(refused.exception.status, 413)

	def test_a_session_token_is_checked(self):
		with self.assertRaises(proxy.Refused):
			proxy.autocomplete_body(json.dumps({"input": "abc", "sessionToken": "a&b"}).encode())


class TestCallerAddress(unittest.TestCase):
	def test_ipv4_must_be_the_server_s_own(self):
		self.assertTrue(proxy.ip_matches("108.61.224.213", "108.61.224.213", None))
		self.assertFalse(proxy.ip_matches("108.61.224.214", "108.61.224.213", None))

	def test_ipv6_must_be_in_the_server_s_64(self):
		v6 = "2001:19f0:6400:24bb:5400:6ff:feb0:7585"
		self.assertTrue(proxy.ip_matches("2001:19f0:6400:24bb::1", None, v6))
		self.assertFalse(proxy.ip_matches("2001:19f0:6400:24bc::1", None, v6))

	def test_nothing_matches_a_missing_or_malformed_address(self):
		self.assertFalse(proxy.ip_matches(None, "108.61.224.213", None))
		self.assertFalse(proxy.ip_matches("unknown", "108.61.224.213", None))
		self.assertFalse(proxy.ip_matches("2001:db8::1", "108.61.224.213", None))
		self.assertFalse(proxy.ip_matches("108.61.224.213", None, None))


class TestQuota(unittest.TestCase):
	def test_a_daily_limit_of_zero_allows_nothing(self):
		self.assertTrue(proxy.over_quota(0, 0, 0, 0))

	def test_the_daily_and_monthly_ceilings(self):
		self.assertFalse(proxy.over_quota(9, 100, 10, 0))
		self.assertTrue(proxy.over_quota(10, 100, 10, 0))
		self.assertTrue(proxy.over_quota(1, 300, 10, 300))


class TestRefusals(unittest.TestCase):
	def test_geocode_over_quota_is_google_s_own_status(self):
		response = proxy.GeocodingProxyRenderer("geocode").refuse(
			proxy.GEOCODE, proxy.Refused(429, "used up", "OVER_DAILY_LIMIT")
		)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(json.loads(response.data)["status"], "OVER_DAILY_LIMIT")

	def test_places_refusals_are_google_s_error_shape(self):
		response = proxy.GeocodingProxyRenderer("v1/places/x").refuse(
			proxy.DETAILS, proxy.Refused(401, "Invalid token", "REQUEST_DENIED")
		)
		self.assertEqual(response.status_code, 401)
		self.assertEqual(json.loads(response.data)["error"]["status"], "REQUEST_DENIED")


class TestTheTokenIsNotALogin(unittest.TestCase):
	"""Frappe ends any request whose two-part Authorization header names no user with a 401."""

	def request(self, path):
		environ = EnvironBuilder(path=path, headers={"Authorization": "Bearer zzt"}).get_environ()
		previous = getattr(frappe.local, "request", None)
		frappe.local.request = Request(environ)
		self.addCleanup(setattr, frappe.local, "request", previous)
		return frappe.local.request

	def test_the_header_is_moved_aside_on_proxy_paths(self):
		request = self.request("/geocode")
		proxy.before_request()
		self.assertIsNone(request.headers.get("Authorization"))
		self.assertEqual(frappe.local.geocoding_authorization, "Bearer zzt")

	def test_other_paths_keep_it(self):
		request = self.request("/api/method/ping")
		proxy.before_request()
		self.assertEqual(request.headers.get("Authorization"), "Bearer zzt")


class TestHash(unittest.TestCase):
	def test_hash_is_stable_and_not_the_token(self):
		self.assertEqual(hash_token("abc"), hash_token("abc"))
		self.assertNotIn("abc", hash_token("abc"))
