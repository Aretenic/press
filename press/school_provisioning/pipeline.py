"""From an accepted spec to a live school site (Aretenic ADR 044 §4, ADR 045b §1).

Each step is started once and then polled until it succeeds or fails; `advance` moves a provisioning
along and `advance_running` does so for all of them from the scheduler. A failure stops the
pipeline and nothing is rolled back; `School Provisioning.retry` restarts the failed step. Every
step is idempotent, so a retry, or a crash between "started" and "recorded", converges.

ADR 044's step 1 (accept the spec) is `accept_spec` itself; step 2 (buckets and keys) runs inside
`Site.before_insert`, so here it is part of creating the site.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import frappe
import requests
from frappe.utils import now_datetime, today

from press.school_provisioning import spec as S

if TYPE_CHECKING:
	from press.press.doctype.school_provisioning.school_provisioning import SchoolProvisioning
	from press.press.doctype.site.site import Site

#: The one entrypoint on the school's site (ADR 044 §5).
APPLY_METHOD = "aretenic.onboarding.apply_onboarding_spec"
AGORA_STATUS_METHOD = "agora.onboarding.receive_status"
FAILED_JOB = ("Failure", "Delivery Failure")

STEPS = (
	("team", "Create the school's team"),
	("site", "Create the site and its buckets"),
	("wizard", "Complete the Setup Wizard"),
	("after_wizard", "Install apps that need the wizard"),
	("apply", "Apply the site section"),
	("backup", "Take the first offsite backup"),
)


class StepFailed(Exception):
	pass


# --- Driving ---------------------------------------------------------------------------------


def advance_running():
	for name in frappe.get_all("School Provisioning", {"status": "Running"}, pluck="name"):
		frappe.enqueue(
			"press.school_provisioning.pipeline.advance",
			name=name,
			queue="short",
			job_id=f"school_provisioning:{name}",
			deduplicate=True,
		)


def advance(name: str):  # noqa: C901
	doc: SchoolProvisioning = frappe.get_doc("School Provisioning", name, for_update=True)
	if doc.status != "Running":
		return
	spec = json.loads(doc.spec)
	for row in doc.steps:
		if row.status in ("Success", "Skipped"):
			continue
		if row.status == "Failure":
			_set_status(doc, "Failed")
			return
		try:
			if row.status == "Pending":
				row.status, row.started = "Running", now_datetime()
				outcome = START[row.step_key](doc, spec, row)
			else:
				outcome = POLL[row.step_key](doc, spec, row)
		except StepFailed as e:
			_finish(doc, row, "Failure", str(e))
			_set_status(doc, "Failed")
			return
		except Exception as e:
			frappe.log_error(
				"School Provisioning Step Failed", reference_doctype=doc.doctype, reference_name=doc.name
			)
			_finish(doc, row, "Failure", f"{type(e).__name__}: {e}")
			_set_status(doc, "Failed")
			return

		if outcome in ("Success", "Skipped"):
			_finish(doc, row, outcome)
			continue
		doc.save(ignore_permissions=True)
		return  # still running; polled again next tick

	_set_status(doc, "Live")


def _finish(doc, row, status, error=None):
	row.status, row.finished, row.error = status, now_datetime(), error
	doc.save(ignore_permissions=True)
	post_status(doc, row, live=False)


def _set_status(doc, status):
	doc.status = status
	doc.save(ignore_permissions=True)
	if status == "Live":
		post_status(doc, doc.steps[-1], live=True)


# --- Steps -----------------------------------------------------------------------------------


def start_team(doc, spec, row):
	if doc.team:
		return "Success"
	settings = frappe.get_single("School Provisioning Settings")
	if not settings.team_user_domain:
		raise StepFailed("School Provisioning Settings has no team user domain.")
	email = f"{spec['infra']['subdomain']}@{settings.team_user_domain}"

	team = frappe.db.get_value("Team", {"user": email})
	if not team:
		if not frappe.db.exists("User", email):
			# No password, no roles, no welcome email: the school never uses Press (ADR 045b §1).
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": spec["site"]["company"][:140],
					"user_type": "Website User",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
		team = (
			frappe.get_doc(
				{
					"doctype": "Team",
					"user": email,
					"team_title": spec["site"]["company"],
					"country": spec["site"]["country"],
					"enabled": 1,
					# School plans cost $0 in Press; billing is on aretenic.com (ADR 045b §2).
					"free_account": 1,
					"team_members": [{"user": email}],
				}
			)
			.insert(ignore_permissions=True, ignore_links=True)
			.name
		)
	doc.team = team
	return "Success"


def start_site(doc, spec, row):
	if doc.site:
		return poll_site(doc, spec, row)
	infra = spec["infra"]
	bench = frappe.get_all(
		"Bench",
		{"group": infra["release_group"], "cluster": infra["cluster"], "status": "Active"},
		["name", "server"],
		order_by="creation desc",
		limit=1,
	)
	if not bench:
		raise StepFailed(f"No active bench of {infra['release_group']} in {infra['cluster']}.")
	site = frappe.get_doc(
		{
			"doctype": "Site",
			"subdomain": infra["subdomain"],
			"domain": frappe.db.get_single_value("Press Settings", "domain"),
			"server": bench[0].server,
			"bench": bench[0].name,
			"group": infra["release_group"],
			"cluster": infra["cluster"],
			"team": doc.team,
			"plan": infra["plan"],
			"apps": [{"app": app} for app in S.new_site_apps(spec)],
			# Usage is billed on aretenic.com, never suspended (ADR 045b §4).
			"disable_site_usage_exceed_check": 1,
		}
	).insert(ignore_permissions=True)
	doc.site = site.name
	return None


def poll_site(doc, spec, row):
	status = frappe.db.get_value("Site", doc.site, "status")
	if status == "Active":
		return "Success"
	if status == "Broken":
		raise StepFailed(f"Site {doc.site} is Broken; see its agent jobs.")
	return None


def start_wizard(doc, spec, row):
	from press.agent import Agent

	site: Site = frappe.get_doc("Site", doc.site)
	done = frappe.db.exists(
		"Agent Job", {"site": site.name, "job_type": "Complete Setup Wizard", "status": "Success"}
	)
	if done:
		return "Success"
	language = spec["site"]["language"]
	language_name = frappe.db.get_value("Language", language, "language_name") or language
	data = S.setup_wizard_data(spec, language_name, frappe.utils.getdate(today()))
	row.agent_job = Agent(site.server).complete_setup_wizard(site, data).name
	return None


def poll_job(doc, spec, row):
	status = frappe.db.get_value("Agent Job", row.agent_job, "status")
	if status == "Success":
		return "Success"
	if status in FAILED_JOB:
		raise StepFailed(f"Agent job {row.agent_job} ended in {status}.")
	return None


def start_after_wizard(doc, spec, row):
	apps = S.after_wizard_apps(spec)
	if not apps:
		return "Skipped"
	return poll_after_wizard(doc, spec, row)


def poll_after_wizard(doc, spec, row):
	"""Install the apps one at a time; Press refuses a second app while the site is Pending."""
	site: Site = frappe.get_doc("Site", doc.site)
	if site.status == "Broken":
		raise StepFailed(f"Site {site.name} is Broken after installing {row.agent_job or 'an app'}.")
	if site.status != "Active":
		return None
	if row.agent_job and frappe.db.get_value("Agent Job", row.agent_job, "status") in FAILED_JOB:
		raise StepFailed(f"Agent job {row.agent_job} failed.")
	installed = {a.app for a in site.apps}
	missing = [app for app in S.after_wizard_apps(spec) if app not in installed]
	if not missing:
		return "Success"
	row.agent_job = site.install_app(missing[0])
	return None


def start_apply(doc, spec, row):
	"""One call to the site; everything we learn about go-live is an app change there (ADR 044 §5)."""
	site: Site = frappe.get_doc("Site", doc.site)
	try:
		site.get_connection_as_admin().post_api(APPLY_METHOD, {"spec": json.dumps(spec)})
	except Exception as e:
		raise StepFailed(f"{APPLY_METHOD} failed: {e}") from e
	return "Success"


def start_backup(doc, spec, row):
	site: Site = frappe.get_doc("Site", doc.site)
	backup = site.backup(with_files=True, offsite=True)
	# Site Backup writes its job with db.set_value during insert, not onto the document
	row.agent_job = frappe.db.get_value("Site Backup", backup.name, "job")
	if not row.agent_job:
		raise StepFailed(f"Backup {backup.name} did not start an agent job.")


START = {
	"team": start_team,
	"site": start_site,
	"wizard": start_wizard,
	"after_wizard": start_after_wizard,
	"apply": start_apply,
	"backup": start_backup,
}
POLL = {
	"team": start_team,
	"site": poll_site,
	"wizard": poll_job,
	"after_wizard": poll_after_wizard,
	"apply": start_apply,
	"backup": poll_job,
}


# --- Status back to agora --------------------------------------------------------------------


def post_status(doc, row, live: bool):
	"""Post one step transition to the onboarding record (ADR 044 §3: status flows back one way)."""
	settings = frappe.get_single("School Provisioning Settings")
	secret = settings.get_password("agora_api_secret", raise_exception=False)
	if not (settings.agora_url and settings.agora_api_key and secret):
		return
	try:
		response = requests.post(
			f"{settings.agora_url.rstrip('/')}/api/method/{AGORA_STATUS_METHOD}",
			data={
				"onboarding": doc.onboarding,
				"provisioning_token": doc.get_password("provisioning_token"),
				"step": f"{row.step}. {row.title}",
				"state": row.status,
				"message": row.error or "",
				"site": doc.site or "",
				"team": doc.team or "",
				"live": int(live),
			},
			headers={"Authorization": f"token {settings.agora_api_key}:{secret}"},
			timeout=20,
		)
		response.raise_for_status()
	except Exception:
		# Provisioning goes on; the record in Press stays the source of truth for its state.
		frappe.log_error(
			"School Provisioning Status Not Delivered", reference_doctype=doc.doctype, reference_name=doc.name
		)
