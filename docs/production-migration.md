# Production migration notes

This file covers database, encrypted-secret and staged rollout requirements. See
`docs/architecture.md` for runtime ownership, `docs/configuration.md` for environment variables,
and `deploy/vps/README.md` for day-to-day VPS operations.

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

## X stack feature flags

This release adds `X_LEGACY_DM_ENABLED`, `X_ACTIVITY_ENABLED`, and `XCHAT_ENABLED`. Their code
defaults are `true` for upgrade compatibility, but new deployment templates explicitly keep XChat
disabled because its key workflow remains experimental. API, Worker, and Scheduler must receive the
same values.

A disabled stack keeps credentials, cursors, subscriptions, and XChat key material intact. Matching
Outbox rows move to a recoverable `NEEDS_REVIEW` state and return to `PENDING` after re-enable.
Workers continue registering all durable actors so already accepted work can drain. Verified
accounts retain low-frequency reconciliation; PostgreSQL checkpoint leases serialize poll ownership
and open gaps trigger resumable backfill. `x_post_reply` is independent of the Legacy DM flag.

## Facebook, Instagram and WhatsApp feature flags

`FACEBOOK_MESSENGER_ENABLED`, `INSTAGRAM_MESSAGING_ENABLED`, and `WHATSAPP_ENABLED` default to
`true` in code for upgrade compatibility. New deployment templates explicitly set all three to
`false`. API, Worker and Scheduler must receive identical values.

First deploy the flag-aware image to every role while preserving the existing `true` values. Verify
that no old container remains. To disable a platform, stop API, Worker and Scheduler together,
change the flag, and restart all three roles; an old image does not understand these gates and an
old Worker can still send queued work. Re-enabling also requires a coordinated three-role restart.
Paused provisioning jobs and Outbox rows resume automatically without consuming a disabled-period
attempt. Signed webhooks received while disabled retain only tenant/app ownership, the event family,
and a SHA-256 body digest, not the original message payload.

## Feishu additive platform revisions and rollout

Revision `e4b7c2d9a610` is additive directly after `c2f4a6d8e901`. It only replaces
`ck_platform_accounts_platform` so `platform_accounts.platform` also accepts `feishu`; it does not
create a table, rewrite account rows or change existing credentials. Dropping and recreating the
check constraint takes a PostgreSQL table lock, so apply it through the API migration owner during a
quiet rollout window and do not run an ad hoc concurrent migration.

Revision `f8a1c3d5e702` follows `e4b7c2d9a610` and creates the unique partial index
`uq_raw_events_feishu_webhook_external_event` on
`raw_events(platform_account_id, external_event_id)` where `source='feishu'`,
`ingress_kind='webhook'`, and `external_event_id IS NOT NULL`. The ingress value is the sanitized,
nonblank Feishu `header.event_id`, not `message_id`, and the same constraint applies while the
feature flag is off. Before upgrade, run this inventory while normal ingress remains online:

```sql
SELECT platform_account_id, external_event_id, count(*)
FROM raw_events
WHERE source = 'feishu'
  AND ingress_kind = 'webhook'
  AND external_event_id IS NOT NULL
GROUP BY platform_account_id, external_event_id
HAVING count(*) > 1;
```

The result must be empty. The migration fails rather than deleting or rewriting append-only RawEvent
evidence when historical duplicates exist; stop and use a separately reviewed data-repair plan or a
verified backup instead of editing evidence ad hoc. PostgreSQL can leave a failed concurrent build as
an invalid same-name index. Inspect that state before retrying:

```sql
SELECT index_class.relname, index_row.indisvalid, index_row.indisready
FROM pg_index AS index_row
JOIN pg_class AS index_class ON index_class.oid = index_row.indexrelid
JOIN pg_namespace AS namespace ON namespace.oid = index_class.relnamespace
WHERE namespace.nspname = current_schema()
  AND index_class.relname = 'uq_raw_events_feishu_webhook_external_event';
```

After the reviewed duplicate repair, rerunning Alembic is safe: revision `f8a1c3d5e702` detects and
concurrently removes an invalid leftover before rebuilding the index. A valid same-name index is
accepted as completed, covering a process exit after concurrent DDL but before the Alembic version
row advanced. The index is created and dropped concurrently in an Alembic autocommit block, so
existing Feishu and non-Feishu ingress stays online and neither only Feishu nor all RawEvent writers
need to pause. A concurrent index build can consume substantial storage I/O, so monitor database
load and schedule it for an appropriate operating window. Downgrading to `e4b7c2d9a610` only drops
this index, also concurrently and safely if it is already absent.

