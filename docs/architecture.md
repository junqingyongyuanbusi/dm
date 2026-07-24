# Runtime architecture

This document describes the current implementation. Historical design notes in `PLAN.md` and
`docs/superpowers/plans/` are not runtime authority.

## System shape

Social Reply is a single Python codebase and container image deployed in three roles:

```text
Platform webhooks -> API -> PostgreSQL
                         -> Redis / Dramatiq -> Worker -> platform APIs / OpenAI

Scheduler -> PostgreSQL recovery scans
          -> Redis / Dramatiq re-enqueue
          -> X polling and reconciliation
```

It is a modular monolith, not a set of HTTP microservices. API, Worker, and Scheduler share the
same PostgreSQL schema, encryption keys, feature flags, and connector code.

## Process ownership

| Role | Owns | Does not own |
| --- | --- | --- |
| API | Admin UI, users and sessions, OAuth callbacks, Provisioning API, webhook verification, webhook `RawEvent` persistence, actor dispatch | LLM decisions, durable retries, routine platform sending |
| Worker | Provisioning actors, direct and Chatwoot ingestion actors, XChat decryption, `DecisionJob` execution, Outbox delivery | Schema migration, periodic recovery scheduling |
| Scheduler | Provisioning/decision/Outbox recovery, Chatwoot reconciliation when enabled, X polling/health, Meta token/subscription reconciliation | HTTP traffic, decision business logic |

The image entrypoint uses `SERVICE_ROLE=api|worker|scheduler`. API runs database preparation and
migrations. Worker and Scheduler refuse to start until the database is at Alembic head and encrypted
credential envelopes can be read.

## State ownership

### PostgreSQL is durable truth

PostgreSQL owns:

- tenants, users, platform apps/accounts and encrypted credentials;
- webhook `RawEvent` rows and normalized event deduplication;
- contacts, conversations, messages and automation state;
- `ProvisioningJob`, `DecisionJob`, `ReplyDecision` and `OutboxMessage` state;
- delivery attempts, audit logs, knowledge documents/chunks, polling checkpoints, sync runs and gaps.

New webhook and Chatwoot reconciliation `RawEvent` rows persist a versioned initial-dispatch contract before commit. Process crashes and Redis queue loss are recoverable from that row before normalization, then from `DecisionJob` and `OutboxMessage` after their transactional boundaries.

### Redis is transient infrastructure

Redis owns only short-lived or reconstructable state:

- Dramatiq broker queues;
- automation kill switches;
- OAuth transaction state and short-lived coordination/cache data.

Redis is not the source of truth for accounts, ingestion, decisions, deliveries, or durable jobs. Scheduler sweeps re-enqueue versioned initial-dispatch `RawEvent` rows plus persisted `ProvisioningJob`, `DecisionJob`, and `OutboxMessage` work after queue loss.

### External systems

- Platform APIs are the transport boundary for Telegram, Facebook, Instagram, WhatsApp and X.
- Chatwoot is an optional bridge, not a startup dependency.
- OpenAI-compatible chat and embedding APIs are called only from decision/knowledge code, never
  directly from webhook routers.

## Direct message path

```text
Telegram / Meta / WhatsApp / X webhook
  -> API signature and account validation
  -> Meta: minimal verified-request evidence + account-scoped occurrence RawEvent
  -> other platforms: account-scoped RawEvent
  -> RawEvent committed in PostgreSQL
  -> CanonicalEvent serialized to Dramatiq
  -> Worker ingest_canonical_event
  -> NormalizedEvent dedupe
  -> text-only CanonicalEvent(kind=message)
  -> Contact / Conversation / inbound Message / AutomationState
  -> DecisionJob(PENDING) in the same transaction
  -> Worker claims DecisionJob(PROCESSING)
  -> rules -> kill switch -> knowledge/history -> LLM -> final guard
  -> ReplyDecision + OutboxMessage in one transaction
  -> post-commit delivery fast path
  -> send-time tenant/status/capability/window/takeover validation
  -> account-scoped connector sender
  -> SENT / FAILED / NEEDS_REVIEW plus DeliveryAttempt
```

A successful text send is written back as an outbound `Message`, linked to its source Outbox row.
The delivery fast path reduces latency; it does not replace durability. `sweep_outbox` recovers rows
that were committed but not sent because a process crashed.

Webhook ingestion and X polling persist `RawEvent` evidence before normalization. New Telegram, Meta, X direct, Chatwoot webhook, and Chatwoot reconciliation rows include immutable versioned dispatch metadata. Scheduler reservations and fenced worker leases recover commit-to-dispatch loss, broker loss, and worker crashes; malformed metadata or eight exhausted worker claims become `INITIAL_DISPATCH_DEAD`. Historical `PENDING` rows without the versioned contract are deliberately not guessed or replayed. Polling and XChat remain owned by their checkpoint/gap and specialized recovery paths.

Polling writes one append-only evidence row per Legacy DM, XChat encrypted envelope, or XChat key-change occurrence, including account, conversation, occurrence time and page/cursor context. `PlatformCheckpoint` is the authoritative cursor, `SyncRun` records each claimed attempt, and `SyncGap` retains page-cap, pagination, or decryption gaps until a fenced backfill completes.

