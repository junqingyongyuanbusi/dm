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

## Optional Chatwoot bridge

`CHATWOOT_ENABLED` now defaults to `false`. Direct-only environments no longer need
`CHATWOOT_WEBHOOK_SECRET` or `CHATWOOT_API_TOKEN`. Existing Chatwoot deployments must explicitly
set `CHATWOOT_ENABLED=true` before deploying this release and keep the same value on API, Worker,
and Scheduler.

With the bridge disabled, the API route and reconcile task are absent. The Worker keeps the
compatibility Actor long enough to drain already queued RawEvents; their delivery decisions remain
`DEFERRED_CHATWOOT` and resume after the bridge is enabled again. Legacy pending Chatwoot
deliveries move to `NEEDS_REVIEW/CHATWOOT_DISABLED` while disabled and are safely returned to the
Outbox queue after re-enable. Database fields and conversation mappings remain intact.

### Previously released `a3f9c2e14b78` databases

An earlier implementation dropped `credential_ref`, `webhook_secret_ref`, and
`staging_secret_ref`. Those deleted values cannot be reconstructed by a later migration. Before
rollout, take and verify a database backup and confirm every active account has usable bundle data.
If an account has neither an encrypted/plaintext bundle nor an accessible legacy file, restore its
credential from the platform and reprovision the account through the control plane.

Legacy files are not automatically deleted. They are retained as rollback evidence and should be
removed only after encrypted-runtime acceptance and backup verification. Restrict them to mode
0600 in the meantime.

## Admin users and server-side sessions

Revision `da4e19c7b203` adds `admin_users` and `admin_sessions`. Revision `e7b2c4d9a610` repairs VPS databases that applied an earlier form of `da4e19c7b203` without `admin_sessions.credential_fingerprint`; it conditionally adds the column and is a no-op when the column already exists. Neither revision modifies existing platform, conversation, delivery, or credential rows.

- `ADMIN_USERNAME` / `ADMIN_PASSWORD` remain environment-backed bootstrap superadmin credentials and are never copied into PostgreSQL.
- Existing signed `/admin` cookies are intentionally invalid after the application switches to opaque database sessions; administrators must log in once again.
- Do not run old and new API images together for an extended rolling window: they interpret the same Cookie name differently. Upgrade the database first, then replace API replicas together.
- Ordinary users are created at `/admin/users`, have exactly one Tenant, and must change their initial password on first login.
- Back up `admin_users` before downgrading this revision; downgrade removes both user and session tables.

## Message history context

Revision `f3a6c1d8e250` adds a database-generated `messages.history_seq`, the `messages.source_outbox_id` provenance key, and a `(conversation_id, history_seq)` index. During the migration it briefly locks `messages` and `outbox_messages`, backfills already-confirmed `SENT` text Outbox rows as outbound conversation facts, and assigns the combined timeline a deterministic order. Take a database backup first and deploy the API migrator before starting the new Worker/Scheduler versions.

History sent to an external LLM is bounded by `CONVERSATION_HISTORY_LIMIT` and `CONVERSATION_HISTORY_MAX_CHARS`; set the limit to `0` to disable multi-turn history. Customer text is retained unchanged in PostgreSQL but email and long-number patterns are redacted in the external LLM request.

## Railway X OAuth state-key rollout

The X OAuth transaction key changes from `oauth:x:<raw-token>` to
`x:oauth1:transaction:<sha256(oauth_token)>`. The new application can read and atomically consume
both formats, but the previous production image can read only the legacy key. Railway must use a
two-phase API rollout:

1. Before deploying the new image, set the API-only compatibility writer without triggering an
   immediate deploy:

   ```bash
   railway variable set X_OAUTH_LEGACY_STATE_WRITE=true --service api --skip-deploys
   ```

2. Deploy the new image to API, Worker, and Scheduler. Wait for the new API deployment to report
   `SUCCESS`, then use `railway deployment list --service api --limit 2` to confirm the previous API
   deployment is `REMOVED` or `REMOVING`. During this phase every API writer uses the legacy key,
   while every new reader accepts both formats.
3. Switch the API writer to the hashed key and redeploy only API:

   ```bash
   railway variable set X_OAUTH_LEGACY_STATE_WRITE=false --service api --skip-deploys
   railway redeploy --service api --yes
   ```

4. Confirm the phase-2 API deployment is `SUCCESS`, `/healthz` is healthy, callback access logs do
   not contain query parameters, and new Redis transactions use only the hashed prefix. Keep this
   dual-reader release in production for at least one 10-minute OAuth transaction TTL before
   removing the compatibility variable.

Do not roll phase 2 directly back to an image that reads only legacy keys. First set the writer back
to `true` on the dual-reader image, deploy it, wait at least 10 minutes for hash-key transactions to
expire or complete, and only then deploy the older image.

## Rollback

Revision `c9e83a4d1f20` intentionally has no in-place downgrade because tenant-scoped knowledge
allows duplicate hashes across tenants. Roll back by restoring the verified pre-upgrade database
backup rather than running `alembic downgrade`.
