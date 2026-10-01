"""Checking agora's onboarding spec (ADR 044 §3, ADR 040). Pure checks, no database."""

import copy
import unittest
from datetime import date

from press.school_provisioning import spec as S

SPEC = {
	"spec_version": 1,
	"onboarding": "ONB-00001",
	"provisioning_token": "t" * 32,
	"infra": {
		"subdomain": "grace",
		"cluster": "dfw",
		"release_group": "bench-0002",
		"apps": ["frappe", "seminary", "aretenic", "tamias"],
		"plan": "Aretenic Standard",
	},
	"site": {
		"company": "Grace Seminary Inc.",
		"abbreviation": "GS",
		"country": "United States",
		"language": "en",
		"currency": "USD",
		"time_zone": "America/Chicago",
		"fiscal_year_start": "07-01",
		"administrator": {"first_name": "Ada", "last_name": "Admin", "email": "ada@grace.edu"},
	},
	"datasets": [{"dataset_type": "people", "url": "https://aretenic.com/x", "sha256": "ab" * 32}],
}


def spec(**changes):
	s = copy.deepcopy(SPEC)
	for path, value in changes.items():
		target = s
		*parents, key = path.split("__")
		for p in parents:
			target = target[p]
		target[key] = value
	return s


class TestSpecErrors(unittest.TestCase):
	def test_a_complete_spec_is_accepted(self):
		self.assertEqual(S.spec_errors(spec()), [])

	def test_an_unknown_version_is_refused(self):
		self.assertEqual(S.spec_errors(spec(spec_version=2)), ["Spec version 2 is not supported."])

	def test_missing_fields_are_named(self):
		errors = S.spec_errors(spec(infra__plan=None, site__administrator__email=""))
		self.assertIn("infra.plan is missing.", errors)
		self.assertIn("administrator.email is missing.", errors)

	def test_erpnext_needs_a_chart_and_oikonomos_needs_erpnext(self):
		self.assertIn(
			"site.chart_of_accounts is required with erpnext.",
			S.spec_errors(spec(infra__apps=["frappe", "erpnext"])),
		)
		self.assertIn("oikonomos needs erpnext.", S.spec_errors(spec(infra__apps=["frappe", "oikonomos"])))

	def test_datasets_need_url_and_checksum(self):
		self.assertTrue(S.spec_errors(spec(datasets=[{"dataset_type": "people", "url": "x"}])))


class TestSubdomain(unittest.TestCase):
	def test_reserved_server_shaped_and_malformed_names_are_refused(self):
		for name in ("admin", "docs", "n1-dfw", "nfs2-sao", "Grace", "grace_sem", "-grace", "g" * 64):
			self.assertTrue(S.subdomain_errors(name), name)

	def test_ordinary_names_pass(self):
		for name in ("grace", "grace-seminary", "n1", "fs-campus", "t1link"):
			self.assertEqual(S.subdomain_errors(name), [], name)


class TestApps(unittest.TestCase):
	def test_oikonomos_waits_for_the_wizard_and_frappe_comes_first(self):
		s = spec(infra__apps=["erpnext", "frappe", "oikonomos", "seminary"])
		self.assertEqual(S.new_site_apps(s), ["frappe", "erpnext", "seminary"])
		self.assertEqual(S.after_wizard_apps(s), ["oikonomos"])


class TestSetupWizard(unittest.TestCase):
	def test_fiscal_year_contains_today(self):
		self.assertEqual(S.fiscal_year("07-01", date(2026, 10, 2)), (date(2026, 7, 1), date(2027, 6, 30)))
		self.assertEqual(S.fiscal_year("07-01", date(2026, 3, 2)), (date(2025, 7, 1), date(2026, 6, 30)))
		self.assertEqual(S.fiscal_year("01-01", date(2026, 1, 1)), (date(2026, 1, 1), date(2026, 12, 31)))

	def test_company_details_only_with_erpnext(self):
		data = S.setup_wizard_data(spec(), "English", date(2026, 10, 2))
		self.assertEqual(
			(data["language"], data["full_name"], data["email"]), ("English", "Ada Admin", "ada@grace.edu")
		)
		self.assertNotIn("company_name", data)
		self.assertNotIn("password", data)

		erp = spec(infra__apps=["frappe", "erpnext"], site__chart_of_accounts="Standard")
		data = S.setup_wizard_data(erp, "English", date(2026, 10, 2))
		self.assertEqual(
			(data["company_name"], data["company_abbr"], data["fy_start_date"], data["fy_end_date"]),
			("Grace Seminary Inc.", "GS", "2026-07-01", "2027-06-30"),
		)
