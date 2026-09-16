"""Last-success timestamps for the R2 backup jobs (Aretenic ADR 043 §2).

The jobs only log failures; silence is not success. Each finished run stores its time as a global
default, and ``press.metrics`` exposes it so Prometheus can alert when a run is missing.
"""

from __future__ import annotations

import time

import frappe

PREFIX = "r2_last_success"


def record_success(job: str, site: str | None = None) -> None:
	frappe.db.set_default(_key(job, site), str(time.time()))


def last_successes() -> list[tuple[str, str, float]]:
	"""(job, site, unix time) for every recorded job; site is empty for jobs not run per site."""
	rows = frappe.get_all(
		"DefaultValue",
		filters={"parent": "__default", "defkey": ("like", f"{PREFIX}:%")},
		fields=["defkey", "defvalue"],
	)
	result = []
	for row in rows:
		_, job, site = [*row.defkey.split(":", 2), ""][:3]
		result.append((job, site, float(row.defvalue)))
	return result


def _key(job: str, site: str | None) -> str:
	return f"{PREFIX}:{job}:{site}" if site else f"{PREFIX}:{job}"
