"""Daily check that every bucket is still where it was put and still locked (ADR 041 §2-§3).

Buckets are checked once, at creation. Afterwards the account token Press holds could move nothing
but could remove a lock rule (ADR 041 §1), and a bucket could be deleted by hand. Each problem is an
`R2 ...` Error Log, which `press.metrics` counts and Prometheus alerts on (ADR 043 §2); the run's
heartbeat makes a missed run visible too. Daily rather than weekly so the existing 26-hour
heartbeat rule covers it.
"""

from __future__ import annotations

import frappe

from press.r2.heartbeat import record_success
from press.r2.storage import LOCATIONS, LOCK_DAYS, LOCK_RULE_ID, get_client, is_enabled

ERROR_TITLE = "R2 Bucket Drift"
#: Purposes whose buckets carry the object lock (storage.ensure_bucket(locked=True)).
LOCKED_PURPOSES = ("Site Backups", "Binlogs", "Control Plane")
#: Purposes we create; Press's own "Cluster Backups" buckets are not ours to judge.
OUR_PURPOSES = ("Site Media", *LOCKED_PURPOSES)


def verify_buckets():
	from press.utils import log_error

	if not is_enabled():
		return
	client = get_client()
	rows = frappe.get_all(
		"Backup Bucket",
		filters={"purpose": ("in", OUR_PURPOSES)},
		fields=["name", "purpose", "cluster", "location"],
	)
	for row in rows:
		try:
			info = client.get_bucket(row.name)
			rules = client.get_lock_rules(row.name) if info and row.purpose in LOCKED_PURPOSES else []
		except Exception as e:
			log_error(ERROR_TITLE, bucket=row.name, problem=f"could not be read: {e}")
			continue
		for problem in bucket_problems(row.purpose, row.cluster, row.location, info, rules):
			log_error(ERROR_TITLE, bucket=row.name, problem=problem)
	record_success("bucket_verify")


def bucket_problems(
	purpose: str, cluster: str, recorded: str | None, info: dict | None, lock_rules: list[dict]
) -> list[str]:
	"""What is wrong with one bucket, in words; empty when nothing is."""
	if info is None:
		return ["the bucket no longer exists"]

	problems = []
	location = (info.get("location") or "").lower()
	expected = LOCATIONS.get(cluster, {}).get("media" if purpose == "Site Media" else "backups")
	if expected and location != expected:
		problems.append(f"is in {location or 'an unknown location'}, expected {expected}")
	if recorded and location and location != recorded.lower():
		problems.append(f"moved from {recorded} to {location}")

	if purpose in LOCKED_PURPOSES and not has_lock(lock_rules):
		problems.append(f"has lost its {LOCK_DAYS}-day lock")
	return problems


def has_lock(rules: list[dict]) -> bool:
	return any(
		rule.get("id") == LOCK_RULE_ID
		and rule.get("enabled")
		and rule.get("prefix", "") == ""
		and (rule.get("condition") or {}).get("type") == "Age"
		and (rule.get("condition") or {}).get("maxAgeSeconds", 0) >= LOCK_DAYS * 86400
		for rule in rules
	)
