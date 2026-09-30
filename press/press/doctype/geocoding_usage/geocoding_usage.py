# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class GeocodingUsage(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		autocomplete: DF.Int
		calls: DF.Int
		date: DF.Date | None
		details: DF.Int
		geocode: DF.Int
		refused: DF.Int
		site: DF.Link | None
	# end: auto-generated types

	pass