## Chatwoot bridge

When `CHATWOOT_ENABLED=true`, API registers the Chatwoot webhook and Scheduler runs message
reconciliation. Chatwoot events converge on the same decision and Outbox model.

When disabled:

- API does not expose the Chatwoot webhook route;
- Scheduler does not poll Chatwoot;
- Worker retains the compatibility actor to drain already queued events;
- decisions defer as `DEFERRED_CHATWOOT` and disabled deliveries pause as
  `NEEDS_REVIEW/CHATWOOT_DISABLED`;
- re-enabling the bridge returns recoverable work to the queue.

## X stack boundaries

The shared X webhook can carry legacy DM, post/mention, and XChat event families. Ingress filters
these families independently.

| Flag | Controls |
| --- | --- |
| `X_LEGACY_DM_ENABLED` | Legacy DM permission probing and `x_dm` sending |
| `X_ACTIVITY_ENABLED` | CRC/signed webhook route, webhook health, and Activity transport |
| `XCHAT_ENABLED` | PIN activation, subscriptions, XChat webhook processing and `x_chat_message` sending |

`x_post_reply` is independent of the Legacy DM flag. Disabling a stack stops its polling,
subscription/recovery work and sending while preserving tokens, cursors, private keys and recoverable
Outbox rows. After re-enable, PostgreSQL leases prevent duplicate ownership and open gaps drive
resumable backfill.

## Meta platform boundaries

Facebook Messenger, Instagram Messaging, and WhatsApp use independent Settings gates even though
they share the signed Meta webhook route. Messenger and Instagram are text-DM-only launch paths:
OAuth/provisioning request DM permissions, install the `messages` subscription, keep comments out of
capability-gated ingress, and default new conversations to `BOT_DRAFT_ONLY`. A verified enabled
request writes one minimal app-scoped request record plus one owned RawEvent per recognized account
entry. A disabled event family stores only a tenant/app-scoped audit summary and SHA-256 body digest,
then acknowledges without dispatch.

Meta Page/account calls include HMAC-SHA256 `appsecret_proof`. Provisioning creates an active route
with health `PROVISIONING` so concurrent inbound occurrences remain durable, while send-time checks
pause all Meta delivery until `READY`; subscription failure disables the account. Scheduler health
reconciliation probes account identity,
reads and repairs the desired subscription, and writes sanitized `READY`, `ERROR`, or
`REAUTH_REQUIRED` state to account config. Existing provisioning jobs and direct Outbox rows pause
without consuming a disabled-period attempt and recover after the matching flag is re-enabled.

These gates are process-local configuration. Old images do not recognize them, so disabling a
platform requires the coordinated API/Worker/Scheduler restart documented in
`docs/configuration.md`; a mixed-version rolling window is not a valid disable procedure.

## Provisioning path

```text
Admin UI / Provisioning API
  -> authenticate Principal and tenant scope
  -> encrypt staging credentials with PLATFORM_SECRET_KEYS
  -> ProvisioningJob(PENDING) + audit row
  -> Dramatiq provisioning actor
  -> platform credential/capability validation
  -> PlatformApp / PlatformAccount upsert
  -> sanitized result + audit, encrypted staging bundle cleared
```

Retryable jobs use persisted backoff and Scheduler recovery. Browser OAuth callbacks submit the same
durable job boundary rather than directly mutating account rows.

## Reliability invariants

- Recoverable webhook actor arguments are committed as immutable RawEvent dispatch metadata before actor dispatch.
- Initial dispatch uses a versioned dedicated queue, database reservations, worker claim tokens, leases and bounded dead-letter attempts.
- Historical or polling RawEvents without that contract are never inferred by the generic sweep.
- Inbound facts and their `DecisionJob` are committed together.
- Decisions and Outbox intent are committed together.
- Public sending rechecks takeover state immediately before network I/O.
- Direct sending rechecks tenant, account status, route/platform compatibility, capability, text
  limit and delivery window.
- Direct text commands bind destination and target kind to the source ReplyDecision/Message target
  and Conversation contact before sender resolution; malformed or wrong-recipient rows never call a
  connector.
- Telegram/Meta/WhatsApp/X adapters only emit supported text-message CanonicalEvents. Unsupported
  media, receipts and reactions remain RawEvent evidence and do not enter reply decisions.
- Ambiguous post-send transport failures do not blindly retry.
- Account sender caches are keyed by account config version and, for Meta senders, PlatformApp config version so App Secret rotation replaces the client.
- Scheduler recovery is idempotent and each sweep is exception-isolated.

## Deployment invariants

- API, Worker, and Scheduler must receive the same feature flags and `PLATFORM_SECRET_KEYS`.
- Only API performs migration; Worker and Scheduler start after the database reaches head.
- PostgreSQL and Redis are private infrastructure endpoints. Only API is exposed through the public
  ingress/tunnel.
- `BOT_DRAFT_ONLY` remains the default for newly connected accounts; full automatic sending is an
  explicit operator decision.