Downgrading `e4b7c2d9a610` first queries for any Feishu account and fails closed with
`cannot downgrade while Feishu platform accounts exist`. Disablement is not sufficient: remove and
reprovision those accounts only under an approved rollback plan, or restore the verified pre-release
database backup. After all Feishu rows are absent, downgrade restores the previous five-value
platform constraint.

Roll out the Feishu-capable code with `FEISHU_ENABLED=false` on API, Worker and Scheduler. Confirm all
three roles run the same immutable image digest and the database is at the unique current head `d3f6a1b8c904`. Enablement is
then a coordinated release operation: stop or replace API, Worker and Scheduler so all three receive
`FEISHU_ENABLED=true` and the same health interval on that one digest. Do not expose ingress on a new
API while an old Worker or Scheduler remains.

After coordinated enablement, provision the tenant account, copy the returned Callback URL into the
Feishu console, complete URL verification, subscribe to `im.message.receive_v1`, publish the
application and perform draft-only smoke checks. Callback configuration is manual and is not
performed by the provisioning API. Do not declare the release complete until API, Worker and
Scheduler all report the identical digest required by `scripts/publish_railway_release.sh`. Promote
an account from `BOT_DRAFT_ONLY` to `BOT_ACTIVE` only after provider-side smoke verification. No
production Feishu credential or successful live Feishu E2E is implied by this migration.

Revision `d4e7f2a9b608` makes `/webhooks/meta/{app_public_id}` unambiguous across Facebook Login
`platform_family=meta` and standalone Instagram Login `platform_family=instagram`. The migration
aborts if a public ID is already present in both families. Check and rename/reprovision a collision
before deployment:

```sql
SELECT public_id, array_agg(platform_family ORDER BY platform_family)
FROM platform_apps
WHERE platform_family IN ('meta', 'instagram')
GROUP BY public_id
HAVING count(*) > 1;
```

## Event and send contracts

`CanonicalEvent` now persists an additive `event_kind=message` field. New readers default historical
serialized events without the field to `message`; old readers ignore the additional key. Telegram,
Meta, WhatsApp and Chatwoot normalization now preserves unsupported media metadata on the Message
and creates an `UNSUPPORTED_ATTACHMENT` human work item. Receipts and reactions remain in RawEvent
evidence without creating DecisionJobs.

Direct Outbox delivery now validates nonblank text and binds the destination target to the source
`ReplyDecision.message_id`, `Message.reply_target`, Conversation contact and account identity before
sender resolution. Existing valid rows require no migration. Historical rows with missing source
links, malformed targets, wrong recipients, or private X post replies move to operator-visible
`NEEDS_REVIEW` without consuming a network attempt; the application does not infer recipients from
arbitrary strings. Telegram retains only its prior empty-target fallback from the persisted
conversation destination.

## Human operations inbox rollout

Revision `f6c2a9d81b40` adds `human_work_items`, explicit Outbox provenance and reply targets,
structured draft review fields, and attachment metadata. It backfills open work for existing
`HANDOFF_PENDING` and `HUMAN_ACTIVE` conversations and derives legacy Outbox targets from their
ReplyDecision where available.

Revision `b8e1d4f7a2c3` makes the Conversation tenant authoritative for each work item before adding
the composite tenant/Conversation foreign key. After that rewrite, it retains a `CLAIMED` item only
when `assigned_user_id` resolves to an `admin_users` row in the Conversation tenant,
`assigned_actor` exactly equals `user:{username}`, and `claimed_at` is present. Every other claimed
item, including legacy bootstrap or incomplete attribution, is released to `WAITING`; the migration
clears `assigned_user_id`, `assigned_actor`, and `claimed_at`, then increments `version` once so an
operator with a stale inbox must reload. The existing missing-attribution and missing-timestamp
repair remains part of this rule.

Inventory affected rows before upgrade:

