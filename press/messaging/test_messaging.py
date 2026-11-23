"""Hosted messaging (Aretenic ADR 094). Twilio, SES and the sites are mocked.

What Press must be able to count on: a text goes out only from an approved number, to a country
the school turned on, within the allowance or the school's choice to keep texting; emergency and
short numbers are never texted; every text, email, bounce and complaint is counted against the
right school; a school's email is paused before SES would pause everyone's; and what the site
receives is signed so that only Press could have sent it.
"""

import hashlib
import hmac
import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import nowdate

from press import messaging as m
from press.messaging import forward, proxy, registration, reputation, segments, ses, sns, twilio, usage
from press.messaging.provision import hash_token, site_for_token

SITE = "zz-messaging.example.test"
US = "+14155550123"
BR = "+5511987654321"


def make_account(**values):
	doc = frappe.get_doc(
		{
			"doctype": m.ACCOUNT,
			"site": SITE,
			"status": "Active",
			"token_hash": hash_token("zz-token"),
			"twilio_subaccount_sid": "ACzz",
			"messaging_service_sid": "MGzz",
			"ses_tenant": "zz",
			"ses_configuration_set": "zz-notices",
			"ses_promotional_configuration_set": "zz-promotional",
			"overuse": "email",
			**values,
		}
	)
	doc.flags.ignore_links = True
	doc.insert(ignore_permissions=True)
	return doc


def approve(account, purpose="General", number="+18885550100"):
	doc = frappe.get_doc(
		{
			"doctype": m.NUMBER,
			"account": account.name,
			"purpose": purpose,
			"status": "approved",
			"number": number,
		}
	)
	doc.flags.quiet = True
	doc.insert(ignore_permissions=True)
	return doc


class MessagingCase(FrappeTestCase):
	def setUp(self):
		if not frappe.db.exists("Site", SITE):
			# A bare row: these tests need the link, not a site Press would create.
			site = frappe.new_doc("Site")
			site.update(
				{"name": SITE, "subdomain": "zz-messaging", "domain": "example.test", "status": "Active"}
			)
			site.db_insert()
		settings = frappe.get_single(m.SETTINGS)
		settings.update(
			{"enabled": 1, "base_url": "https://messaging.example.test/messaging", "margin_percent": 20}
		)
		settings.save(ignore_permissions=True)
		frappe.clear_document_cache(m.SETTINGS, m.SETTINGS)
		self.events = []
		for target, name in ((forward, "site_event"), (forward, "message_event")):
			p = patch.object(target, name, side_effect=lambda *a, _n=name, **k: self.events.append((_n, a)))
			p.start()
			self.addCleanup(p.stop)
		self.sent = []
		p = patch.object(
			twilio,
			"send",
			side_effect=lambda account, **k: self.sent.append(k) or {"sid": "SMzz", "num_segments": "1"},
		)
		p.start()
		self.addCleanup(p.stop)

	def tearDown(self):
		frappe.db.rollback()


class TestNumbers(FrappeTestCase):
	def test_emergency_and_short_numbers_are_never_texted(self):
		for raw in ("911", "112", "+1 911", "999"):
			with self.assertRaises(proxy.Refused) as refused:
				proxy.checked_number(raw)
			self.assertIn(refused.exception.code, ("emergency_number", "blocked_number"), raw)
		with self.assertRaises(proxy.Refused) as refused:
			proxy.checked_number("911")
		self.assertEqual(refused.exception.code, "emergency_number")

	def test_valid_numbers_come_back_in_e164(self):
		self.assertEqual(proxy.checked_number("+1 (415) 555-0123"), (US, "US"))
		self.assertEqual(proxy.checked_number(BR), (BR, "BR"))
		with self.assertRaises(proxy.Refused):
			proxy.checked_number("+1 555")

	def test_segments_match_the_site(self):
		self.assertEqual(segments.count("a" * 160), 1)
		self.assertEqual(segments.count("a" * 161), 2)
		self.assertEqual(segments.count("ç" * 70), 1)
		self.assertEqual(segments.count("ç" * 71), 2)


