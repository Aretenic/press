"""The administrator's password link, made on the school's site when they ask (Aretenic ADR 044c §5).

agora calls when the administrator clicks its invitation. The link is made on the site, passed
straight back and stored nowhere here; Administrator's own password never leaves Press.
"""

import hmac

import frappe

#: On the school's site, in aretenic.
SITE_METHOD = "aretenic.onboarding.administrator_password_link"


def make(onboarding: str, provisioning_token: str) -> str:
	if not frappe.db.exists("School Provisioning", onboarding):
		raise frappe.DoesNotExistError(f"No provisioning for {onboarding}.")
	doc = frappe.get_doc("School Provisioning", onboarding)
	if not hmac.compare_digest(doc.get_password("provisioning_token"), provisioning_token):
		raise frappe.PermissionError("Provisioning token does not match.")
	if doc.status != "Live" or not doc.site:
		frappe.throw(f"{onboarding} is not live yet.")
	try:
		return frappe.get_doc("Site", doc.site).get_connection_as_admin().post_api(SITE_METHOD)
	except Exception as e:
		raise frappe.ValidationError(f"{doc.site} did not make a link: {e}") from e
