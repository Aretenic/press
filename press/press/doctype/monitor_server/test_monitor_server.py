# Copyright (c) 2021, Frappe and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from press.press.doctype.monitor_server.monitor_server import check_monitoring_servers_rate_limit_key

MONITOR_IP = "203.0.113.7"
MONITOR_SERVER = "press.press.doctype.monitor_server.monitor_server"


@patch(f"{MONITOR_SERVER}.get_monitor_server_ips", return_value=[MONITOR_IP])
@patch("press.telegram_utils.Telegram")
class TestMonitoringRateLimitKey(FrappeTestCase):
	"""Aretenic: the cleanup must find the key frappe's rate_limit actually writes."""

	def setUp(self):
		self.key = frappe.cache.make_key(f"rl:press.api.monitoring.targets:{MONITOR_IP}")
		self.addCleanup(frappe.cache.delete, self.key)

	def test_deletes_a_counter_with_no_expiry(self, telegram, _ips):
		frappe.cache.set(self.key, 5)  # INCRBY on an expired key: no TTL, never resets
		check_monitoring_servers_rate_limit_key()
		self.assertIsNone(frappe.cache.get(self.key))
		telegram.assert_called_once()

	def test_deletes_a_counter_that_is_too_high(self, telegram, _ips):
		frappe.cache.setex(self.key, 60, 101)
		check_monitoring_servers_rate_limit_key()
		self.assertIsNone(frappe.cache.get(self.key))

	def test_deletes_frappe_16s_key_too(self, telegram, _ips):
		v16_key = self.key + b":60"  # frappe 16 appends the window
		self.addCleanup(frappe.cache.delete, v16_key)
		frappe.cache.set(v16_key, 5)
		check_monitoring_servers_rate_limit_key()
		self.assertIsNone(frappe.cache.get(v16_key))

	def test_keeps_a_healthy_counter(self, telegram, _ips):
		frappe.cache.setex(self.key, 60, 2)
		check_monitoring_servers_rate_limit_key()
		self.assertEqual(frappe.cache.get(self.key), b"2")
		telegram.assert_not_called()