class TestSignatures(FrappeTestCase):
	def test_site_signature_uses_the_token_hash(self):
		"""The site keys with sha256(token): aretenic.messaging.proxy.valid_signature."""
		body = b'{"kind":"usage"}'
		token_hash = hash_token("zz-token")
		key = hashlib.sha256(b"zz-token").hexdigest().encode()
		self.assertEqual(forward.signature(token_hash, body), hmac.new(key, body, hashlib.sha256).hexdigest())

	def test_twilio_signature(self):
		params = {"MessageSid": "SM1", "MessageStatus": "delivered"}
		url = "https://messaging.example.test/messaging/twilio/status/x"
		payload = url + "MessageSidSM1MessageStatusdelivered"
		import base64

		good = base64.b64encode(hmac.new(b"tok", payload.encode(), hashlib.sha1).digest()).decode()
		self.assertTrue(twilio.valid_signature(url, params, good, "tok"))
		self.assertFalse(twilio.valid_signature(url, {**params, "MessageStatus": "read"}, good, "tok"))
		self.assertFalse(twilio.valid_signature(url, params, None, "tok"))

	def test_sns_certificates_come_only_from_sns(self):
		self.assertTrue(
			sns.cert_url_ok("https://sns.us-east-1.amazonaws.com/SimpleNotificationService-abc.pem")
		)
		for url in (
			"http://sns.us-east-1.amazonaws.com/x.pem",
			"https://sns.us-east-1.amazonaws.com.evil.test/x.pem",
			"https://evil.test/sns.us-east-1.amazonaws.com/x.pem",
			"https://sns.us-east-1.amazonaws.com/x.txt",
		):
			self.assertFalse(sns.cert_url_ok(url), url)

	def test_sns_string_to_sign(self):
		message = {
			"Type": "Notification",
			"Message": "m",
			"MessageId": "1",
			"Timestamp": "t",
			"TopicArn": "arn",
			"Subject": None,
		}
		self.assertEqual(
			sns.string_to_sign(message),
			b"Message\nm\nMessageId\n1\nTimestamp\nt\nTopicArn\narn\nType\nNotification\n",
		)
		self.assertIsNone(sns.string_to_sign({"Type": "Other"}))

	def test_smtp_password_has_the_version_byte(self):
		password = ses.smtp_password("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1")
		import base64

		self.assertEqual(base64.b64decode(password)[0], 4)
		self.assertEqual(len(base64.b64decode(password)), 33)


class TestTexts(MessagingCase):
	def test_token_finds_only_an_active_account(self):
		account = make_account()
		self.assertEqual(site_for_token("zz-token"), SITE)
		self.assertIsNone(site_for_token("other"))
		account.db_set("status", "Revoked")
		self.assertIsNone(site_for_token("zz-token"))

	def test_no_approved_number_refuses(self):
		account = make_account()
		with self.assertRaises(proxy.Refused) as refused:
			proxy.send_text(account, {"channel": "sms", "to": US, "body": "Hi"})
		self.assertEqual(refused.exception.code, "not_registered")
		self.assertTrue(refused.exception.final)

	def test_domestic_text_counts_against_the_allowance(self):
		account = make_account()
		approve(account)
		result = proxy.send_text(account, {"channel": "sms", "to": US, "body": "Class moved."})
		self.assertEqual(result, {"id": "SMzz", "texts": 1})
		self.assertEqual(usage.this_month(SITE)["texts"], 1)
		self.assertEqual(self.sent[0]["to"], US)
		self.assertIsNone(self.sent[0]["from_number"])

	def test_spent_allowance_follows_the_school_choice(self):
		account = make_account()
		approve(account)
		usage.count(SITE, "texts", 5000)
		with self.assertRaises(proxy.Refused) as refused:
			proxy.send_text(account, {"channel": "sms", "to": US, "body": "Hi"})
		self.assertEqual(refused.exception.code, "over_allowance")
		# A verification code still goes, or nobody could opt in.
		proxy.send_text(account, {"channel": "sms", "to": US, "body": "Code 123456", "verification": True})
		account.db_set("overuse", "keep")
		account.reload()
		proxy.send_text(account, {"channel": "sms", "to": US, "body": "Hi"})
		self.assertEqual(usage.this_month(SITE)["texts"], 5002)

	def test_crossing_the_allowance_tells_the_site_once(self):
		account = make_account()
		approve(account)
		usage.count(SITE, "texts", 4999)
		proxy.send_text(account, {"channel": "sms", "to": US, "body": "Hi"})
		account.db_set("overuse", "keep")
		account.reload()
		proxy.send_text(account, {"channel": "sms", "to": US, "body": "Hi"})
		spent = [e for e in self.events if e[0] == "site_event" and e[1][1].get("kind") == "texts_spent"]
		self.assertEqual(len(spent), 1)

	def test_international_needs_its_country_and_is_passed_through(self):
		account = make_account()
		approve(account)
		with self.assertRaises(proxy.Refused) as refused:
			proxy.send_text(account, {"channel": "sms", "to": BR, "body": "Oi"})
		self.assertEqual(refused.exception.code, "country_disabled")
		account.db_set("international_countries", "BR")
		account.reload()
		usage.count(SITE, "texts", 5000)  # the allowance does not apply abroad
		with patch.object(twilio, "country_price", return_value=0.05):
			proxy.send_text(account, {"channel": "sms", "to": BR, "body": "Oi"})
		used = usage.this_month(SITE)
		self.assertEqual(used["international_texts"], 1)
		self.assertAlmostEqual(used["international_cost"], 0.06)
		self.assertEqual(used["texts"], 5000)

	def test_promotional_texts_use_the_promotional_number(self):
		account = make_account()
		approve(account)
		approve(account, "Promotional", "+18885550111")
		proxy.send_text(account, {"channel": "sms", "to": US, "body": "Gala", "purpose": "Promotional"})
		self.assertEqual(self.sent[-1]["from_number"], "+18885550111")

	def test_whatsapp_needs_the_approved_sender(self):
		account = make_account()
		with self.assertRaises(proxy.Refused) as refused:
			proxy.send_text(account, {"channel": "whatsapp", "to": BR, "body": "Oi"})
		self.assertEqual(refused.exception.code, "whatsapp_disabled")
		account.db_set({"whatsapp": 1, "whatsapp_ready": 1, "whatsapp_number": "+18885550122"})
		account.reload()
		proxy.send_text(account, {"channel": "whatsapp", "to": BR, "body": "Oi"})
		self.assertTrue(self.sent[-1]["whatsapp"])
		self.assertEqual(usage.this_month(SITE)["whatsapp_messages"], 1)

	def test_carrier_refusals_are_final(self):
		account = make_account()
		approve(account)
		with (
			patch.object(twilio, "send", side_effect=twilio.TwilioError("landline", 21614)),
			self.assertRaises(proxy.Refused) as refused,
		):
			proxy.send_text(account, {"channel": "sms", "to": US, "body": "Hi"})
		self.assertEqual((refused.exception.code, refused.exception.final), ("landline", True))
		with (
			patch.object(twilio, "send", side_effect=twilio.TwilioError("down", None, 503)),
			self.assertRaises(proxy.Refused) as refused,
		):
			proxy.send_text(account, {"channel": "sms", "to": US, "body": "Hi"})
		self.assertFalse(refused.exception.final)

	def test_settings_from_the_site(self):
		account = make_account()
		proxy.save_settings(
			account, {"overuse": "keep", "international_countries": ["br", "PT", "1x"], "whatsapp": True}
		)
		account.reload()
		self.assertEqual(
			(account.overuse, account.international_countries, account.whatsapp), ("keep", "BR,PT", 1)
		)


