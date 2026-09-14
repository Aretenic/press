"""Minimal client for the Vultr API v2.

Vultr publishes no official Python SDK. Endpoints and field names follow the official Go
client (github.com/vultr/govultr) and were checked against the live API.
"""

from __future__ import annotations

import time
from typing import Any

import requests

BASE_URL = "https://api.vultr.com/v2"
RETRY_STATUSES = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 5
TIMEOUT = 30


class VultrAPIError(Exception):
	def __init__(self, status: int, message: str, method: str = "", path: str = ""):
		self.status = status
		self.message = message
		super().__init__(f"Vultr API {method} {path} returned {status}: {message}")

	@property
	def not_found(self) -> bool:
		return self.status == 404


class Client:
	def __init__(self, api_key: str):
		self.session = requests.Session()
		self.session.headers.update(
			{"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
		)

	def request(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
		for attempt in range(1, MAX_ATTEMPTS + 1):
			response = self.session.request(
				method, BASE_URL + path, json=body, params=params, timeout=TIMEOUT
			)
			if response.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
				time.sleep(min(2**attempt, 30))
				continue
			if response.status_code >= 400:
				try:
					message = response.json().get("error") or response.text
				except ValueError:
					message = response.text
				raise VultrAPIError(response.status_code, message[:500], method, path)
			return response.json() if response.content else {}
		raise VultrAPIError(response.status_code, "retries exhausted", method, path)

	def list(self, path: str, key: str, params: dict | None = None) -> list[dict]:
		"""Follow Vultr's cursor pagination and return every item under `key`."""
		params = {"per_page": 500, **(params or {})}
		items: list[dict] = []
		while True:
			data = self.request("GET", path, params=params)
			items.extend(data.get(key, []))
			cursor = data.get("meta", {}).get("links", {}).get("next")
			if not cursor:
				return items
			params = {**params, "cursor": cursor}

	# Account
	def get_account(self) -> dict:
		return self.request("GET", "/account")["account"]

	# SSH keys
	def list_ssh_keys(self) -> list[dict]:
		return self.list("/ssh-keys", "ssh_keys")

	def create_ssh_key(self, name: str, public_key: str) -> dict:
		return self.request("POST", "/ssh-keys", {"name": name, "ssh_key": public_key})["ssh_key"]

	# VPCs
	def create_vpc(self, region: str, description: str, subnet: str, mask: int) -> dict:
		return self.request(
			"POST",
			"/vpcs",
			{"region": region, "description": description, "v4_subnet": subnet, "v4_subnet_mask": mask},
		)["vpc"]

	def delete_vpc(self, vpc_id: str) -> None:
		self.request("DELETE", f"/vpcs/{vpc_id}")

	# Firewall groups
	def list_firewall_groups(self) -> list[dict]:
		return self.list("/firewalls", "firewall_groups")

	def create_firewall_group(self, description: str) -> dict:
		return self.request("POST", "/firewalls", {"description": description})["firewall_group"]

	def delete_firewall_group(self, group_id: str) -> None:
		self.request("DELETE", f"/firewalls/{group_id}")

	def list_firewall_rules(self, group_id: str) -> list[dict]:
		return self.list(f"/firewalls/{group_id}/rules", "firewall_rules")

	def create_firewall_rule(self, group_id: str, rule: dict) -> dict:
		return self.request("POST", f"/firewalls/{group_id}/rules", rule)["firewall_rule"]

	# Instances
	def create_instance(self, body: dict) -> dict:
		return self.request("POST", "/instances", body)["instance"]

	def get_instance(self, instance_id: str) -> dict:
		return self.request("GET", f"/instances/{instance_id}")["instance"]

	def list_instances(self, params: dict | None = None) -> list[dict]:
		return self.list("/instances", "instances", params)

	def update_instance(self, instance_id: str, body: dict) -> dict:
		return self.request("PATCH", f"/instances/{instance_id}", body).get("instance", {})

	def delete_instance(self, instance_id: str) -> None:
		self.request("DELETE", f"/instances/{instance_id}")

	def start_instance(self, instance_id: str) -> None:
		self.request("POST", f"/instances/{instance_id}/start")

	def halt_instance(self, instance_id: str) -> None:
		self.request("POST", f"/instances/{instance_id}/halt")

	def reboot_instance(self, instance_id: str) -> None:
		self.request("POST", f"/instances/{instance_id}/reboot")

	def list_instance_vpcs(self, instance_id: str) -> list[dict]:
		return self.list(f"/instances/{instance_id}/vpcs", "vpcs")

	def get_instance_upgrades(self, instance_id: str) -> list[str]:
		data = self.request("GET", f"/instances/{instance_id}/upgrades", params={"type": "plans"})
		return data.get("upgrades", {}).get("plans", [])

	# Snapshots
	def create_snapshot(self, instance_id: str, description: str) -> dict:
		return self.request("POST", "/snapshots", {"instance_id": instance_id, "description": description})[
			"snapshot"
		]

	def get_snapshot(self, snapshot_id: str) -> dict:
		return self.request("GET", f"/snapshots/{snapshot_id}")["snapshot"]

	def delete_snapshot(self, snapshot_id: str) -> None:
		self.request("DELETE", f"/snapshots/{snapshot_id}")

	# Catalogue
	def list_os(self) -> list[dict]:
		return self.list("/os", "os")

	def get_plan(self, plan_id: str) -> dict[str, Any] | None:
		return next((p for p in self.list("/plans", "plans") if p["id"] == plan_id), None)
