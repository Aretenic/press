# Copyright (c) 2021, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import frappe
import yaml
from frappe.core.utils import find
from frappe.model.document import Document

from press.agent import Agent

if TYPE_CHECKING:
	from press.press.doctype.server.server import Server

WATCHDOG_ALERT = "Watchdog"


def get_email_recipients(settings) -> str:
	return ", ".join(email.strip() for email in (settings.email_recipients or "").split(",") if email.strip())


def get_alertmanager_email_global(settings) -> dict | None:
	password = settings.get_password("alertmanager_smtp_password", raise_exception=False)
	if not (
		settings.alertmanager_smtp_smarthost
		and settings.alertmanager_smtp_username
		and password
		and get_email_recipients(settings)
	):
		return None
	return {
		"smtp_smarthost": settings.alertmanager_smtp_smarthost,
		"smtp_from": settings.alertmanager_smtp_username,
		"smtp_auth_username": settings.alertmanager_smtp_username,
		"smtp_auth_password": password,
		"smtp_require_tls": True,
	}


class PrometheusAlertRule(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from press.press.doctype.prometheus_alert_rule_cluster.prometheus_alert_rule_cluster import (
			PrometheusAlertRuleCluster,
		)

		alert_preview: DF.Code | None
		annotations: DF.Code
		description: DF.Data
		enabled: DF.Check
		expression: DF.Code | None
		group_by: DF.Code
		group_interval: DF.Data
		group_wait: DF.Data
		ignore_on_clusters: DF.TableMultiSelect[PrometheusAlertRuleCluster]
		labels: DF.Code
		only_on_shared: DF.Check
		press_job_type: DF.Link | None
		repeat_interval: DF.Data
		route_preview: DF.Code | None
		severity: DF.Literal["Critical", "Warning", "Information"]
		silent: DF.Check
	# end: auto-generated types

	def validate(self):
		self.alert_preview = yaml.dump(self.get_rule())
		self.route_preview = yaml.dump(self.get_route())
		if self.enabled and not self.expression:
			frappe.throw("Please add an expression for this alert rule before enabling it.")

	def get_rule(self):
		labels = json.loads(self.labels)
		labels.update({"severity": self.severity.lower()})

		annotations = json.loads(self.annotations)
		annotations.update({"description": self.description})

		return {
			"alert": self.name,
			"expr": self.expression,
			"for": self.get("for"),
			"labels": labels,
			"annotations": annotations,
		}

	def get_route(self):
		return {
			"group_by": json.loads(self.group_by),
			"group_wait": self.group_wait,
			"group_interval": self.group_interval,
			"repeat_interval": self.repeat_interval,
			"matchers": [f'alertname="{self.name}"'],
		}

	def on_update(self):
		rules = yaml.dump(self.get_rules())
		routes = yaml.dump(self.get_routes())

		monitoring_server = frappe.db.get_single_value("Press Settings", "monitor_server")
		agent = Agent(monitoring_server, "Monitor Server")
		agent.update_monitor_rules(rules, routes)

	def get_rules(self):
		rules_dict = {"groups": [{"name": "All", "rules": []}]}

		rules = frappe.get_all(self.doctype, {"enabled": True})
		for rule in rules:
			rule_doc = frappe.get_doc(self.doctype, rule.name)
			rules_dict["groups"][0]["rules"].append(rule_doc.get_rule())

		return rules_dict

	def get_routes(self):
		settings = frappe.get_single("Press Settings")
		webhook_token = frappe.db.get_value("Monitor Server", settings.monitor_server, "webhook_token")

		callback_url = frappe.utils.get_url("api/method/press.api.monitoring.alert")
		if webhook_token:
			callback_url = f"{callback_url}?webhook_token={webhook_token}"

		press_receiver = {
			"name": "web.hook",
			"webhook_configs": [{"url": callback_url}],
		}
		routes_dict = {
			"route": {"receiver": "web.hook", "routes": []},
			"receivers": [press_receiver],
		}

		# Aretenic (ADR 043 §4): Alertmanager also emails directly, so email alerts do not depend on Press
		if email_global := get_alertmanager_email_global(settings):
			routes_dict["global"] = email_global
			press_receiver["email_configs"] = [
				{"to": get_email_recipients(settings), "send_resolved": True},
			]

		# Aretenic (ADR 043 §3): the always-firing Watchdog pings an external check; silence raises its alert
		watchdog_url = settings.get_password("watchdog_ping_url", raise_exception=False)
		if watchdog_url:
			routes_dict["receivers"].append(
				{"name": "watchdog", "webhook_configs": [{"url": watchdog_url, "send_resolved": False}]}
			)
			routes_dict["route"]["routes"].append(
				{
					"receiver": "watchdog",
					"matchers": [f'alertname="{WATCHDOG_ALERT}"'],
					"group_wait": "0s",
					"group_interval": "1m",
					"repeat_interval": "1m",
				}
			)

		rules = frappe.get_all(self.doctype, {"enabled": True})
		for rule in rules:
			if watchdog_url and rule.name == WATCHDOG_ALERT:
				continue
			rule_doc = frappe.get_doc(self.doctype, rule.name)
			routes_dict["route"]["routes"].append(rule_doc.get_route())

		return routes_dict

	def react(self, instance_type: str, instance: str, labels: dict | None = None):
		return self.run_press_job(self.press_job_type, instance_type, instance, labels)  # type: ignore[arg-type]

	def run_press_job(
		self, job_name: str, server_type: str, server_name: str, labels: dict | None = None, arguments=None
	):
		server: "Server" = frappe.get_doc(server_type, server_name)
		if self.only_on_shared and not server.public:
			return None
		if find(self.ignore_on_clusters, lambda x: x.cluster == server.cluster):
			return None

		if arguments is None:
			arguments = {}

		if not labels:
			labels = {}

		arguments.update({"labels": labels})

		if existing_jobs := frappe.get_all(
			"Press Job",
			{
				"status": ("in", ["Pending", "Running"]),
				"server_type": server_type,
				"server": server_name,
			},
			pluck="name",
		):
			return frappe.get_doc("Press Job", existing_jobs[0])

		return frappe.get_doc(
			{
				"doctype": "Press Job",
				"job_type": job_name,
				"server_type": server_type,
				"server": server_name,
				"virtual_machine": server.virtual_machine,
				"arguments": json.dumps(arguments, indent=2, sort_keys=True),
			}
		).insert()
