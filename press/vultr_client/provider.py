"""Vultr support for Cluster, Virtual Machine and Virtual Machine Image.

Kept in one module so the shared doctype files only carry one-line dispatches, which keeps
upstream merges small. Behaviour was measured against the live API (Aretenic ADR 039):

- a VPC is attached at creation and Vultr assigns the private address itself;
- an instance has exactly one firewall group;
- the stock Ubuntu image ships UFW enabled, so plain-image instances get cloud-init to disable it;
- snapshots are account-wide, take minutes, and restore onto any plan with an equal or larger disk;
- resizes are limited to the plans `/instances/{id}/upgrades` lists (same product line, larger).
"""

from __future__ import annotations

import base64
import ipaddress
import math
import typing

import frappe

from press.vultr_client.client import Client, VultrAPIError

if typing.TYPE_CHECKING:
	from press.press.doctype.cluster.cluster import Cluster
	from press.press.doctype.virtual_machine.virtual_machine import VirtualMachine
	from press.press.doctype.virtual_machine_image.virtual_machine_image import VirtualMachineImage

VULTR_ROOT_DISK_ID = "vultr-root-disk"
VULTR_ROOT_DEVICE = "/dev/vda"
UBUNTU_IMAGE_NAME = "Ubuntu 22.04 LTS x64"  # Press's Ansible pins 22.04
DEFAULT_INSTANCE_TYPE = "vhp-2c-4gb-amd"

# Vultr's Ubuntu image ships UFW enabled and a `linuxuser` account holding uid 1000, which
# Press's `user` role needs for `frappe`
PLAIN_IMAGE_CLOUD_INIT = """#cloud-config
runcmd:
- ufw disable
- userdel -r linuxuser || true
"""


def get_client(cluster: Cluster | str) -> Client:
	if isinstance(cluster, str):
		cluster = frappe.get_cached_doc("Cluster", cluster)
	return Client(cluster.get_password("vultr_api_token"))


# Cluster


def validate_api_token(cluster: Cluster) -> None:
	try:
		get_client(cluster).get_account()
	except VultrAPIError as e:
		if e.status in (401, 403):
			frappe.throw(
				"This Vultr API key is invalid, or this Press host's IP address is not in the key's "
				"access control list. Vultr checks the IPv6 address first when the host has one."
			)
		raise


def provision_cluster(cluster: Cluster) -> None:
	client = get_client(cluster)
	network = ipaddress.ip_network(cluster.cidr_block)
	try:
		vpc = client.create_vpc(
			cluster.region,
			f"Press {cluster.name}",
			str(network.network_address),
			network.prefixlen,
		)
		cluster.vpc_id = vpc["id"]
		cluster.save()
	except VultrAPIError as e:
		frappe.throw(f"Failed to provision VPC on Vultr: {e.message}")

	ensure_ssh_key(client, cluster.ssh_key)

	try:
		cluster.security_group_id = _create_firewall_group(client, cluster, "Servers", proxy=False)
		cluster.proxy_security_group_id = _create_firewall_group(client, cluster, "Proxy", proxy=True)
		cluster.save()
	except VultrAPIError as e:
		frappe.throw(f"Failed to provision firewall groups on Vultr: {e.message}")


def ensure_ssh_key(client: Client, ssh_key: str) -> str:
	"""Return the Vultr id of `ssh_key`, uploading it if Vultr has no key with the same public key."""
	public_key = frappe.db.get_value("SSH Key", ssh_key, "public_key").strip()
	fingerprint = " ".join(public_key.split()[:2])
	for key in client.list_ssh_keys():
		if key["name"] == ssh_key or " ".join(key["ssh_key"].split()[:2]) == fingerprint:
			return key["id"]
	return client.create_ssh_key(ssh_key, public_key)["id"]


def _create_firewall_group(client: Client, cluster: Cluster, role: str, proxy: bool) -> str:
	group = client.create_firewall_group(f"Press {cluster.name} - {role}")
	for rule in _firewall_rules(cluster, proxy=proxy):
		client.create_firewall_rule(group["id"], rule)
	return group["id"]


def _sources(value: str | None) -> list[str]:
	return [ip.strip() for ip in (value or "").splitlines() if ip.strip()]


