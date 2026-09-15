# Copyright (c) 2022, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe.model.document import Document


class BackupBucket(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		access_key_id: DF.Data | None
		bucket_name: DF.Data | None
		cluster: DF.Link | None
		endpoint_url: DF.Data | None
		location: DF.Data | None
		media_missing: DF.JSON | None
		purpose: DF.Literal["Cluster Backups", "Site Backups", "Site Media", "Binlogs", "Control Plane"]
		region: DF.Data | None
		replication_bucket: DF.Data | None
		replication_enabled: DF.Check
		replication_endpoint_url: DF.Data | None
		replication_region: DF.Data | None
		secret_access_key: DF.Password | None
		site: DF.Link | None
	# end: auto-generated types


def get_replication_target(bucket_name: str) -> dict | None:
	"""Where reads go once a bucket replicates, or None when it doesn't.

	Backups are written to the primary and replicated out, so the replica is what still
	holds an object the primary's lifecycle rules have already expired.
	"""
	if not frappe.db.exists("Backup Bucket", bucket_name):
		return None

	bucket: BackupBucket = frappe.get_doc("Backup Bucket", bucket_name)
	if not bucket.replication_enabled:
		return None

	return {
		"name": bucket.replication_bucket,
		"region": bucket.replication_region,
		"endpoint_url": bucket.replication_endpoint_url,
	}


def get_bucket_credentials(bucket_name: str) -> dict:
	"""Region, endpoint and keys for one bucket.

	A bucket with its own key (per-site buckets, ADR 041) uses only that key. Any other bucket
	falls back to Press Settings' offsite keys, as upstream Press does.
	"""
	from frappe.utils.password import get_decrypted_password

	settings_region = frappe.db.get_single_value("Press Settings", "backup_region")
	credentials = {
		"name": bucket_name,
		"region": settings_region,
		"endpoint_url": None,
		"access_key_id": frappe.db.get_single_value("Press Settings", "offsite_backups_access_key_id"),
		"secret_access_key": get_decrypted_password(
			"Press Settings", "Press Settings", "offsite_backups_secret_access_key", raise_exception=False
		),
	}
	if not bucket_name or not frappe.db.exists("Backup Bucket", bucket_name):
		return credentials

	bucket = frappe.db.get_value(
		"Backup Bucket", bucket_name, ["region", "endpoint_url", "access_key_id"], as_dict=True
	)
	credentials["region"] = bucket.region or settings_region
	credentials["endpoint_url"] = bucket.endpoint_url or None
	if bucket.access_key_id:
		credentials["access_key_id"] = bucket.access_key_id
		credentials["secret_access_key"] = get_decrypted_password(
			"Backup Bucket", bucket_name, "secret_access_key"
		)
	return credentials
