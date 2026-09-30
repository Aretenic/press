# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class GeocodingToken(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		revoke_reason: DF.Data | None
		revoked_on: DF.Datetime | None
		site: DF.Link
		status: DF.Literal["Active", "Revoked"]
		token_hash: DF.Data | None
	# end: auto-generated types

	@frappe.whitelist()
	def rotate(self):
		"""Aretenic (ADR 053 §5): mint a new token for this site and push it; this one stops working."""
		frappe.only_for("System Manager")
		from press.geocoding.tokens import provision

		provision(frappe.get_doc("Site", self.site), reason="Rotated on demand")
