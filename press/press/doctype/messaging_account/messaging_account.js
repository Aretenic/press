// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

// Hosted messaging for one school (Aretenic ADR 094).
frappe.ui.form.on('Messaging Account', {
	refresh(frm) {
		if (frm.is_new()) return
		const call = (method, label, confirm) => () => {
			const run = () =>
				frappe
					.call({ method, args: { account: frm.doc.name }, freeze: true })
					.then(() => frm.reload_doc())
			confirm ? frappe.confirm(confirm, run) : run()
		}
		if (frm.doc.status !== 'Active') {
			frm.add_custom_button(
				__('Provision'),
				call(
					'press.messaging.provision.provision',
					__('Provision'),
					__(
						'This creates a Twilio subaccount and SES resources for the site, and writes its site config.',
					),
				),
			)
		} else {
			frm.add_custom_button(
				__('Rotate Token'),
				call(
					'press.messaging.provision.rotate_token',
					__('Rotate Token'),
					__('The old token stops working at once.'),
				),
			)
		}
		if (frm.doc.email_paused) {
			frm.add_custom_button(
				__('Lift Email Pause'),
				call(
					'press.messaging.reputation.resume',
					__('Lift Email Pause'),
					__("Only lift the pause once the school's list is cleaned."),
				),
			)
		}
	},
})
