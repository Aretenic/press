"""Backup schedule and retention for sites with their own R2 backup bucket (ADR 041 §1).

- **Every scheduled backup goes offsite** (every `backup_interval` hours, 6 by default).
  Only the first offsite backup of the day carries files; the rest are database only.
- **Database-only backups** are kept 7 days.
- **Full backups** follow GFS: 7 daily, Sundays for 4 weeks, the 1st of the month for 12 months,
  and no yearly backups.
- **Nothing is deleted inside the bucket lock.** Every age limit is at least the 7-day lock plus a
  margin, because a delete the lock refuses would leave the object behind a backup already marked
  Unavailable.
- **Archived sites are not rotated.** Their backups go 6 months after archival (ADR 038 §10).

Sites without an R2 backup bucket keep Press's FIFO or GFS scheme, which exclude R2 sites.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import frappe

from press.r2.heartbeat import record_success
from press.r2.storage import LOCK_DAYS

DAILY_DAYS = 7
WEEKLY_WEEKS = 4
MONTHLY_DAYS = 366
WEEKLY_DAY = 1  # SQL DAYOFWEEK: 1 = Sunday
MONTHLY_DAY = 1
LOCK_MARGIN = timedelta(hours=1)


def r2_sites_subquery() -> str:
	return "select name from tabSite where ifnull(r2_backup_bucket, '') != ''"


def has_r2_backups(site: str) -> bool:
	return bool(frappe.db.get_value("Site", site, "r2_backup_bucket"))


def logical_backup_plan(site: str, day: date) -> tuple[bool, bool]:
	"""(offsite, with_files) for a scheduled logical backup of an R2 site."""
	from press.press.doctype.site_backup.site_backup import SiteBackup

	full_offsite_today = SiteBackup.backup_exists(site, day, {"offsite": True, "with_files": True})
	return True, not full_offsite_today


def backups_due_for_expiry(now: datetime | None = None) -> list[str]:
	now = now or frappe.utils.now_datetime()
	# Never younger than the lock, whatever the schedule says
	daily_cutoff = now - max(timedelta(days=DAILY_DAYS), timedelta(days=LOCK_DAYS)) - LOCK_MARGIN
	weekly_cutoff = now - timedelta(weeks=WEEKLY_WEEKS)
	monthly_cutoff = now - timedelta(days=MONTHLY_DAYS)

	return frappe.db.sql(
		f"""
		SELECT backup.name FROM `tabSite Backup` backup
		JOIN tabSite site ON site.name = backup.site
		WHERE
			site.status != "Archived"
			AND ifnull(site.r2_backup_bucket, '') != ''
			AND backup.status = "Success"
			AND backup.files_availability = "Available"
			AND backup.offsite = 1
			AND backup.physical = 0
			AND backup.creation < %(daily_cutoff)s
			AND (
				backup.with_files = 0
				OR (
					(DAYOFWEEK(backup.creation) != {WEEKLY_DAY} OR backup.creation < %(weekly_cutoff)s)
					AND (DAYOFMONTH(backup.creation) != {MONTHLY_DAY} OR backup.creation < %(monthly_cutoff)s)
				)
			)
		""",
		{"daily_cutoff": daily_cutoff, "weekly_cutoff": weekly_cutoff, "monthly_cutoff": monthly_cutoff},
		pluck=True,
	)


def cleanup_offsite():
	"""Expire and delete R2 sites' backups. Runs inside Press's daily offsite cleanup."""
	from press.press.doctype.remote_file.remote_file import delete_remote_backup_objects
	from press.press.doctype.site.backups import BackupRotationScheme

	scheme = BackupRotationScheme()
	remote_files = scheme._expire_and_get_remote_files(backups_due_for_expiry())
	delete_remote_backup_objects(remote_files)
	record_success("offsite_rotation")
