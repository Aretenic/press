"""The daily bucket check's judgement (ADR 041 §2-§3). Pure checks, no database and no Cloudflare."""

import unittest

from press.r2.storage import LOCK_DAYS, LOCK_RULE_ID
from press.r2.verify import bucket_problems, has_lock

LOCK = {
	"id": LOCK_RULE_ID,
	"enabled": True,
	"prefix": "",
	"condition": {"type": "Age", "maxAgeSeconds": LOCK_DAYS * 86400},
}


class TestBucketProblems(unittest.TestCase):
	def test_buckets_in_place_and_locked_are_fine(self):
		self.assertEqual(bucket_problems("Site Media", "dfw", "enam", {"location": "ENAM"}, []), [])
		self.assertEqual(bucket_problems("Site Backups", "dfw", "wnam", {"location": "WNAM"}, [LOCK]), [])
		self.assertEqual(bucket_problems("Binlogs", "dfw", "wnam", {"location": "WNAM"}, [LOCK]), [])

	def test_a_missing_bucket_is_reported(self):
		self.assertEqual(
			bucket_problems("Site Media", "dfw", "enam", None, []), ["the bucket no longer exists"]
		)

	def test_a_backup_bucket_in_the_wrong_place_is_reported(self):
		problems = bucket_problems("Site Backups", "dfw", "wnam", {"location": "ENAM"}, [LOCK])
		self.assertIn("is in enam, expected wnam", problems)
		self.assertIn("moved from wnam to enam", problems)

	def test_a_lost_lock_is_reported_only_where_a_lock_belongs(self):
		self.assertIn(
			f"has lost its {LOCK_DAYS}-day lock",
			bucket_problems("Control Plane", "dfw", "wnam", {"location": "WNAM"}, []),
		)
		self.assertEqual(bucket_problems("Site Media", "dfw", "enam", {"location": "ENAM"}, []), [])

	def test_an_unknown_cluster_skips_the_placement_check(self):
		self.assertEqual(bucket_problems("Site Media", "nowhere", None, {"location": "WEUR"}, []), [])


class TestHasLock(unittest.TestCase):
	def test_only_our_full_enabled_rule_counts(self):
		self.assertTrue(has_lock([LOCK]))
		self.assertFalse(has_lock([{**LOCK, "enabled": False}]))
		self.assertFalse(has_lock([{**LOCK, "prefix": "media/"}]))
		self.assertFalse(has_lock([{**LOCK, "condition": {"type": "Age", "maxAgeSeconds": 86400}}]))
		self.assertFalse(has_lock([{**LOCK, "id": "someone-elses"}]))
