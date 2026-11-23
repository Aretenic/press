"""Amazon SES, one tenant per school (Aretenic ADR 094 §7).

Each school gets, inside our one AWS account: a sending domain `<slug>.mail.<root>`, a notices
and a promotional configuration set (both sending their events to our SNS topic), an SES tenant
holding all three, and an IAM user that may send only from that domain. SES judges the tenant's
reputation on its own, so one school's bounces cannot stop the others; `reputation` pauses a
school well before SES would.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import boto3
import frappe

from press import messaging as m

EVENT_TYPES = ["SEND", "REJECT", "BOUNCE", "COMPLAINT", "DELIVERY"]


def _client(service: str):
	s = m.settings()
	# `service` is one of "sesv2", "iam", "route53": the typed stubs want a literal.
	return boto3.client(  # type: ignore[call-overload]
		service,
		region_name=s.aws_region,
		aws_access_key_id=s.aws_access_key_id,
		aws_secret_access_key=m.secret("aws_secret_access_key"),
	)


def names(slug: str) -> dict:
	root = m.settings().sending_root_domain
	return {
		"tenant": slug,
		"identity": f"{slug}.mail.{root}",
		"notices": f"{slug}-notices",
		"promotional": f"{slug}-promotional",
		"iam_user": f"ses-{slug}",
	}


def provision(account, slug: str) -> dict:
	"""Create the school's SES resources and an SMTP login. Returns the site config to push."""
	n = names(slug)
	s = m.settings()
	ses, iam = _client("sesv2"), _client("iam")
	account_id = _client("sts").get_caller_identity()["Account"]
	arn = f"arn:aws:ses:{s.aws_region}:{account_id}"

	for config_set in (n["notices"], n["promotional"]):
		ses.create_configuration_set(ConfigurationSetName=config_set, SendingOptions={"SendingEnabled": True})
		ses.create_configuration_set_event_destination(
			ConfigurationSetName=config_set,
			EventDestinationName="press",
			EventDestination={
				"Enabled": True,
				"MatchingEventTypes": EVENT_TYPES,
				"SnsDestination": {"TopicArn": s.ses_sns_topic_arn},
			},
		)
	identity = ses.create_email_identity(EmailIdentity=n["identity"], ConfigurationSetName=n["notices"])
	records = dkim_records(n["identity"], identity.get("DkimAttributes", {}).get("Tokens") or [])
	write_dns(records)

	ses.create_tenant(TenantName=n["tenant"])
	for resource in (
		f"{arn}:identity/{n['identity']}",
		f"{arn}:configuration-set/{n['notices']}",
		f"{arn}:configuration-set/{n['promotional']}",
	):
		ses.create_tenant_resource_association(TenantName=n["tenant"], ResourceArn=resource)

	iam.create_user(UserName=n["iam_user"], Tags=[{"Key": "site", "Value": account.site}])
	iam.put_user_policy(
		UserName=n["iam_user"],
		PolicyName="send-as-school",
		PolicyDocument=json.dumps(
			{
				"Version": "2012-10-17",
				"Statement": [
					{
						"Effect": "Allow",
						"Action": ["ses:SendRawEmail", "ses:SendEmail"],
						"Resource": [
							f"{arn}:identity/{n['identity']}",
							f"{arn}:configuration-set/{n['notices']}",
							f"{arn}:configuration-set/{n['promotional']}",
						],
					}
				],
			}
		),
	)
	key = iam.create_access_key(UserName=n["iam_user"])["AccessKey"]

	account.update(
		{
			"ses_tenant": n["tenant"],
			"ses_identity": n["identity"],
			"ses_configuration_set": n["notices"],
			"ses_promotional_configuration_set": n["promotional"],
			"iam_user": n["iam_user"],
			"dkim_records": json.dumps(records, indent=1),
			"email_daily_limit": s.new_school_daily_emails or 2000,
		}
	)
	school = frappe.db.get_value("Site", account.site, "host_name") or account.site
	return {
		"mail_server": f"email-smtp.{s.aws_region}.amazonaws.com",
		"mail_port": 587,
		"use_tls": 1,
		"mail_login": key["AccessKeyId"],
		"mail_password": smtp_password(key["SecretAccessKey"], s.aws_region),
		"auto_email_id": f"notices@{n['identity']}",
		"always_use_account_email_id_as_sender": 1,
		"email_sender_name": school,
		"ses_tenant": n["tenant"],
		"ses_configuration_set": n["notices"],
		"ses_promotional_configuration_set": n["promotional"],
	}


def dkim_records(domain: str, tokens: list[str]) -> list[dict]:
	return [
		{"name": f"{token}._domainkey.{domain}", "type": "CNAME", "value": f"{token}.dkim.amazonses.com"}
		for token in tokens
	]


def write_dns(records: list[dict]) -> bool:
	"""Upsert the DKIM records when the root domain is on Route 53; otherwise they wait on the
	account for us to add."""
	root = m.settings().sending_root_domain
	if not records or not root:
		return False
	domain = frappe.get_doc("Root Domain", root)
	if domain.generic_dns_provider:
		return False
	domain.boto3_client.change_resource_record_sets(
		HostedZoneId=domain.hosted_zone,
		ChangeBatch={
			"Changes": [
				{
					"Action": "UPSERT",
					"ResourceRecordSet": {
						"Name": r["name"],
						"Type": r["type"],
						"TTL": 3600,
						"ResourceRecords": [{"Value": r["value"]}],
					},
				}
				for r in records
			]
		},
	)
	return True


def set_sending(account, enabled: bool) -> None:
	"""Pause or resume both of the school's configuration sets."""
	ses = _client("sesv2")
	for config_set in (account.ses_configuration_set, account.ses_promotional_configuration_set):
		if config_set:
			ses.put_configuration_set_sending_options(ConfigurationSetName=config_set, SendingEnabled=enabled)


def revoke_login(account) -> None:
	if not account.iam_user:
		return
	iam = _client("iam")
	for key in iam.list_access_keys(UserName=account.iam_user).get("AccessKeyMetadata") or []:
		iam.delete_access_key(UserName=account.iam_user, AccessKeyId=key["AccessKeyId"])


def smtp_password(secret_access_key: str, region: str) -> str:
	"""AWS's documented derivation of an SES SMTP password from an IAM secret key."""

	def sign(key: bytes, message: str) -> bytes:
		return hmac.new(key, message.encode(), hashlib.sha256).digest()

	signature = sign(("AWS4" + secret_access_key).encode(), "11111111")
	for part in (region, "ses", "aws4_request", "SendRawEmail"):
		signature = sign(signature, part)
	return base64.b64encode(bytes([0x04]) + signature).decode()
