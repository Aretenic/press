# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class MessagingStatement(Document):
	pass


def on_doctype_update():
	frappe.db.add_unique("Messaging Statement", ("site", "period"))
