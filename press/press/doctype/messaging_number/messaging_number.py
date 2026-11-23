# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MessagingNumber(Document):
	def on_update(self):
		from press.messaging import registration

		registration.on_number_update(self)