def _firewall_rules(cluster: Cluster, proxy: bool) -> list[dict]:
	"""Inbound IPv4 rules for one role. Servers run with IPv6 disabled, so there are no IPv6 rules and
	Vultr drops IPv6 traffic.

	- Proxy: 80 and 443 from anywhere (sites, and the agent behind nginx on 443).
	- Servers (app, database): no public web ports. School traffic reaches them through the proxy on
	their private IPs; publicly they answer only the agent on 443, from `vultr_agent_allowed_ips`.
	- Both: SSH from `vultr_ssh_allowed_ips`, and ICMP.

	An empty address list falls back to anywhere, matching upstream Press's other providers.
	Production clusters set both lists (ADR 042)."""

	def rule(protocol, subnet, port=None, notes=""):
		network = ipaddress.ip_network(subnet, strict=False)
		r = {
			"ip_type": "v4",
			"protocol": protocol,
			"subnet": str(network.network_address),
			"subnet_size": network.prefixlen,
			"notes": notes,
		}
		if port:
			r["port"] = port
		return r

	anywhere = ["0.0.0.0/0"]
	ssh_sources = _sources(cluster.vultr_ssh_allowed_ips) or anywhere
	agent_sources = _sources(cluster.vultr_agent_allowed_ips) or anywhere

	rules = [rule("icmp", "0.0.0.0/0", notes="ICMP")]
	if proxy:
		rules += [
			rule("tcp", "0.0.0.0/0", "80", "HTTP"),
			rule("tcp", "0.0.0.0/0", "443", "HTTPS and agent"),
		]
	else:
		rules += [rule("tcp", source, "443", "Agent from Press") for source in agent_sources]
	rules += [rule("tcp", source, "22", "SSH") for source in ssh_sources]

	# Documentation only: Vultr firewall groups do not filter traffic inside the VPC.
	private = cluster.subnet_cidr_block or cluster.cidr_block
	rules += [
		rule("tcp", private, "3306", "MariaDB from private network"),
		rule("tcp", private, "2049", "NFS from private network"),
		rule("tcp", private, "11000:20000", "Redis from private network"),
		rule("tcp", private, "22000:22999", "SSH from private network"),
	]
	return rules


def sync_firewall_rules(cluster: Cluster) -> None:
	"""Bring the cluster's existing firewall groups in line with `_firewall_rules`, so a change to
	`vultr_ssh_allowed_ips` or `vultr_agent_allowed_ips` reaches running servers. Missing rules are
	added before stale ones are removed, so access is never narrower than both old and new sets."""
	client = get_client(cluster)

	def key(rule: dict) -> tuple:
		return (
			rule["ip_type"],
			rule["protocol"],
			str(rule.get("port") or ""),
			rule["subnet"],
			int(rule["subnet_size"]),
		)

	for group_id, proxy in ((cluster.security_group_id, False), (cluster.proxy_security_group_id, True)):
		if not group_id:
			continue
		wanted = {key(rule): rule for rule in _firewall_rules(cluster, proxy=proxy)}
		existing = client.list_firewall_rules(group_id)
		present = {key(rule) for rule in existing}
		for rule_key, rule in wanted.items():
			if rule_key not in present:
				client.create_firewall_rule(group_id, rule)
		for rule in existing:
			if key(rule) not in wanted:
				client.delete_firewall_rule(group_id, rule["id"])


def delete_firewall_group(cluster: Cluster, group_id: str) -> None:
	try:
		get_client(cluster).delete_firewall_group(group_id)
	except VultrAPIError as e:
		if not e.not_found:
			raise


# Virtual Machine


def provision(vm: VirtualMachine) -> None:
	if not vm.machine_image:
		frappe.throw("A machine image is required to provision a Vultr virtual machine.")

	cluster: Cluster = frappe.get_doc("Cluster", vm.cluster)
	client = get_client(cluster)

	body = {
		"region": cluster.region,
		"plan": vm.machine_type,
		"label": vm.name,
		"hostname": vm.name.split(".")[0],
		"sshkey_id": [ensure_ssh_key(client, vm.ssh_key)],
		"attach_vpc": [cluster.vpc_id],
		"firewall_group_id": _firewall_group_for(vm),
		"enable_ipv6": False,
		"disable_public_ipv4": not vm.assign_public_ip,
		"backups": "disabled",
		"activation_email": False,
		"tags": ["press", f"cluster-{frappe.scrub(cluster.name)}", f"series-{vm.series}"],
	}
	if vm.virtual_machine_image:
		body["snapshot_id"] = vm.machine_image
		user_data = vm.get_cloud_init()
	else:
		body["os_id"] = int(vm.machine_image)
		user_data = PLAIN_IMAGE_CLOUD_INIT
	body["user_data"] = base64.b64encode(user_data.encode()).decode()

	instance = client.create_instance(body)
	vm.instance_id = instance["id"]
	vm.private_ip_address = instance.get("internal_ip") or ""
	vm.status = get_status(instance)
	vm.save()
	frappe.db.commit()


def _firewall_group_for(vm: VirtualMachine) -> str:
	# A Vultr instance has a single firewall group; proxies use the proxy group
	if vm.series == "n":
		return frappe.db.get_value("Cluster", vm.cluster, "proxy_security_group_id")
	return vm.security_group_id


def get_status(instance: dict) -> str:
	if instance["status"] == "pending":
		return "Pending"
	if instance["status"] != "active":  # suspended, resizing
		return "Pending"
	if instance["server_status"] in ("locked", "installingbooting", "none"):
		return "Pending"  # a new instance reports power_status "stopped" while it installs
	if instance["power_status"] == "stopped":
		return "Stopped"
	if instance["server_status"] == "ok":
		return "Running"
	return "Pending"