```sql
SELECT
    w.id,
    w.tenant_id AS work_tenant_id,
    c.tenant_id AS conversation_tenant_id,
    w.assigned_user_id,
    w.assigned_actor,
    u.username AS assigned_username,
    u.tenant_id AS assigned_user_tenant_id,
    w.claimed_at,
    w.version
FROM human_work_items AS w
JOIN conversations AS c ON c.id = w.conversation_id
LEFT JOIN admin_users AS u ON u.id = w.assigned_user_id
WHERE w.tenant_id IS DISTINCT FROM c.tenant_id
   OR (
       w.status = 'CLAIMED'
       AND (
           w.claimed_at IS NULL
           OR u.id IS NULL
           OR u.tenant_id IS DISTINCT FROM c.tenant_id
           OR w.assigned_actor IS DISTINCT FROM 'user:' || u.username
       )
   )
ORDER BY c.tenant_id, w.created_at, w.id;
```

Record expected repair counts in the rollout log before applying the migration. The two counts can
overlap because a row may need both a tenant rewrite and claim release:

```sql
SELECT
    count(*) FILTER (
        WHERE w.tenant_id IS DISTINCT FROM c.tenant_id
    ) AS tenant_rewrites,
    count(*) FILTER (
        WHERE w.status = 'CLAIMED'
          AND (
              w.claimed_at IS NULL
              OR u.id IS NULL
              OR u.tenant_id IS DISTINCT FROM c.tenant_id
              OR w.assigned_actor IS DISTINCT FROM 'user:' || u.username
          )
    ) AS claims_released
FROM human_work_items AS w
JOIN conversations AS c ON c.id = w.conversation_id
LEFT JOIN admin_users AS u ON u.id = w.assigned_user_id;
```

Take and verify a database backup before upgrade. Downgrade preserves the repaired rows and cannot
reconstruct released assignment attribution; restore the backup if that history must be recovered.

The previous Worker cannot deliver `MANUAL_REPLY` rows because it resolves direct targets only
through a ReplyDecision. For this release's API-first Railway rollout, pause claims, resolves,
account-policy mutations, manual replies, and draft approvals before the migration starts. Keep the
pause until every API instance runs the new image and API, Worker, and Scheduler all report
`SUCCESS` at the same digest. Read-only inbox use remains safe. Run the standard
`scripts/publish_railway_release.sh`, keep the coordinated pause through its final digest
verification, then perform one manual-reply smoke test with a dedicated platform account. A row
accepted during an accidental mixed-version window may move to `NEEDS_REVIEW`; inspect its delivery
attempts and retry only after every Worker is on the new digest.

The migration is expand-only for the running application. Database rollback still requires the
normal verified pre-release backup because removing work items and provenance loses operator audit
context even though an Alembic downgrade is mechanically available.

Revision `a9d4e6f2b713` is a data-only lifecycle repair directly after `f8a1c3d5e702`. It acquires the
same conversation delivery advisory locks as runtime claim/resolve, in deterministic Conversation ID
order, before changing affected rows. It repairs:

- open `CLAIMED` work paired with `HANDOFF_PENDING`, `BOT_ACTIVE`, or `BOT_DRAFT_ONLY` to
  `HUMAN_ACTIVE`, using the assigned actor as `human_agent_id`; it also repairs mismatched
  attribution for claimed work already paired with `HUMAN_ACTIVE` without rewriting correctly
  attributed rows;
- open `WAITING` work paired with `BOT_ACTIVE` or `BOT_DRAFT_ONLY` to `HANDOFF_PENDING`;
- `HANDOFF_PENDING` with no open work and at least one `RESOLVED` item to the owning account's current
  `automation_default`, clearing human attribution. Because Alembic has no runtime deployment
  settings, stored Meta `BOT_ACTIVE` policy is conservatively repaired to `BOT_DRAFT_ONLY`;
- pending or failed `DECISION/BOT` Outboxes for conversations that still have open work to
  `CANCELLED/TAKEOVER`.

The repair deliberately does not rewrite `CLOSED`, `BOT_COOLDOWN`, or resolved `HUMAN_ACTIVE` rows.
Every changed automation row increments `state_version` and records a migration-specific
`state_changed_reason`; a correctly attributed claimed `HUMAN_ACTIVE` row is unchanged. After taking
conversation advisory locks, the migration locks every affected account row in deterministic account
ID order before any state update. The account selection requires matching Conversation and account
tenants and ignores mismatched ownership. A concurrent account-policy update that commits before the
migration obtains the row lock is therefore read as the committed policy; one that starts later waits
for the migration transaction. The operational pause is still required because Alembic cannot
coordinate mixed-version API behavior around those locks. Inventory the four repair classes and
record counts before upgrade; after upgrade, verify no open work remains paired with a bot state and
no claimed work remains paired with `HANDOFF_PENDING`:

