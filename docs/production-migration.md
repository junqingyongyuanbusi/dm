# Production migration notes

## Platform secret encryption

Set `PLATFORM_SECRET_KEYS` before deploying. The first comma-separated Fernet key encrypts new
values; later keys remain available for decrypting older envelopes.

The API startup runs `scripts/prepare_database.py`:

1. expand the schema through `b7d1e4a9c2f3` when necessary;
2. encrypt legacy file references or plaintext JSONB bundles;
3. upgrade to Alembic head.

Worker and scheduler roles refuse to start until the database is at head and all encrypted
envelopes can be decrypted.

### Previously released `a3f9c2e14b78` databases

An earlier implementation dropped `credential_ref`, `webhook_secret_ref`, and
`staging_secret_ref`. Those deleted values cannot be reconstructed by a later migration. Before
rollout, take and verify a database backup and confirm every active account has usable bundle data.
If an account has neither an encrypted/plaintext bundle nor an accessible legacy file, restore its
credential from the platform and reprovision the account through the control plane.

Legacy files are not automatically deleted. They are retained as rollback evidence and should be
removed only after encrypted-runtime acceptance and backup verification. Restrict them to mode
0600 in the meantime.

## Rollback

Revision `c9e83a4d1f20` intentionally has no in-place downgrade because tenant-scoped knowledge
allows duplicate hashes across tenants. Roll back by restoring the verified pre-upgrade database
backup rather than running `alembic downgrade`.
