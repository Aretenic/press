"""The step machine (ADR 044 §4): order, waiting, failure stops it, Live at the end. No database."""

import json
import unittest
from unittest.mock import patch

import frappe

from press.school_provisioning import pipeline as P


class Row(frappe._dict):
	pass


class FakeProvisioning(frappe._dict):
	def __init__(self):
		super().__init__(
			doctype="School Provisioning",
			name="ONB-1",
			status="Running",
			spec=json.dumps({"infra": {"apps": ["frappe"]}}),
			steps=[
				Row(step=i, step_key=key, title=title, status="Pending", error=None)
				for i, (key, title) in enumerate(P.STEPS, 1)
			],
			saves=0,
		)

	def save(self, **kwargs):
		self.saves += 1


def run(doc, start, poll=None):
	posted, users = [], []
	with (
		patch.object(P.frappe, "set_user", users.append),
		patch.object(P.frappe, "get_doc", return_value=doc),
		patch.dict(P.START, start),
		patch.dict(P.POLL, poll or {}),
		patch.object(P, "post_status", lambda d, row, live: posted.append((row.step_key, row.status, live))),
		patch.object(P.frappe, "log_error"),
		patch.object(P, "now_datetime", return_value="now"),
	):
		P.advance(doc.name)
	assert users == ["Administrator"], users  # never as agora's provisioner user
	return posted


ALL_DONE = {key: (lambda d, s, r: "Success") for key, _ in P.STEPS}


class TestAdvance(unittest.TestCase):
	def test_steps_run_in_order_to_live(self):
		doc = FakeProvisioning()
		posted = run(doc, {**ALL_DONE, "after_wizard": lambda d, s, r: "Skipped"})
		self.assertEqual(doc.status, "Live")
		self.assertEqual([r.status for r in doc.steps].count("Success"), len(P.STEPS) - 1)
		self.assertEqual(posted[-1], ("backup", "Success", True))

	def test_a_running_step_waits_for_the_next_tick(self):
		doc = FakeProvisioning()
		run(doc, {**ALL_DONE, "site": lambda d, s, r: None})
		self.assertEqual(doc.status, "Running")
		self.assertEqual([r.status for r in doc.steps[:3]], ["Success", "Running", "Pending"])

		# Next tick: the site is polled, not started again.
		started_again = []
		run(doc, {**ALL_DONE, "site": lambda d, s, r: started_again.append(1)}, {**ALL_DONE})
		self.assertEqual(started_again, [])
		self.assertEqual(doc.status, "Live")

	def test_a_failure_stops_the_pipeline_and_is_reported(self):
		doc = FakeProvisioning()

		def broken(d, s, r):
			raise P.StepFailed("No active bench.")

		posted = run(doc, {**ALL_DONE, "site": broken})
		self.assertEqual(doc.status, "Failed")
		self.assertEqual(doc.steps[1].error, "No active bench.")
		self.assertEqual(doc.steps[2].status, "Pending")
		self.assertIn(("site", "Failure", False), posted)

	def test_an_unexpected_error_fails_the_step_too(self):
		doc = FakeProvisioning()
		run(doc, {**ALL_DONE, "wizard": lambda d, s, r: 1 / 0})
		self.assertEqual(doc.status, "Failed")
		self.assertIn("ZeroDivisionError", doc.steps[2].error)

	def test_a_failed_or_finished_provisioning_is_left_alone(self):
		for status in ("Failed", "Live"):
			doc = FakeProvisioning()
			doc.status = status
			run(doc, {key: (lambda d, s, r: self.fail("started")) for key, _ in P.STEPS})
			self.assertEqual(doc.status, status)
