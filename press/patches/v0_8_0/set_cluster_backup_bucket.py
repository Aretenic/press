import frappe


def execute():
	"""Move each cluster-backups bucket's cluster to Cluster.backup_bucket, so one bucket can serve many clusters."""
	# The field is gone from Backup Bucket, but its column stays on sites that had it
	if not frappe.db.has_column("Backup Bucket", "cluster"):
		return

	BackupBucket = frappe.qb.DocType("Backup Bucket")
	buckets = (
		frappe.qb.from_(BackupBucket)
		.select(BackupBucket.name, BackupBucket.cluster)
		.where(BackupBucket.cluster.isnotnull() & (BackupBucket.cluster != ""))
		# Aretenic: a cluster also has binlog buckets and each school's own (ADR 041); only its
		# cluster-backups bucket may become Cluster.backup_bucket
		.where(BackupBucket.site.isnull() | (BackupBucket.site == ""))
		.where(BackupBucket.purpose.isnull() | BackupBucket.purpose.isin(["", "Cluster Backups"]))
		# The last modified bucket of a cluster wins, as in the old get_all (sort_field: modified)
		.orderby(BackupBucket.modified)
		.run(as_dict=True)
	)
	for bucket in buckets:
		if frappe.db.exists("Cluster", bucket.cluster):
			frappe.db.set_value(
				"Cluster", bucket.cluster, "backup_bucket", bucket.name, update_modified=False
			)
