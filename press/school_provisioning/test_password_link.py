"""agora's request for the administrator's password link (Aretenic ADR 044c §5). No database."""

import unittest
from unittest.mock import MagicMock, patch

import frappe

from press.school_provisioning import password_link as P


class Thrown(Exception):
	pass


class FakeProvisioning(frappe._dict):
	def get_password(self, field):
		return self.token


def call(doc, site=None, token="right"):
	site = site or MagicMock()
	docs = {"School Provisioning": doc, "Site": site}

	def throw(message, *args, **kwargs):
		raise Thrown(message)

	with (
		patch.object(P.frappe, "throw", throw),
		patch.object(P.frappe, "db", MagicMock(**{"exists.return_value": True})),
		patch.object(P.frappe, "get_doc", lambda doctype, name: docs[doctype]),
	):
		return P.make("ONB-1", token), site


class TestPasswordLink(unittest.TestCase):
	def live(self, **kwargs):
		return FakeProvisioning(name="ONB-1", token="right", status="Live", site="s.example", **kwargs)

	def test_a_live_site_makes_the_link(self):
		site = MagicMock()
		site.get_connection_as_admin.return_value.post_api.return_value = (
			"https://s.example/update-password?key=k"
		)
		link, _ = call(self.live(), site)
		self.assertEqual(link, "https://s.example/update-password?key=k")
		site.get_connection_as_admin.return_value.post_api.assert_called_once_with(P.SITE_METHOD)

	def test_the_token_must_match(self):
		with self.assertRaises(frappe.PermissionError):
			call(self.live(), token="wrong")

	def test_only_a_live_site(self):
		with self.assertRaisesRegex(Thrown, "not live"):
			call(FakeProvisioning(name="ONB-1", token="right", status="Running", site="s.example"))

	def test_a_refusal_on_the_site_is_named(self):
		site = MagicMock()
		site.get_connection_as_admin.return_value.post_api.side_effect = Exception("already set a password")
		with self.assertRaisesRegex(
			frappe.ValidationError, "s.example did not make a link: already set a password"
		):
			call(self.live(), site)
