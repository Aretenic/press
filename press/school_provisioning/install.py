"""The role agora's Press user holds, and nothing else (Aretenic ADR 044 §3).

Created here rather than in press/fixtures/role.json, which is upstream's and merged weekly.
"""

import frappe

PROVISIONER_ROLE = "School Provisioner"


def ensure_role():
	if not frappe.db.exists("Role", PROVISIONER_ROLE):
		frappe.get_doc({"doctype": "Role", "role_name": PROVISIONER_ROLE, "desk_access": 0}).insert(
			ignore_permissions=True
		)