```sql
SELECT w.status, s.state, count(*)
FROM human_work_items AS w
JOIN automation_states AS s ON s.conversation_id = w.conversation_id
WHERE w.status IN ('WAITING', 'CLAIMED')
GROUP BY w.status, s.state
ORDER BY w.status, s.state;

SELECT count(*) AS stranded_resolved_handoff
FROM automation_states AS s
WHERE s.state = 'HANDOFF_PENDING'
  AND EXISTS (
      SELECT 1 FROM human_work_items AS w
      WHERE w.conversation_id = s.conversation_id AND w.status = 'RESOLVED'
  )
  AND NOT EXISTS (
      SELECT 1 FROM human_work_items AS w
      WHERE w.conversation_id = s.conversation_id AND w.status IN ('WAITING', 'CLAIMED')
  );
```

This migration accompanies an API behavior change: claim now means active human takeover, and
resolve now restores the account policy in the same transaction. During the mixed-version window,
keep claims, resolves, account-policy mutations, manual replies, and draft approvals paused until
every API instance runs the new image and API, Worker, and Scheduler have converged on the same
digest. Read-only inbox use remains safe. Worker and Scheduler retain the same durable paused-message
behavior: inbound messages during `HANDOFF_PENDING` or `HUMAN_ACTIVE` persist terminal ignore
decisions and are never replayed after resolve. The Alembic downgrade to `f8a1c3d5e702` is explicitly
a data no-op and cannot reconstruct prior inconsistent state; downgrade/re-upgrade is safe but
irreversible. Restore the verified backup if the pre-repair data itself must be recovered.

## Prompt governance and draft-first knowledge rollout

Revision `d3f6a1b8c904` follows `a9d4e6f2b713`. Before upgrade, take and verify a PostgreSQL backup and record these inventories:

```sql
SELECT tenant_id, brand_id, revision, updated_by, length(persona) AS legacy_persona_chars
FROM reply_prompts
ORDER BY tenant_id, brand_id;

SELECT status, count(*)
FROM knowledge_documents
GROUP BY status
ORDER BY status;

SELECT id, tenant_id, brand_id, platform, reply
FROM knowledge_documents
WHERE status = 'published'
  AND (reply ~ '[[:alnum:]._%+-]+@[[:alnum:].-]+\\.[[:alpha:]]{2,}'
       OR reply ~ '(^|[^0-9])[0-9]([[:space:].-]*[0-9]){5,}([^0-9]|$)'
       OR reply ~* '(https?://|www\\.)'
       OR reply ~* '(^|[^[:alnum:]_.+@-])@[[:alnum:]_]'
       OR reply ~* '(telegram|whatsapp|wechat|line|skype|qq|feishu|lark|微信|飞书)'
       OR reply ~* '(hotline|phone|tel|客服|热线|电话).{0,16}[0-9]{3,5}');
```

Treat every PII-looking published template in the pre-upgrade inventory as unclassified. After upgrade, repeat the query with `is_official_contact` in the selected columns and retain both exports in the rollout record.

The migration aborts if any knowledge status is not exactly `draft` or `published`. It preserves every existing valid status: the production inventory of 399 published rows remains published and is grandfathered. It does not classify, unpublish, rewrite, or broadly moderate historical templates. New manual and CSV rows become drafts and cannot be retrieved until an administrator explicitly publishes them.

Every existing `reply_prompts` row is intentionally neutralized: `voice_preferences` becomes the canonical code default, `persona` becomes the exact code-compiled compatibility projection, `revision` increments once, and `updated_by` becomes `migration:d3f6a1b8c904`. Arbitrary legacy prompt text is not copied into JSON or audit. Alembic downgrade cannot recover that text; restore the verified pre-upgrade backup if it is required.

Before the migration starts, pause decision and delivery dispatch as well as `/admin/prompt` and `/admin/knowledge` mutations. Scale Worker and Scheduler to zero; tenant-global kill switches alone are insufficient because delivery does not re-check them. Record the current region and replica counts first, then run the equivalent of:

```bash
railway scale --environment production --service worker us-east4-eqdc4a=0
railway scale --environment production --service scheduler us-east4-eqdc4a=0
```

