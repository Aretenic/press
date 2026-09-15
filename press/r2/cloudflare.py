"""Minimal client for the Cloudflare API endpoints R2 provisioning needs.

Behaviour was checked against the live API on 2026-09-15 (Aretenic runbook quirk 40):

- creating a bucket that exists returns 409 with error code 10004; a missing bucket is 404, 10006;
- ``GET /r2/buckets/<name>`` returns ``location`` in upper case (``WNAM``);
- an S3 key is a Cloudflare API token: access key id = token id, secret = sha256(token value);
- a token takes a few seconds to work against the S3 endpoint after it is minted.
"""

from __future__ import annotations

import hashlib
import time

import requests

BASE_URL = "https://api.cloudflare.com/client/v4"
RETRY_STATUSES = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 5
TIMEOUT = 30

BUCKET_EXISTS = 10004
BUCKET_NOT_FOUND = 10006
ITEM_WRITE_PERMISSION = "Workers R2 Storage Bucket Item Write"


class CloudflareAPIError(Exception):
	def __init__(self, status: int, errors: list, method: str = "", path: str = ""):
		self.status = status
		self.errors = errors or []
		self.codes = [e.get("code") for e in self.errors if isinstance(e, dict)]
		super().__init__(f"Cloudflare API {method} {path} returned {status}: {self.errors}")


class Client:
	def __init__(self, account_id: str, api_token: str):
		self.account_id = account_id
		self.session = requests.Session()
		self.session.headers.update(
			{"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
		)

	@property
	def s3_endpoint(self) -> str:
		return s3_endpoint(self.account_id)

	def request(self, method: str, path: str, body: dict | None = None, params: dict | None = None):
		url = f"{BASE_URL}/accounts/{self.account_id}{path}"
		for attempt in range(1, MAX_ATTEMPTS + 1):
			try:
				response = self.session.request(method, url, json=body, params=params, timeout=TIMEOUT)
			except (requests.Timeout, requests.ConnectionError):
				# Only reads are retried: a POST that timed out may still have created the resource.
				if method != "GET" or attempt == MAX_ATTEMPTS:
					raise
				time.sleep(min(2**attempt, 30))
				continue
			if response.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
				time.sleep(min(2**attempt, 30))
				continue
			try:
				data = response.json()
			except ValueError:
				data = {"errors": [{"message": response.text[:500]}]}
			if response.status_code >= 400 or not data.get("success", True):
				raise CloudflareAPIError(response.status_code, data.get("errors"), method, path)
			return data.get("result")
		raise CloudflareAPIError(response.status_code, [{"message": "retries exhausted"}], method, path)

	# Buckets

	def create_bucket(self, name: str, location_hint: str) -> bool:
		"""Create a bucket. Returns False if this account already owns one by that name."""
		try:
			self.request("POST", "/r2/buckets", {"name": name, "locationHint": location_hint})
		except CloudflareAPIError as e:
			if BUCKET_EXISTS in e.codes:
				return False
			raise
		return True

	def get_bucket(self, name: str) -> dict | None:
		try:
			return self.request("GET", f"/r2/buckets/{name}")
		except CloudflareAPIError as e:
			if BUCKET_NOT_FOUND in e.codes:
				return None
			raise

	def get_lock_rules(self, bucket: str) -> list[dict]:
		return (self.request("GET", f"/r2/buckets/{bucket}/lock") or {}).get("rules", [])

	def set_lock_rules(self, bucket: str, rules: list[dict]) -> None:
		self.request("PUT", f"/r2/buckets/{bucket}/lock", {"rules": rules})

	# S3 keys

	def permission_group_id(self, name: str) -> str:
		for group in self.request("GET", "/tokens/permission_groups"):
			if group["name"] == name:
				return group["id"]
		raise CloudflareAPIError(404, [{"message": f"permission group {name!r} not visible to the token"}])

	def create_s3_key(self, name: str, buckets: list[str] | None) -> tuple[str, str]:
		"""Mint an Object Read & Write key. `buckets=None` means every bucket in the account."""
		if buckets is None:
			resources = {f"com.cloudflare.api.account.{self.account_id}": "*"}
		else:
			resources = {f"com.cloudflare.edge.r2.bucket.{self.account_id}_default_{b}": "*" for b in buckets}
		token = self.request(
			"POST",
			"/tokens",
			{
				"name": name,
				"policies": [
					{
						"effect": "allow",
						"permission_groups": [{"id": self.permission_group_id(ITEM_WRITE_PERMISSION)}],
						"resources": resources,
					}
				],
			},
		)
		return token["id"], hashlib.sha256(token["value"].encode()).hexdigest()

	def find_tokens(self, name: str) -> list[dict]:
		"""Every token with this name. Two tokens per school outgrow one page at 50 schools."""
		found, page = [], 1
		while True:
			tokens = self.request("GET", "/tokens", params={"per_page": 50, "page": page}) or []
			found.extend(t for t in tokens if t.get("name") == name)
			if len(tokens) < 50:
				return found
			page += 1

	def delete_token(self, token_id: str) -> None:
		self.request("DELETE", f"/tokens/{token_id}")


def s3_endpoint(account_id: str) -> str:
	return f"https://{account_id}.r2.cloudflarestorage.com"
