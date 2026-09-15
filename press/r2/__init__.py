"""Tenant storage on Cloudflare R2 (Aretenic ADR 041).

Every school gets two buckets in a Cloudflare account that holds nothing else:

- ``<subdomain>-media``, near the school, whose key goes into the site's config for apotheke;
- ``<subdomain>-backups``, in another region, whose key stays in Press and is sent to the app
  server only in that site's backup jobs.

Isolated like ``press/vultr_client/``: shared Press files only call into this package.
"""