Wait until no `decision_jobs` row is `PROCESSING` and no `DECISION/BOT` Outbox row is `SENDING`. Revision `d3f6a1b8c904` aborts if such a send is active and quarantines queued `PENDING` or `FAILED` `DECISION/BOT` Outboxes as `NEEDS_REVIEW/PROMPT_GOVERNANCE_ROLLOUT`. With both roles still at zero replicas, run `scripts/publish_railway_release.sh`; the script retains the prior digest, promotes the immutable SHA image, migrates and verifies API first, then creates the target Worker and Scheduler deployments. After the script succeeds, restore the recorded capacities against the target deployments:

```bash
railway scale --environment production --service worker us-east4-eqdc4a=1
railway scale --environment production --service scheduler us-east4-eqdc4a=1
```

Do not resume admin mutations until Worker and Scheduler processes are running, all three roles report `SUCCESS` at the same target digest, migration head and `/healthz` are correct, and role logs are clean. If the release script cannot verify a zero-replica deployment, stop and use a reported manual fallback: deploy API from the already-promoted immutable digest, restore and deploy Worker, then restore and deploy Scheduler while preserving every digest, health, rollback, and deployment-ID invariant in this runbook. Old Workers must never make automated decisions after `d3f6a1b8c904`. Replace the example region and replica values with the recorded Railway topology rather than assuming them in another environment.

After upgrade:

1. verify prompt rows contain only canonical JSON and the compiled projection, with the expected single revision increment;
2. verify knowledge status counts exactly match the pre-upgrade inventory and unknown statuses are rejected by the database constraint;
3. keep all new/imported records as drafts while they are reviewed;
4. for an official email or long-number contact template, explicitly classify it as `is_official_contact=true`, review the exact reply text and tenant/brand/platform scope, then explicitly publish it;
5. verify no `DECISION/BOT` Outbox remains in `PENDING`, retryable `FAILED`, or `SENDING`; review quarantined `PROMPT_GOVERNANCE_ROLLOUT` rows instead of retrying them blindly;
6. smoke-test that the exact published template is sent verbatim, while a draft/unpublished copy and any LLM-generated, copied, or modified contact detail produce `GUARD_PII_LEAK` handoff with no Outbox.

Official-contact classification is a narrow deterministic sending approval, not a general content moderation system. Historical templates are never automatically classified. Alembic downgrade does not restore quarantined Outbox statuses; use the pre-upgrade backup and delivery-attempt evidence if historical queue state must be reconstructed.

## Polling RawEvent journal

Revision `d6b8f0a2c431` adds tenant/account/stream/conversation/occurrence metadata to `raw_events` and preserves Legacy X DM plus XChat polling occurrences before normalization or decryption side effects. It also persists external conversation and event metadata on `normalized_events`.

A database trigger makes RawEvent evidence fields append-only while still allowing operational status transitions. Code or manual SQL that attempts to rewrite payload, source, ownership, occurrence context or timestamps will fail with `raw_event_evidence_is_append_only`.

## Durable polling checkpoints

Revision `a1c7e4f2b903` creates `platform_checkpoints`, `sync_runs`, and `sync_gaps`. It migrates Legacy X DM and per-conversation XChat cursors from `platform_accounts.config`, retains the old config keys for rollback, and makes the new checkpoint rows authoritative for writes. Database leases plus fencing revisions serialize Scheduler ownership. Page caps, pagination failures, and XChat decryption failures leave the checkpoint unchanged and open a resumable gap.

Pause old Scheduler processes before starting the new Scheduler and keep the mixed-version window short: old code writes config cursors while new code writes checkpoint rows.

## Initial RawEvent dispatch recovery

The existing `b2d8f5a3c714` RawEvent processing columns now also back generic initial-dispatch recovery; this release adds no new table or migration. New Telegram, Meta, X direct, Chatwoot webhook, and Chatwoot reconciliation rows persist a versioned actor contract in immutable `RawEvent.context`.

New recovery actors use the dedicated `initial_raw_v1` Dramatiq queue. Old workers do not declare or consume that queue, while new workers retain the old direct and Chatwoot actor signatures to drain messages produced by the previous image. This supports either API-first or Worker-first rolling replacement without interpreting a token as the old actor payload.

Scheduler redispatches lost reservations and expired worker leases. Eight failed worker claims move a row to `INITIAL_DISPATCH_DEAD`; broker-send failures remain retryable without consuming a worker attempt. Historical `PENDING` rows without versioned dispatch metadata and all polling/XChat-owned rows are intentionally excluded because their actor arguments cannot be reconstructed safely.