def get_latest_ubuntu_image(vm: VirtualMachine) -> str:
	image = next((o for o in get_client(vm.cluster).list_os() if o["name"] == UBUNTU_IMAGE_NAME), None)
	if not image:
		frappe.throw(f"Vultr no longer offers {UBUNTU_IMAGE_NAME}.")
	return str(image["id"])


def sync(vm: VirtualMachine) -> None:
	try:
		instance = get_client(vm.cluster).get_instance(vm.instance_id)
	except VultrAPIError as e:
		if not e.not_found:
			raise
		vm.status = "Terminated"
		vm.save()
		vm.update_servers()
		return

	vm.status = get_status(instance)
	vm.machine_type = instance["plan"]
	vm.vcpu = instance["vcpu_count"]
	vm.ram = instance["ram"]
	vm.public_ip_address = "" if instance["main_ip"] in ("", "0.0.0.0") else instance["main_ip"]
	vm.private_ip_address = instance.get("internal_ip") or ""
	vm.root_disk_size = instance["disk"]
	vm.disk_size = instance["disk"]
	vm._vultr_instance = instance
	vm._update_volume_info_after_sync()
	vm.save()
	vm.update_servers()


def get_volumes(vm: VirtualMachine) -> list:
	# Block storage is not supported yet (ADR 039); the root disk is the only volume
	instance = getattr(vm, "_vultr_instance", None) or get_client(vm.cluster).get_instance(vm.instance_id)
	return [frappe._dict({"id": VULTR_ROOT_DISK_ID, "device": VULTR_ROOT_DEVICE, "size": instance["disk"]})]


def start(vm: VirtualMachine) -> None:
	get_client(vm.cluster).start_instance(vm.instance_id)


def stop(vm: VirtualMachine) -> None:
	get_client(vm.cluster).halt_instance(vm.instance_id)


def reboot(vm: VirtualMachine) -> None:
	get_client(vm.cluster).reboot_instance(vm.instance_id)


def terminate(vm: VirtualMachine) -> None:
	try:
		get_client(vm.cluster).delete_instance(vm.instance_id)
	except VultrAPIError as e:
		if not e.not_found:
			raise


def resize(vm: VirtualMachine, machine_type: str) -> None:
	client = get_client(vm.cluster)
	allowed = client.get_instance_upgrades(vm.instance_id)
	if machine_type not in allowed:
		frappe.throw(
			f"Vultr can't resize this server to {machine_type}. It can only move to: {', '.join(allowed) or 'none'}. "
			"A different product line needs a new server and a site migration."
		)
	# Vultr restarts the instance to apply the new plan; the disk grows with it
	client.update_instance(vm.instance_id, {"plan": machine_type})


def unsupported_volume_operation() -> None:
	frappe.throw("Vultr block storage volumes are not supported yet. The root disk grows with a plan resize.")


def bulk_sync() -> None:
	for cluster in frappe.get_all("Cluster", {"cloud_provider": "Vultr", "status": "Active"}, pluck="name"):
		machines = frappe.get_all(
			"Virtual Machine",
			{"cluster": cluster, "status": ("not in", ("Terminated", "Draft"))},
			pluck="name",
		)
		for machine in machines:
			frappe.enqueue_doc("Virtual Machine", machine, "sync", queue="sync", job_id=f"sync_vm_{machine}")


# Virtual Machine Image


def create_image(image: VirtualMachineImage) -> None:
	snapshot = get_client(image.cluster).create_snapshot(
		image.instance_id, f"Press VMI {image.name} - {image.virtual_machine}"
	)
	image.image_id = snapshot["id"]
	image.snapshot_id = snapshot["id"]


def sync_image(image: VirtualMachineImage) -> None:
	try:
		snapshot = get_client(image.cluster).get_snapshot(image.image_id)
	except VultrAPIError as e:
		if not e.not_found:
			raise
		image.status = "Unavailable"
		return
	image.status = {"pending": "Pending", "complete": "Available"}.get(snapshot["status"], "Unavailable")
	if snapshot.get("size"):
		image.size = math.ceil(snapshot["size"] / 1024**3)
		image.root_size = image.size


def delete_image(image: VirtualMachineImage) -> None:
	try:
		get_client(image.cluster).delete_snapshot(image.image_id)
	except VultrAPIError as e:
		if not e.not_found:
			raise


# Untracked Servers report


def get_untracked_instances(cluster: Cluster, known_instance_ids: set) -> list[dict]:
	return [
		{
			"instance_id": instance["id"],
			"name": instance["label"],
			"status": instance["status"],
			"instance_type": instance["plan"],
		}
		for instance in get_client(cluster).list_instances()
		if instance["id"] not in known_instance_ids and instance["region"] == cluster.region
	]
