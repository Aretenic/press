# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# For license information, please see license.txt

import hmac
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import add_to_date, cint, get_datetime, get_system_timezone
from prometheus_client import (
	CollectorRegistry,
	Gauge,
	generate_latest,
)
from werkzeug.wrappers import Response

from press.r2.heartbeat import last_successes

#: Error Log titles written by the R2 backup jobs (ADR 041); counted so Prometheus can alert on them.
R2_ERROR_TITLE_PREFIX = "R2 "
#: How far back failures are counted: longer than the daily jobs' interval.
ERROR_WINDOW_HOURS = 26


class MetricsRenderer:
	def __init__(self, path, status_code=None):
		self.path = path
		self.registry = CollectorRegistry(auto_describe=True)

	def get_status(self, metric, doctype, status_field="status", filters=None):
		if filters is None:
			filters = {}
		c = Gauge(metric, "", [status_field], registry=self.registry)
		rows = frappe.get_all(
			doctype,
			fields=[status_field, "count(*) as count"],
			filters=filters,
			group_by=status_field,
			order_by=f"{status_field} asc",
			ignore_ifnull=True,
		)
		for row in rows:
			c.labels(row[status_field]).set(row.count)

	def metrics(self):
		suspended_builds = Gauge(
			"press_builds_suspended", "Are docker builds suspended", registry=self.registry
		)
		suspended_builds.set(
			cint(frappe.db.get_value("Press Settings", None, "suspend_builds"))
		)
		# Aretenic: Deploy Candidate has no status column any more; builds carry it
		self.get_status(
			"press_deploy_candidate_build_total",
			"Deploy Candidate Build",
			filters={"status": ("!=", "Success")},
		)

		self.get_status("press_site_total", "Site", filters={"status": ("!=", "Archived")})
		self.get_status("press_bench_total", "Bench", filters={"status": ("!=", "Archived")})
		self.get_status("press_server_total", "Server")

		self.get_status("press_database_server_total", "Database Server")
		self.get_status("press_virtual_machine_total", "Virtual Machine")

		self.get_status(
			"press_site_backup_total", "Site Backup", filters={"status": ("!=", "Success")}
		)
		self.get_status(
			"press_site_update_total", "Site Update", filters={"status": ("!=", "Success")}
		)
		self.get_status("press_site_migration_total", "Site Migration")
		self.get_status("press_site_upgrade_total", "Version Upgrade")

		self.get_status("press_press_job_total", "Press Job")
		self.get_status(
			"press_ansible_play_total", "Ansible Play", filters={"status": ("!=", "Success")}
		)
		self.get_status(
			"press_agent_job_total", "Agent Job", filters={"status": ("!=", "Success")}
		)

		self.backup_freshness()
		self.binlog_freshness()
		self.r2_jobs()
		self.registry_token_expiry()

		return generate_latest(self.registry).decode("utf-8")

	def backup_freshness(self):
		"""Aretenic (ADR 043 §2): last successful offsite backup per active site.

		A site with none yet reports its creation time, so a new site alerts only once it is overdue."""
		gauge = Gauge(
			"press_offsite_backup_last_success_timestamp",
			"Unix time of the site's last successful offsite backup",
			["site"],
			registry=self.registry,
		)
		sites = frappe.get_all(
			"Site",
			filters={"status": "Active", "skip_scheduled_logical_backups": 0},
			fields=["name", "creation"],
		)
		if not sites:
			return
		last = dict(
			frappe.get_all(
				"Site Backup",
				filters={"status": "Success", "offsite": 1, "site": ("in", [s.name for s in sites])},
				fields=["site", "max(creation) as last"],
				group_by="site",
				as_list=True,
			)
		)
		for site in sites:
			gauge.labels(site.name).set(to_unix(last.get(site.name) or site.creation))

	def binlog_freshness(self):
		"""Aretenic (ADR 043 §2): last binlog upload per database server that uploads binlogs."""
		gauge = Gauge(
			"press_binlog_last_upload_timestamp",
			"Unix time of the database server's last binlog upload",
			["database_server"],
			registry=self.registry,
		)
		servers = frappe.get_all(
			"Database Server",
			filters={"status": "Active", "enable_binlog_upload_to_s3": 1},
			fields=["name", "creation"],
		)
		for server in servers:
			last = frappe.db.sql(
				"""
				select max(remote.creation)
				from `tabMariaDB Binlog` binlog
				join `tabRemote File` remote on remote.name = binlog.remote_file
				where binlog.database_server = %s and binlog.uploaded = 1
				""",
				server.name,
			)[0][0]
			gauge.labels(server.name).set(to_unix(last or server.creation))

	def r2_jobs(self):
		"""Aretenic (ADR 043 §2): last success of each R2 backup job, and its recent failures."""
		success = Gauge(
			"press_r2_job_last_success_timestamp",
			"Unix time of the R2 job's last successful run",
			["job", "site"],
			registry=self.registry,
		)
		for job, site, timestamp in last_successes():
			success.labels(job, site).set(timestamp)

		failures = Gauge(
			"press_r2_error_log_recent_total",
			f"R2 job errors logged in the last {ERROR_WINDOW_HOURS} hours",
			["title"],
			registry=self.registry,
		)
		rows = frappe.get_all(
			"Error Log",
			filters={
				"method": ("like", f"{R2_ERROR_TITLE_PREFIX}%"),
				"creation": (">", add_to_date(None, hours=-ERROR_WINDOW_HOURS)),
			},
			fields=["method", "count(*) as count"],
			group_by="method",
		)
		for row in rows:
			failures.labels(row.method).set(row.count)

	def registry_token_expiry(self):
		"""Aretenic (ADR 043 §2): the image registry token's expiry, set by hand in Press Settings."""
		expires_on = frappe.db.get_single_value("Press Settings", "docker_registry_password_expires_on")
		if not expires_on:
			return
		gauge = Gauge(
			"press_registry_token_expiry_timestamp",
			"Unix time the image registry token expires",
			registry=self.registry,
		)
		gauge.set(to_unix(expires_on))

	def can_render(self):
		if self.path in ("metrics",):
			return True

	def render(self):
		if not self.is_authorized():
			return Response("Unauthorized", status=401, headers={"WWW-Authenticate": 'Basic realm="metrics"'})
		response = Response()
		response.mimetype = "text"
		response.data = self.metrics()
		return response

	def is_authorized(self) -> bool:
		"""Aretenic (ADR 043 §1): the monitor scrapes with Press Settings' monitoring password.

		Fails closed: the metrics name every school's site, and no nginx guards the path here."""
		password = frappe.get_single("Press Settings").get_password(
			"press_monitoring_password", raise_exception=False
		)
		if not password:
			return False
		auth = frappe.request.authorization if frappe.request else None
		return bool(
			auth
			and auth.type == "basic"
			and auth.username == "frappe"
			and hmac.compare_digest(auth.password or "", password)
		)


def to_unix(value) -> float:
	"""Press stores naive datetimes in the system timezone."""
	return get_datetime(value).replace(tzinfo=ZoneInfo(get_system_timezone())).timestamp()
