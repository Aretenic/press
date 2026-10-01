# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

"""One school's provisioning from agora's spec (Aretenic ADR 044 §4); see press.school_provisioning."""

import frappe
from frappe.model.document import Document

from press.school_provisioning.pipeline import STEPS


class SchoolProvisioning(Document):
	def before_insert(self):
		self.steps = []
		for number, (key, title) in enumerate(STEPS, 1):
			self.append("steps", {"step": number, "step_key": key, "title": title, "status": "Pending"})

	@frappe.whitelist()
	def retry(self):
		"""Restart the failed step; every step is idempotent (ADR 044 §4: retries are per step)."""
		frappe.only_for("System Manager")
		if self.status != "Failed":
			frappe.throw("Only a failed provisioning can be retried.")
		for row in self.steps:
			if row.status == "Failure":
				row.status, row.error, row.finished = "Pending", None, None
		self.status = "Running"
		self.save()
		frappe.enqueue(
			"press.school_provisioning.pipeline.advance",
			name=self.name,
			queue="short",
			enqueue_after_commit=True,
		)
