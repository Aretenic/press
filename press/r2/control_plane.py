"""Daily offsite backup of Press itself (ADR 041 §6).

Press's database is the map of every school: servers, sites, buckets and their keys. It goes daily
to ``press-control-plane-backups``, away from the Press host and locked like a school's backups.

- **What is uploaded:** Frappe's own backup of Press's site (database, public and private files)
  and its ``site_config.json``, encrypted to Press Settings' age public key. The config holds the
  ``encryption_key`` that decrypts every Password field in the database, so the database alone does
  not reveal Press's secrets, and the config is useless without the operator's private key.
- **Where:** ``<site>/<UTC timestamp>/<file>``, one prefix per run, with the bucket's own key.
- **Retention:** as a school's full backups (ADR 041 §1): 7 daily, Sundays for 4 weeks, the 1st
  of the month for 12 months, nothing deleted inside the 7-day lock.
- **Restoring needs nothing from the lost host:** the Cloudflare account (or its API token) to
  download, and the operator's copy of the age private key to decrypt the config.

Vultr's automatic backups of the Press host remain a same-region convenience, not the backup.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

import frappe
from boto3 import client

from press.press.doctype.backup_bucket.backup_bucket import get_bucket_credentials
from press.r2.retention import DAILY_DAYS, LOCK_MARGIN, MONTHLY_DAY, MONTHLY_DAYS, WEEKLY_WEEKS
from press.r2.storage import LOCATIONS, LOCK_DAYS, ensure_bucket, get_client, is_enabled
from press.utils import log_error

BUCKET = "press-control-plane-backups"
#: The Press host is in Dallas (ADR 039); its backups go where Dallas schools' backups go.
PRESS_HOST_CLUSTER = "dfw"
PURPOSE = "Control Plane"
TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"


def backup_control_plane():
	"""Scheduled daily."""
	if not is_enabled():
		return
	frappe.enqueue(
		"press.r2.control_plane.run",
		queue="long",
		timeout=3600,
		job_id="r2_control_plane_backup",
		deduplicate=True,
		enqueue_after_commit=True,
	)


def run(now: datetime | None = None) -> dict:
	now = now or datetime.now(timezone.utc)
	try:
		bucket = get_control_plane_bucket()
		uploaded = upload_backup(bucket, now)
		deleted = expire(bucket, now)
	except Exception:
		log_error("R2 Control Plane Backup Failed")
		raise
	return {"bucket": bucket, "uploaded": uploaded, "deleted": deleted}


def get_control_plane_bucket() -> str:
	existing = frappe.db.get_value("Backup Bucket", {"purpose": PURPOSE})
	if existing and frappe.db.get_value("Backup Bucket", existing, "access_key_id"):
		return existing
	locations = LOCATIONS[PRESS_HOST_CLUSTER]
	return ensure_bucket(
		get_client(),
		base_name=BUCKET,
		purpose=PURPOSE,
		hint=locations["backups"],
		cluster=PRESS_HOST_CLUSTER,
		site=None,
		locked=True,
		avoid_location=locations["media"],
	)


def upload_backup(bucket: str, now: datetime) -> dict[str, int]:
	from frappe.utils.backups import new_backup

	recipient = frappe.db.get_single_value("Press Settings", "r2_config_age_recipient")
	if not (recipient or "").startswith("age1"):
		frappe.throw("Press Settings has no age public key: the control-plane config would go unencrypted")

	workdir = tempfile.mkdtemp(prefix="press-control-plane-")
	try:
		backup = new_backup(ignore_files=False, compress=True, force=True, backup_path=workdir)
		files = [
			backup.backup_path_db,
			backup.backup_path_files,
			backup.backup_path_private_files,
			encrypt_with_age(backup.backup_path_conf, recipient),
		]
		prefix = f"{frappe.local.site}/{now.strftime(TIMESTAMP_FORMAT)}/"
		s3 = get_s3(bucket)
		uploaded = {}
		for path in files:
			key = prefix + os.path.basename(path)
			s3.upload_file(path, bucket, key)
			size = os.path.getsize(path)
			if s3.head_object(Bucket=bucket, Key=key)["ContentLength"] != size:
				frappe.throw(f"R2 holds a different size for {key}")
			uploaded[key] = size
		return uploaded
	finally:
		shutil.rmtree(workdir, ignore_errors=True)


def encrypt_with_age(path: str, recipient: str) -> str:
	"""Encrypt next to the file and delete the plaintext copy (the site's own config stays)."""
	encrypted = f"{path}.age"
	subprocess.run(["age", "--encrypt", "--recipient", recipient, "--output", encrypted, path], check=True)
	if not os.path.getsize(encrypted):
		frappe.throw(f"age produced an empty file for {path}")
	os.remove(path)
	return encrypted


def expire(bucket: str, now: datetime) -> list[str]:
	s3 = get_s3(bucket)
	prefix = f"{frappe.local.site}/"
	runs: dict[str, list[str]] = {}
	for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
		for obj in page.get("Contents", []):
			run_prefix = obj["Key"][len(prefix) :].split("/", 1)[0]
			runs.setdefault(run_prefix, []).append(obj["Key"])

	deleted = []
	for run_prefix, keys in sorted(runs.items()):
		try:
			taken = datetime.strptime(run_prefix, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
		except ValueError:
			continue  # not written by this module: leave it
		if not is_due_for_expiry(taken, now):
			continue
		response = s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": key} for key in keys]})
		if response.get("Errors"):  # R2 reports refused deletes here, without raising
			log_error("R2 Control Plane Backup Expiry Failed", run=run_prefix, errors=response["Errors"])
			continue
		deleted.append(run_prefix)
	return deleted


def is_due_for_expiry(taken: datetime, now: datetime) -> bool:
	age = now - taken
	if age < max(timedelta(days=DAILY_DAYS), timedelta(days=LOCK_DAYS)) + LOCK_MARGIN:
		return False
	if taken.weekday() == 6 and age < timedelta(weeks=WEEKLY_WEEKS):  # Sunday
		return False
	if taken.day == MONTHLY_DAY and age < timedelta(days=MONTHLY_DAYS):
		return False
	return True


def get_s3(bucket: str):
	credentials = get_bucket_credentials(bucket)
	return client(
		"s3",
		aws_access_key_id=credentials["access_key_id"],
		aws_secret_access_key=credentials["secret_access_key"],
		endpoint_url=credentials["endpoint_url"],
		region_name="auto",
	)
