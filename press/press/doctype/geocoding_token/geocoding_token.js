// Copyright (c) 2026, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("Geocoding Token", {
	refresh(frm) {
		if (frm.doc.status !== "Active") return;
		frm.add_custom_button(__("Rotate"), () =>
			frappe.confirm(__("Issue a new token to {0}? This one stops working.", [frm.doc.site]), () =>
				frm.call("rotate").then(() => frm.reload_doc())
			)
		);
	},
});
