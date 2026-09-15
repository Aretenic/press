"""Decrypting site_config backups (ADR 041 §5).

App servers encrypt a backup's site_config to Press Settings' age public key before upload and
store it as ``<file>.age``. Only this host can read it back, with the private key in a file:

- never in Press's database, so a control-plane backup does not carry it;
- with a copy kept by the operator, without which losing this host makes every backed-up
  ``encryption_key`` unrecoverable.

By hand, anywhere: ``age --decrypt --identity <key file> <file>.age``.
"""

from __future__ import annotations

import os
import subprocess

import frappe

DEFAULT_IDENTITY_PATH = "~/.config/aretenic/backup-config-age.key"
SUFFIX = ".age"


def identity_path() -> str:
	return os.path.expanduser(frappe.conf.get("backup_config_age_identity") or DEFAULT_IDENTITY_PATH)


def is_encrypted(file_path: str | None) -> bool:
	return bool(file_path) and file_path.endswith(SUFFIX)


def decrypt(data: bytes) -> bytes:
	path = identity_path()
	if not os.path.exists(path):
		frappe.throw(f"This backup's site config is encrypted, and the age identity {path} is missing")
	result = subprocess.run(
		["age", "--decrypt", "--identity", path], input=data, capture_output=True, check=False
	)
	if result.returncode:
		frappe.throw(
			f"Could not decrypt the site config backup: {result.stderr.decode(errors='replace')[:300]}"
		)
	return result.stdout
