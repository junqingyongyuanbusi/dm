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
| Scheduler | Provisioning/decision/Outbox recovery, Chatwoot reconciliation when enabled, X polling, webhook health and XChat subscription reconciliation | HTTP traffic, decision business logic |

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

Once a `ProvisioningJob`, `DecisionJob`, or `OutboxMessage` exists, process crashes and Redis queue loss are recoverable from PostgreSQL. A committed webhook `RawEvent` that has not yet produced its durable job is a known gap described below.

### Redis is transient infrastructure

Redis owns only short-lived or reconstructable state:

- Dramatiq broker queues;
- automation kill switches;
- OAuth transaction state and short-lived coordination/cache data.

Redis is not the source of truth for accounts, decisions, deliveries, or durable jobs. Scheduler sweeps re-enqueue persisted `ProvisioningJob`, `DecisionJob`, and `OutboxMessage` work after queue loss; there is not yet a generic PENDING `RawEvent` recovery sweep.

### External systems

- Platform APIs are the transport boundary for Telegram, Facebook, Instagram, WhatsApp and X.
- Chatwoot is an optional bridge, not a startup dependency.
- OpenAI-compatible chat and embedding APIs are called only from decision/knowledge code, never
  directly from webhook routers.

## Direct message path

```text
Telegram / Meta / WhatsApp / X webhook
  -> API signature and account validation
  -> RawEvent committed in PostgreSQL
  -> CanonicalEvent serialized to Dramatiq
  -> Worker ingest_canonical_event
  -> NormalizedEvent dedupe
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

Webhook ingestion and X polling persist `RawEvent` evidence before normalization. Polling writes one append-only evidence row per Legacy DM, XChat encrypted envelope, or XChat key-change occurrence, including account, conversation, occurrence time and page/cursor context. `PlatformCheckpoint` is the authoritative cursor, `SyncRun` records each claimed attempt, and `SyncGap` retains page-cap, pagination, or decryption gaps until a fenced backfill completes. A crash or dispatch loss between a RawEvent commit and creation of `DecisionJob` can still leave a PENDING row with no automatic recovery; the RawEvent recovery sweep remains the next reliability boundary.

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

`x_post_reply` is independent of the Legacy DM flag. Disabling a send stack preserves tokens,
cursors, private keys and recoverable Outbox rows. Verified accounts retain low-frequency polling;
PostgreSQL leases prevent duplicate ownership and open gaps drive resumable backfill.

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

- Webhook payloads are committed before actor dispatch; this is an audit boundary, not yet a complete RawEvent recovery guarantee.
- Inbound facts and their `DecisionJob` are committed together.
- Decisions and Outbox intent are committed together.
- Public sending rechecks takeover state immediately before network I/O.
- Direct sending rechecks tenant, account status, route/platform compatibility, capability, text
  limit and delivery window.
- Ambiguous post-send transport failures do not blindly retry.
- Account sender caches are keyed by `(platform, account_id, config_version)`.
- Scheduler recovery is idempotent and each sweep is exception-isolated.

## Deployment invariants

- API, Worker, and Scheduler must receive the same feature flags and `PLATFORM_SECRET_KEYS`.
- Only API performs migration; Worker and Scheduler start after the database reaches head.
- PostgreSQL and Redis are private infrastructure endpoints. Only API is exposed through the public
  ingress/tunnel.
- `BOT_DRAFT_ONLY` remains the default for newly connected accounts; full automatic sending is an
  explicit operator decision.