class TestCallbacks(MessagingCase):
	def test_status_carries_a_carrier_opt_out(self):
		account = make_account()
		proxy.on_status(
			account, {"MessageSid": "SM1", "MessageStatus": "undelivered", "ErrorCode": "21610", "To": US}
		)
		kind, (site, channel, payload) = self.events[-1]
		self.assertEqual((kind, site, channel), ("message_event", SITE, "sms"))
		self.assertEqual(payload["error"], "opted_out")
		proxy.on_status(account, {"MessageSid": "SM1", "MessageStatus": "queued"})
		self.assertEqual(len(self.events), 1)

	def test_inbound_help_is_answered_and_forwarded(self):
		account = make_account(help_text="ZZ School, (555) 010-0000.")
		proxy.on_inbound(account, {"From": US, "To": "+18885550100", "Body": "help", "MessageSid": "SM2"})
		self.assertIn("Reply STOP", self.sent[-1]["body"])
		self.assertEqual(self.events[-1][1][2]["body"], "help")

	def test_whatsapp_inbound_is_routed_to_the_whatsapp_account(self):
		account = make_account()
		proxy.on_inbound(account, {"From": f"whatsapp:{BR}", "Body": "STOP", "MessageSid": "SM3"})
		self.assertEqual(self.events[-1][1][1], "whatsapp")
		self.assertEqual(self.events[-1][1][2]["from_address"], BR)

	def test_ses_events_are_counted_for_the_right_school(self):
		make_account()
		mail = {
			"tags": {"ses:configuration-set": ["zz-promotional"]},
			"destination": ["a@x.test", "b@x.test"],
		}
		proxy.on_ses_event({"eventType": "Send", "mail": mail})
		proxy.on_ses_event(
			{
				"eventType": "Bounce",
				"mail": mail,
				"bounce": {"bounceType": "Permanent", "bouncedRecipients": [{"emailAddress": "a@x.test"}]},
			}
		)
		proxy.on_ses_event(
			{
				"eventType": "Complaint",
				"mail": mail,
				"complaint": {"complainedRecipients": [{"emailAddress": "b@x.test"}]},
			}
		)
		proxy.on_ses_event({"eventType": "Send", "mail": {"tags": {"ses:configuration-set": ["other"]}}})
		used = usage.this_month(SITE)
		self.assertEqual((used["emails"], used["bounces"], used["complaints"]), (2, 1, 1))
		forwarded = [e[1][1] for e in self.events]
		self.assertIn({"kind": "email", "event": "bounce", "email": "a@x.test", "permanent": True}, forwarded)
		self.assertIn({"kind": "email", "event": "complaint", "email": "b@x.test"}, forwarded)