## Platform account contract

Revision `92a6e3f1c4d8` converts legacy `platform_accounts.status='CONNECTED'` rows to the canonical
`active` value, fills missing capability keys with fail-closed defaults, and adds database checks for
supported platforms, account statuses, and JSON-object capability storage. The application also
rejects non-boolean permission flags, platform limits above the provider maximum, and delivery
routes that do not belong to the account platform.

The migration aborts instead of guessing when it finds an unknown platform/status, a non-object
capability, an unknown capability key, a non-boolean permission flag, or an invalid platform text
limit. Start with this coarse inventory before deployment; the migration's error identifies the first
account that fails the complete application contract:

```sql
SELECT platform, status, jsonb_typeof(capability), count(*)
FROM platform_accounts
GROUP BY platform, status, jsonb_typeof(capability)
ORDER BY platform, status;
```

Back up the database before applying the revision. Fix unsupported values through an explicit data
repair reviewed by an operator; do not coerce them during application startup.

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

## Conversation decision generation fencing

Revision `c2f4a6d8e901` adds a monotonic `conversations.decision_generation`, nullable message and
job generation provenance, random claim-token fences on `decision_jobs`, and durable job provenance
on `reply_decisions`. The migration ranks every public inbound contact message by `history_seq`,
copies that generation through its job and decision, marks nonterminal older jobs `SUPERSEDED`, and
cancels only their pending or failed bot decision outboxes with `STALE_CONVERSATION_INPUT`.

The required revision order is `f6c2a9d81b40` (human inbox and Outbox provenance), then
`b8e1d4f7a2c3` (work-item tenant/assignment repair), then `c2f4a6d8e901` (decision fencing), then
`e4b7c2d9a610` (add Feishu to the platform constraint), then `f8a1c3d5e702` (Feishu RawEvent webhook deduplication), then `a9d4e6f2b713` (human handoff lifecycle repair), then `d3f6a1b8c904` (structured voice and draft-first knowledge governance). Do not cherry-pick only the final revision.
Upgrade through `f6c2a9d81b40`, then run the Human operations
inventory above and record repair counts before applying `b8e1d4f7a2c3`. Before the fencing step,
inventory active DecisionJobs by conversation/status, pending or failed `DECISION/BOT` Outboxes tied
to older inbound messages, conversations with multiple public inbound contact messages, and
currently `SENDING` Outboxes; let
active sends drain and retain the counts in the rollout log.

Deploy the database migration before replacing Worker and Scheduler instances. Three PostgreSQL
triggers bound the mixed-version window:

- `trg_reserve_message_decision_generation` assigns a generation to eligible inbound rows written by
  old ingestion code and retires stale work;
- `trg_attach_decision_job_generation` derives the generation for old DecisionJob writers without
  incrementing the conversation again;
- `trg_attach_reply_decision_generation` attaches job/generation provenance and rejects stale old
  decision writers.

Explicit message generations, agent or bot messages, outgoing messages, and private messages do not
advance the counter. New workers do not hold a transaction or database lock while calling the LLM;
finalization acquires its locks only after the model returns and atomically writes decision
provenance, completes the fenced job, and aggregates RawEvent state.

Coordinate Scheduler replacement with the same rollout. The new recovery code understands
`SUPERSEDED` as terminal and clears expired claim tokens before reclaim; keep old/new Scheduler
overlap short, and do not start the new Worker or Scheduler until the database is at head. Scheduler
reads one settings snapshot at startup, so deploy the intended `SCHEDULER_TICK_SECONDS`,
`SCHEDULER_CORE_INTERVAL_SECONDS`, `SCHEDULER_CORE_WARN_AFTER_SECONDS`, and
`SCHEDULER_INSPECTION_WARN_AFTER_SECONDS` values with the replacement rather than changing them
mid-rollout. Compatibility triggers permit a bounded mixed-version generation window, but API,
Worker and Scheduler must converge on one image digest before the rollout is complete.

The migration briefly takes row and advisory locks for conversations with existing decision jobs or
pending bot decision outboxes. Take a database backup before upgrade. A database downgrade removes
the fencing columns and triggers and is safe only after all new Worker and Scheduler instances have
been replaced with the prior image; otherwise restore the pre-upgrade backup.

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
