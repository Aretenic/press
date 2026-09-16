"""Nightly copy of each school's media bucket into its backup bucket (ADR 041 §4).

Without it, lecture video exists in one copy, near the school.

- **Incremental and server-side:** objects missing from `media/` of the backup bucket, or of a
  different size, are copied inside R2 with Press's all-buckets key. No bytes pass through Press.
  Sizes are compared, not ETags: a large object is copied in parts, and its copy's ETag differs.
- **Deleted media is kept 35 days.** R2 has no object versioning, so the copy records when each
  object first went missing from the media bucket, and deletes the copy only after 35 days. An
  object that comes back is forgotten. The record lives on the backup bucket's Backup Bucket row,
  not in the bucket: the bucket lock refuses overwriting any object younger than 7 days.
- **Overwrites:** apotheke keys media by content hash, so an object never changes in place. A
  changed object is copied again; the backup bucket's 7-day lock refuses that for a copy younger
  than 7 days, and the next night retries.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import frappe
from boto3 import client
from botocore.exceptions import ClientError

from press.r2.cloudflare import s3_endpoint
from press.r2.heartbeat import record_success
from press.r2.storage import get_press_s3_credentials, is_enabled
from press.utils import log_error

PREFIX = "media/"
KEEP_DELETED_DAYS = 35


def copy_all_media():
	"""Scheduled daily. One background job per site."""
	if not is_enabled():
		return
	sites = frappe.get_all(
		"Site",
		{"status": ("!=", "Archived"), "r2_media_bucket": ("is", "set"), "r2_backup_bucket": ("is", "set")},
		pluck="name",
	)
	for site in sites:
		frappe.enqueue(
			"press.r2.media_backup.copy_site_media",
			site=site,
			queue="long",
			timeout=6 * 3600,
			job_id=f"r2_media_backup:{site}",
			deduplicate=True,
			enqueue_after_commit=True,
		)


def copy_site_media(site: str, today: date | None = None) -> dict:
	today = today or date.today()
	media, backups = frappe.db.get_value("Site", site, ["r2_media_bucket", "r2_backup_bucket"])
	s3 = get_s3(media)
	stats = {"copied": 0, "unchanged": 0, "failed": 0, "missing": 0, "deleted": 0}

	source = list_objects(s3, media, "")
	copies = list_objects(s3, backups, PREFIX)
	copy_new(s3, site, media, backups, source, copies, stats)
	expire_missing(s3, site, backups, source, copies, today, stats)

	if stats["failed"]:
		log_error("R2 Media Backup Incomplete", site=site, stats=stats)
	else:
		record_success("media_backup", site)
	return stats


def copy_new(s3, site: str, media: str, backups: str, source: dict, copies: dict, stats: dict) -> None:
	for key, size in source.items():
		if copies.get(PREFIX + key) == size:
			stats["unchanged"] += 1
			continue
		try:
			s3.copy({"Bucket": media, "Key": key}, backups, PREFIX + key)
			stats["copied"] += 1
		except Exception as e:  # ClientError, or boto3's transfer errors for part copies
			stats["failed"] += 1
			log_error("R2 Media Backup Copy Failed", site=site, key=key, error=str(e))


def expire_missing(s3, site: str, backups: str, source: dict, copies: dict, today: date, stats: dict) -> None:
	missing = json.loads(frappe.db.get_value("Backup Bucket", backups, "media_missing") or "{}")
	for key in list(missing):
		if key in source or PREFIX + key not in copies:
			missing.pop(key)  # back in the media bucket, or its copy is already gone
	for copy_key in copies:
		key = copy_key[len(PREFIX) :]
		if key in source:
			continue
		first_missing = date.fromisoformat(missing.setdefault(key, today.isoformat()))
		if today - first_missing < timedelta(days=KEEP_DELETED_DAYS):
			stats["missing"] += 1
			continue
		try:
			s3.delete_object(Bucket=backups, Key=copy_key)
			missing.pop(key)
			stats["deleted"] += 1
		except ClientError as e:
			stats["failed"] += 1
			log_error("R2 Media Backup Delete Failed", site=site, key=copy_key, error=str(e))
	frappe.db.set_value("Backup Bucket", backups, "media_missing", json.dumps(missing, sort_keys=True))
	frappe.db.commit()


def get_s3(probe_bucket: str):
	access_key_id, secret = get_press_s3_credentials(probe_bucket)
	return client(
		"s3",
		aws_access_key_id=access_key_id,
		aws_secret_access_key=secret,
		endpoint_url=s3_endpoint(frappe.db.get_single_value("Press Settings", "r2_account_id")),
		region_name="auto",
	)


def list_objects(s3, bucket: str, prefix: str) -> dict[str, int]:
	objects = {}
	for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
		for obj in page.get("Contents", []):
			objects[obj["Key"]] = obj["Size"]
	return objects