class TestReputation(MessagingCase):
	def test_verdict(self):
		s = frappe._dict(rate_window_sends=500, pause_bounce_rate=3, pause_complaint_rate=0.08)
		self.assertIsNone(reputation.verdict(100, 50, 5, s))  # too few sends to judge
		self.assertIsNone(reputation.verdict(1000, 2.9, 0.07, s))
		self.assertIn("bounced", reputation.verdict(1000, 3, 0, s))
		self.assertIn("spam", reputation.verdict(1000, 0, 0.1, s))

	def test_a_bouncing_school_is_paused_and_told(self):
		account = make_account()
		usage.count(SITE, "emails", 1000)
		usage.count(SITE, "bounces", 40)
		with patch.object(ses, "set_sending") as set_sending:
			reputation.check_all()
		set_sending.assert_called_once()
		self.assertFalse(set_sending.call_args.args[1])
		account.reload()
		self.assertTrue(account.email_paused)
		self.assertIn(
			{"kind": "email_pause", "paused": True, "reason": account.email_paused_reason},
			[e[1][1] for e in self.events],
		)

	def test_daily_limit_pause_lifts_itself(self):
		account = make_account(email_daily_limit=100)
		usage.count(SITE, "emails", 100)
		with patch.object(ses, "set_sending"):
			reputation.check_all()
			account.reload()
			self.assertTrue(account.email_paused)
			reputation.daily()
		account.reload()
		self.assertFalse(account.email_paused)


class TestRegistrationAndStatement(MessagingCase):
	def test_toll_free_is_bought_and_verified(self):
		account = make_account()
		with (
			patch.object(twilio, "buy_toll_free", return_value=("+18885550133", "PNzz")),
			patch.object(twilio, "submit_toll_free_verification", return_value="HHzz"),
		):
			result = registration.submit(
				account,
				{
					"purpose": "General",
					"kind": "Toll-free",
					"use_case": "Notices",
					"sample_messages": ["ZZ: hi"],
					"brand": {"legal_name": "ZZ"},
				},
			)
		number = frappe.get_doc(m.NUMBER, result["id"])
		self.assertEqual(
			(number.number, number.verification_sid, number.status), ("+18885550133", "HHzz", "submitted")
		)
		self.assertEqual(registration.rows(account)[0]["id"], number.name)

	def test_ten_dlc_waits_for_us(self):
		account = make_account()
		result = registration.submit(account, {"kind": "10DLC", "use_case": "Notices"})
		self.assertEqual(result["status"], "in_review")

	def test_approval_reaches_the_site_and_starts_billing(self):
		account = make_account()
		with patch.object(twilio, "toll_free_status", return_value=("approved", None)):
			number = frappe.get_doc(
				{
					"doctype": m.NUMBER,
					"account": account.name,
					"kind": "Toll-free",
					"status": "submitted",
					"verification_sid": "HH1",
				}
			)
			number.flags.quiet = True
			number.insert(ignore_permissions=True)
			registration.poll()
		number.reload()
		self.assertEqual((number.status, str(number.billed_from)), ("approved", nowdate()))
		self.assertEqual(self.events[-1][1][1]["kind"], "registration")

	def test_statement_counts_what_is_over(self):
		account = make_account()
		approve(account)
		approve(account, "Promotional", "+18885550111")
		usage.count(SITE, "texts", 5200)
		usage.count(SITE, "emails", 49000)
		period = nowdate()[:7]
		doc = frappe.get_doc(m.STATEMENT, usage.statement(SITE, period))
		self.assertEqual((doc.texts_over, doc.emails_over, doc.numbers, doc.extra_numbers), (200, 0, 2, 1))
		usage.count(SITE, "emails", 2000)
		doc = frappe.get_doc(m.STATEMENT, usage.statement(SITE, period))
		self.assertEqual(doc.emails_over, 1000)

	def test_usage_report(self):
		make_account()
		usage.count(SITE, "texts", 7)
		report = usage.report(SITE)
		self.assertEqual(
			(report["texts"], report["texts_included"], report["emails_included"]), (7, 5000, 50000)
		)
		self.assertEqual(json.loads(json.dumps(report))["period"], nowdate()[:7])
