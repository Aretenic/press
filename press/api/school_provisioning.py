"""The Press end of agora's onboarding bridge (Aretenic ADR 044 §3, ADR 045b §1).

aretenic.com posts the spec here as a user holding only the School Provisioner role. The
onboarding name plus its provisioning token is the idempotency key: a re-push returns the same
provisioning and never creates a second site.
"""

import hmac
import json

import frappe

from press.school_provisioning import spec as S
from press.school_provisioning.install import PROVISIONER_ROLE


@frappe.whitelist(methods=["POST"])
def accept_spec(spec):
	frappe.only_for(PROVISIONER_ROLE)
	spec = json.loads(spec) if isinstance(spec, str) else spec
	if errors := S.spec_errors(spec):
		frappe.throw("<br>".join(errors), title="Spec refused")

	if existing := frappe.db.exists("School Provisioning", spec["onboarding"]):
		doc = frappe.get_doc("School Provisioning", existing)
		if not hmac.compare_digest(doc.get_password("provisioning_token"), spec["provisioning_token"]):
			raise frappe.PermissionError("This onboarding was already sent with another token.")
		return {"provisioning": doc.name, "status": doc.status}

	if errors := record_errors(spec):
		frappe.throw("<br>".join(errors), title="Spec refused")

	infra = spec["infra"]
	doc = frappe.get_doc(
		{
			"doctype": "School Provisioning",
			"onboarding": spec["onboarding"],
			"provisioning_token": spec["provisioning_token"],
			"subdomain": infra["subdomain"],
			"cluster": infra["cluster"],
			"release_group": infra["release_group"],
			"plan": infra["plan"],
			"spec": json.dumps(spec, indent=1),
		}
	).insert(ignore_permissions=True)
	frappe.enqueue(
		"press.school_provisioning.pipeline.advance",
		name=doc.name,
		queue="short",
		enqueue_after_commit=True,
	)
	return {"provisioning": doc.name, "status": doc.status}


def record_errors(spec):
	"""What Press's own records say against the spec, checked before anything is created."""
	infra = spec["infra"]
	errors = []
	if not frappe.db.exists("Cluster", infra["cluster"]):
		errors.append(f"Cluster {infra['cluster']} does not exist.")
	if infra["release_group"] not in open_groups():
		errors.append(f"Release group {infra['release_group']} is not open to schools.")
	else:
		offered = set(frappe.get_all("Release Group App", {"parent": infra["release_group"]}, pluck="app"))
		errors += [
			f"{app} is not in {infra['release_group']}." for app in infra["apps"] if app not in offered
		]
	if not frappe.db.exists("Site Plan", infra["plan"]):
		errors.append(f"Plan {infra['plan']} does not exist.")
	domain = frappe.db.get_single_value("Press Settings", "domain")
	if frappe.db.exists("Site", {"name": f"{infra['subdomain']}.{domain}", "status": ("!=", "Archived")}):
		errors.append(f"{infra['subdomain']}.{domain} already exists.")
	if frappe.db.exists("School Provisioning", {"subdomain": infra["subdomain"], "status": ("!=", "Failed")}):
		errors.append(f"{infra['subdomain']} is already being provisioned.")
	return errors


def open_groups():
	return frappe.get_all(
		"School Provisioning Group", {"parent": "School Provisioning Settings"}, pluck="release_group"
	)


@frappe.whitelist(methods=["GET"])
def release_groups():
	"""The groups agora may choose from, with their apps, so it can mirror them."""
	frappe.only_for(PROVISIONER_ROLE)
	return [
		{
			"group": group,
			"title": frappe.db.get_value("Release Group", group, "title"),
			"apps": frappe.get_all("Release Group App", {"parent": group}, pluck="app", order_by="idx"),
		}
		for group in open_groups()
	]
