"""Per-school buckets and keys (ADR 041 §2-§3).

`ensure_site_storage` is idempotent. It can be interrupted anywhere (a failed site insert rolls
back the Backup Bucket rows but not the Cloudflare objects) and a retry converges:

- a bucket Cloudflare says we own, with no Backup Bucket row, is adopted: every bucket Press
  keeps has a row, since rows are never deleted automatically (ADR 041 §3);
- a bucket with a row for another site is a name collision, and the next suffix is tried;
- keys are named after their bucket, so a retry deletes a key minted by an interrupted run
  before minting the one it stores.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frappe
from frappe.utils.password import get_decrypted_password, set_encrypted_password

from press.r2.cloudflare import Client, s3_endpoint

if TYPE_CHECKING:
	from press.press.doctype.site.site import Site

#: Cloudflare location hints per cluster: media near the school, backups away from it (ADR 041 §2).
LOCATIONS = {
	"dfw": {"media": "enam", "backups": "wnam"},
	"sao": {"media": "enam", "backups": "wnam"},
	"jnb": {"media": "weur", "backups": "eeur"},
	"sgp": {"media": "apac", "backups": "oc"},
}

#: Objects in a backup bucket cannot be deleted or overwritten for this long (ADR 041 §1).
LOCK_DAYS = 7
LOCK_RULE_ID = "aretenic-backup-lock"

#: Keys apotheke reads from site_config.json. Marked internal so the dashboard neither shows nor
#: accepts them, and restores strip them (a restore must never point a site at another's bucket).
MEDIA_CONFIG_KEYS = ("r2_account_id", "r2_bucket", "r2_endpoint", "r2_access_key_id", "r2_secret_access_key")

PURPOSE_FIELD = {"Site Backups": "r2_backup_bucket", "Site Media": "r2_media_bucket"}
SUFFIX = {"Site Backups": "backups", "Site Media": "media"}


def is_enabled() -> bool:
	return bool(frappe.db.get_single_value("Press Settings", "r2_account_id")) and bool(
		get_decrypted_password("Press Settings", "Press Settings", "r2_api_token", raise_exception=False)
	)


def get_client() -> Client:
	return Client(
		frappe.db.get_single_value("Press Settings", "r2_account_id"),
		get_decrypted_password("Press Settings", "Press Settings", "r2_api_token"),
	)


def ensure_site_storage(site: Site) -> None:
	"""Give a site its media and backup buckets and keys, and apotheke its config.

	On a site not yet inserted (called from `Site.before_insert`) the links and config are set on
	the document; on an existing site they are saved and pushed to the server.
	"""
	if not is_enabled():
		return
	locations = LOCATIONS.get(site.cluster)
	if not locations:
		frappe.throw(f"No R2 locations are defined for cluster {site.cluster} (press/r2/storage.py)")

	client = get_client()
	for purpose, field in PURPOSE_FIELD.items():
		if site.get(field) and frappe.db.exists("Backup Bucket", site.get(field)):
			continue
		bucket = _ensure_bucket(client, site, purpose, locations)
		if site.is_new():
			site.set(field, bucket)
		else:
			site.db_set(field, bucket)

	config = get_media_config(site.r2_media_bucket)
	ensure_internal_config_keys()
	current = {row.key: row.value for row in site.configuration}
	if all(str(current.get(k)) == str(v) for k, v in config.items()):
		return
	if site.is_new():
		site._update_configuration(config, save=False)
	else:
		site.update_site_config(config)


def _ensure_bucket(client: Client, site: Site, purpose: str, locations: dict) -> str:
	kind = SUFFIX[purpose]
	hint = locations[kind]
	for attempt in range(1, 10):
		name = f"{site.subdomain}-{kind}" if attempt == 1 else f"{site.subdomain}-{kind}-{attempt}"
		row = frappe.db.get_value("Backup Bucket", name, ["site", "purpose"], as_dict=True)
		if row and (row.site != site.name or row.purpose != purpose):
			continue  # belongs to another site, archived or not: never reuse it
		# Created now, ours from an earlier try (row), or left over from an interrupted run (no row)
		client.create_bucket(name, hint)
		break
	else:
		frappe.throw(f"No free R2 bucket name for {site.name} ({kind})")

	info = client.get_bucket(name) or {}
	location = (info.get("location") or "").lower()
	if purpose == "Site Backups":
		if location == locations["media"]:
			frappe.throw(
				f"R2 placed backup bucket {name} in {location}, the school's own location. "
				"ADR 041 §2 requires another region: move or recreate it by hand."
			)
		_ensure_lock(client, name)

	if not row:
		bucket_doc = frappe.get_doc(
			{
				"doctype": "Backup Bucket",
				"bucket_name": name,
				"purpose": purpose,
				"site": site.name,
				"cluster": site.cluster,
				"region": "auto",
				"endpoint_url": client.s3_endpoint,
				"location": location,
			}
		)
		# From Site.before_insert the site row does not exist yet
		bucket_doc.flags.ignore_links = True
		bucket_doc.insert(ignore_permissions=True)
	else:
		frappe.db.set_value("Backup Bucket", name, "location", location)

	if not frappe.db.get_value("Backup Bucket", name, "access_key_id"):
		key_name = f"press-{name}"
		for stale in client.find_tokens(key_name):
			client.delete_token(stale["id"])
		access_key_id, secret = client.create_s3_key(key_name, [name])
		bucket_doc = frappe.get_doc("Backup Bucket", name)
		bucket_doc.access_key_id = access_key_id
		bucket_doc.secret_access_key = secret
		bucket_doc.flags.ignore_links = True
		bucket_doc.save(ignore_permissions=True)
	return name


def _ensure_lock(client: Client, bucket: str) -> None:
	rules = [r for r in client.get_lock_rules(bucket) if r.get("id") != LOCK_RULE_ID]
	rules.append(
		{
			"id": LOCK_RULE_ID,
			"enabled": True,
			"prefix": "",
			"condition": {"type": "Age", "maxAgeSeconds": LOCK_DAYS * 86400},
		}
	)
	client.set_lock_rules(bucket, rules)
	if not any(r.get("id") == LOCK_RULE_ID and r.get("enabled") for r in client.get_lock_rules(bucket)):
		frappe.throw(f"R2 did not keep the lock rule on {bucket}")


def get_media_config(bucket: str) -> dict:
	account_id = frappe.db.get_single_value("Press Settings", "r2_account_id")
	return {
		"r2_account_id": account_id,
		"r2_bucket": bucket,
		"r2_endpoint": s3_endpoint(account_id),
		"r2_access_key_id": frappe.db.get_value("Backup Bucket", bucket, "access_key_id"),
		"r2_secret_access_key": get_decrypted_password("Backup Bucket", bucket, "secret_access_key"),
	}


def ensure_internal_config_keys() -> None:
	for key in MEDIA_CONFIG_KEYS:
		if frappe.db.exists("Site Config Key", key):
			if not frappe.db.get_value("Site Config Key", key, "internal"):
				frappe.db.set_value("Site Config Key", key, "internal", 1)
			continue
		frappe.get_doc(
			{
				"doctype": "Site Config Key",
				"key": key,
				"type": "Password" if "secret" in key else "String",
				"title": key.replace("_", " ").title(),
				"description": "Apotheke media storage (ADR 041). Set by Press.",
				"internal": 1,
			}
		).insert(ignore_permissions=True)


def get_site_backup_bucket(site: str) -> str | None:
	"""The site's own backup bucket, created on demand for sites older than this feature."""
	if not is_enabled():
		return None
	bucket = frappe.db.get_value("Site", site, "r2_backup_bucket")
	if bucket and frappe.db.exists("Backup Bucket", bucket):
		return bucket
	doc = frappe.get_doc("Site", site)
	ensure_site_storage(doc)
	return frappe.db.get_value("Site", site, "r2_backup_bucket")


def get_press_s3_credentials() -> tuple[str, str]:
	"""Press's own all-buckets key, for server-side copies. Minted once, never sent to servers."""
	access_key_id = frappe.db.get_single_value("Press Settings", "r2_press_access_key_id")
	secret = get_decrypted_password(
		"Press Settings", "Press Settings", "r2_press_secret_access_key", raise_exception=False
	)
	if access_key_id and secret:
		return access_key_id, secret

	client = get_client()
	for stale in client.find_tokens("press-all-buckets"):
		client.delete_token(stale["id"])
	access_key_id, secret = client.create_s3_key("press-all-buckets", None)
	# Written directly: saving Press Settings would re-run validations unrelated to R2
	frappe.db.set_single_value("Press Settings", "r2_press_access_key_id", access_key_id)
	set_encrypted_password("Press Settings", "Press Settings", secret, "r2_press_secret_access_key")
	return access_key_id, secret
