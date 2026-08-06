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
- local operations inbox state in `HumanWorkItem`, including tenant-scoped claim ownership and optimistic versioning;
- Feishu handoff routes, operator authorization, notification intents and card-action receipts;
- `ProvisioningJob`, `DecisionJob`, `ReplyDecision` and `OutboxMessage` state;
- immutable Outbox origin, actor and explicit reply-target provenance for bot decisions, draft approvals and manual replies;
- delivery attempts, audit logs, knowledge documents/chunks, polling checkpoints, sync runs and gaps.

New webhook and Chatwoot reconciliation `RawEvent` rows persist a versioned initial-dispatch contract before commit. Process crashes and Redis queue loss are recoverable from that row before normalization, then from `DecisionJob` and `OutboxMessage` after their transactional boundaries.

### Redis is transient infrastructure

Redis owns only short-lived or reconstructable state:

- Dramatiq broker queues;
- automation kill switches;
- OAuth transaction state and short-lived coordination/cache data.

Redis is not the source of truth for accounts, ingestion, decisions, deliveries, or durable jobs. Scheduler sweeps re-enqueue versioned initial-dispatch `RawEvent` rows plus persisted `ProvisioningJob`, `DecisionJob`, and `OutboxMessage` work after queue loss.

### External systems

- Platform APIs are the transport boundary for Telegram, Facebook, Instagram, WhatsApp, Feishu and X.
- Chatwoot is an optional bridge, not a startup dependency.
- OpenAI-compatible chat and embedding APIs are called only from decision/knowledge code, never
  directly from webhook routers.

## Direct message path

```text
Telegram / Meta / WhatsApp / Feishu / X webhook
  -> API signature, encryption and account validation
  -> Meta: minimal verified-request evidence + account-scoped occurrence RawEvent
  -> other platforms: account-scoped RawEvent
  -> RawEvent committed in PostgreSQL
  -> CanonicalEvent serialized to Dramatiq
  -> Worker ingest_canonical_event
  -> NormalizedEvent dedupe
  -> text-only CanonicalEvent(kind=message)
  -> Contact / Conversation / inbound Message / AutomationState
  -> reserve Conversation.decision_generation
  -> DecisionJob(PENDING, generation) in the same transaction
  -> supersede older active jobs and cancel their unsent bot decision Outboxes
  -> Worker claims DecisionJob(PROCESSING) with a random claim_token
  -> commit claim; rules -> kill switch -> knowledge/history -> LLM -> final guard with no database transaction held
  -> short final transaction locks the Conversation and validates job/generation/claim_token
  -> ReplyDecision + OutboxMessage + DecisionJob(COMPLETED) in one transaction
  -> post-commit delivery fast path
  -> send-time tenant/status/capability/window/takeover validation
  -> account-scoped connector sender
  -> SENT / FAILED / NEEDS_REVIEW plus DeliveryAttempt
```

A successful text send is written back as an outbound `Message`, linked to its source Outbox row.
The delivery fast path reduces latency; it does not replace durability. `sweep_outbox` recovers rows
that were committed but not sent because a process crashed.

Each reply-eligible public inbound contact message advances one monotonic conversation generation;
duplicate ingestion, private notes, outbound messages and agent/bot messages do not. Reserving a new
generation makes older nonterminal jobs `SUPERSEDED`, which is terminal and counts as settled when
aggregating the parent RawEvent. It also cancels only older `PENDING` or `FAILED`
`DECISION/BOT` Outboxes with `STALE_CONVERSATION_INPUT`; manual replies and draft approvals remain
valid. Delivery repeats the generation check immediately before provider I/O. Conversation advisory
locks serialize cancellation with delivery, but an external send that already completed cannot be
undone.

Webhook ingestion and X polling persist `RawEvent` evidence before normalization. New Telegram, Meta, Feishu, X direct, Chatwoot webhook, and Chatwoot reconciliation rows include immutable versioned dispatch metadata. Scheduler reservations and fenced worker leases recover commit-to-dispatch loss, broker loss, and worker crashes; malformed metadata or eight exhausted worker claims become `INITIAL_DISPATCH_DEAD`. Historical `PENDING` rows without the versioned contract are deliberately not guessed or replayed. Polling and XChat remain owned by their checkpoint/gap and specialized recovery paths.

Polling writes one append-only evidence row per Legacy DM, XChat encrypted envelope, or XChat key-change occurrence, including account, conversation, occurrence time and page/cursor context. `PlatformCheckpoint` is the authoritative cursor, `SyncRun` records each claimed attempt, and `SyncGap` retains page-cap, pagination, or decryption gaps until a fenced backfill completes.

## Local human operations path

The built-in Admin and PostgreSQL inbox are the native operations path; they do not depend on
Chatwoot:

```text
HANDOFF / unsupported attachment -> HumanWorkItem(WAITING) + HANDOFF_PENDING
                                 -> HandoffNotificationIntent(PENDING or BLOCKED_CONFIG)
claim                          -> HumanWorkItem(CLAIMED) + HUMAN_ACTIVE
resolve                        -> HumanWorkItem(RESOLVED) + current account automation policy
legacy exception               -> explicit resume to BOT_DRAFT_ONLY or BOT_ACTIVE
DRAFT                          -> ReplyDecision(DRAFT) -> explicit approval
Delivery exception             -> OutboxMessage(NEEDS_REVIEW) -> explicit retry
Manual reply / draft approval  -> explicit inbound Message target -> provenance-bearing OutboxMessage
                                -> account-scoped sender -> outbound Message(source_outbox_id)
```

`PlatformAccount.automation_default` is the account-wide policy and restore target shared by many
conversations; a `HumanWorkItem` is conversation-local. Only one open `WAITING` or `CLAIMED` item
exists per conversation. Claim and resolve use the conversation delivery advisory lock before row
locks, so they serialize with inbound generation reservation and send-time checks. Claim atomically
moves `WAITING/HANDOFF_PENDING` to `CLAIMED/HUMAN_ACTIVE`, assigns the actor, advances both versions,
audits both mutations, and cancels only pending or failed `DECISION/BOT` Outboxes for that
conversation. Resolve atomically closes the item and restores the account's current policy, clearing
human attribution and using `BOT_DRAFT_ONLY` when the deployment's Meta gate disallows a stored
`BOT_ACTIVE` policy. Normal resolution therefore needs no separate resume action; resume remains for
stranded legacy `HANDOFF_PENDING`, `HUMAN_ACTIVE`, or `BOT_COOLDOWN` states without open work.

Inbound messages captured while `HANDOFF_PENDING` or `HUMAN_ACTIVE` still persist their `Message`,
`DecisionJob`, and terminal `ReplyDecision(ignore)` and create no bot Outbox. Those completed jobs
are never reconsidered after resolve; only a later newly ingested message snapshots the restored
policy. A manual reply creates or claims work, moves the conversation to `HUMAN_ACTIVE`, cancels
pending or failed bot-decision Outboxes, and records `actor_id` plus `reply_to_message_id`. Bot
decisions use `DECISION/BOT`, approved drafts use `DRAFT_APPROVAL/ADMIN_HUMAN`, and manual sends use
`MANUAL_REPLY/ADMIN_HUMAN`, so the Outbox row is the durable provenance bridge from operator action
to outbound history. Direct-platform delivery is independent of Chatwoot; accounts deliberately
using a Chatwoot destination still require their persisted conversation mapping.

When Feishu handoff notifications are enabled, the HANDOFF persistence transaction also creates or
reuses one `HandoffNotificationIntent` per work item. Missing or disabled routing does not roll back
the customer handoff: it leaves a durable `BLOCKED_CONFIG` notification for Scheduler recovery.
Worker creates or updates a minimized schema-2.0 card in the configured support chat; notification
leases, claim tokens, desired revisions and provider message IDs remain PostgreSQL state. Card claim
and resolve callbacks use the same session-scoped work-item transactions as Admin, require a
Tenant/app-scoped operator allowlist, and persist an idempotent receipt keyed by Feishu callback
`event_id`. A card resolve records `FEISHU_OPERATOR_ATTESTED`; it proves the operator declaration,
not delivery of an external social-platform reply. A local Admin manual reply can instead retain
Reply Core Outbox delivery evidence.

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
they share the signed Meta webhook route. Both Facebook and Instagram remain draft-first and
DM-only by default; enabling both Meta release switches makes newly authorized accounts
comment-capable while they remain `BOT_DRAFT_ONLY`. OAuth validates target-specific comment permissions;
provisioning installs Facebook `feed` or Instagram `comments`; and comment decisions are forced to
public visibility before Final Guard. Delivery therefore creates only `meta_public_comment` child
replies, never `meta_private_reply`; account-level automation state also applies to DMs. A verified
enabled request writes one minimal app-scoped request record plus one owned RawEvent per recognized
account entry. A disabled event family stores only a tenant/app-scoped audit summary and SHA-256
body digest, then acknowledges without dispatch.

Instagram credential paths remain explicit. Facebook Login stores a Page token, IG professional
account ID and required Page ID under `PlatformApp(platform_family=meta)`; subscription and sending
use the Page path, but comments are subscribed on the App-level `instagram` object because Page
`subscribed_apps` does not accept `comments`. Instagram Login stores an Instagram long-lived token
and IG professional account ID under `PlatformApp(platform_family=instagram)`; it forbids a Page ID
and uses the Instagram Graph account path, where `comments` is also an account-level subscription.
A partial unique index makes the shared Meta webhook `public_id` unambiguous across these two App
families.

Meta Page/account calls include HMAC-SHA256 `appsecret_proof`. Provisioning creates an active route
with health `PROVISIONING` so concurrent inbound occurrences remain durable, while send-time checks
pause all Meta delivery until `READY`; subscription failure disables the account. Scheduler health
reconciliation probes account identity and Meta comment permission scope,
reads and repairs the desired subscription, and writes sanitized `READY`, `ERROR`, or
`REAUTH_REQUIRED` state to account config. Existing provisioning jobs and direct Outbox rows pause
without consuming a disabled-period attempt and recover after the matching flag is re-enabled.

These gates are process-local configuration. Old images do not recognize them, so disabling a
platform requires the coordinated API/Worker/Scheduler restart documented in
`docs/configuration.md`; a mixed-version rolling window is not a valid disable procedure.

## Feishu platform boundary

Feishu uses one account-owned `PlatformAccount` per enterprise self-built application Bot. App ID,
App Secret, Verification Token and Encrypt Key belong to that account's encrypted credential bundle;
Feishu does not create or share a `PlatformApp`. The account's opaque `public_id` selects the
always-registered `/webhooks/feishu/{public_id}` route.

URL-verification challenges may be plaintext or AES-encrypted and remain available while
`FEISHU_ENABLED=false`. Normal events must use the encrypted envelope, carry valid `X-Lark-*`
signature headers within the replay window, match the account Verification Token and App ID, and
decrypt successfully before dispatch. Invalid authentication creates no `RawEvent`. A verified
normal event is acknowledged even while disabled; the API persists sanitized
`IGNORED_AT_INGRESS` evidence with `ingress_gate=FEISHU_DISABLED` and does not dispatch it.

Accepted normal callbacks require a nonblank `header.event_id`. The API atomically reserves that
provider callback ID per account in PostgreSQL, so a duplicate callback returns 200 without creating
a second `RawEvent` or dispatching again, including while `FEISHU_ENABLED=false`. New
`im.message.receive_v1` occurrences are committed as account-scoped `RawEvent` rows with the same
immutable direct-dispatch contract used by other recoverable webhook ingress. Worker normalization
requires `event.message.create_time` and uses it for provider ordering; header `create_time` remains
metadata only. Under the conversation delivery advisory lock, a strictly older occurrence is kept as
a `NormalizedEvent` with `stale_provider_order` disposition but creates no Message, generation,
DecisionJob or Outbox. Normalization supports P2P text and group text that explicitly mentions the
configured Bot. Group conversation identity includes account, chat, sender and any available
thread/root scope so users and threads do not share decision history accidentally. Bot/self events,
unsupported schemas and blank mention-only text do not enter decisions; unsupported attachments
remain durable evidence and follow the human-work path.

Outbox delivery has two explicit destinations: `feishu_p2p_reply` and `feishu_group_reply`. Both
reply to the persisted inbound Feishu message target; thread replies preserve thread scope. The
Outbox row UUID is sent as Feishu's `uuid`, so recovery and token refresh reuse one provider
idempotency key. Delivery rechecks `FEISHU_ENABLED` and account health immediately before network
I/O. Disabled or non-ready accounts pause in operator-visible `NEEDS_REVIEW` without consuming an
attempt; ambiguous post-dispatch outcomes are never retried blindly.

Scheduler's Feishu health sweep validates credentials, Bot activation and Bot identity, then stores
sanitized `READY`, `BOT_NOT_ACTIVE`, `BOT_ID_MISMATCH`, `CREDENTIAL_INVALID` or `ERROR` status. This
inspection lane is separate from durable core recovery and cannot serialize RawEvent, DecisionJob or
Outbox recovery.

Handoff cards use the same enterprise self-built application credentials but a separate
`/webhooks/feishu/{public_id}/card-actions` protocol and `FEISHU_HANDOFF_NOTIFICATIONS_ENABLED`
gate. Card callbacks repeat signature, encryption, Verification Token, App ID, timestamp and body
size validation, then perform only a bounded PostgreSQL transaction. They do not call Feishu, Redis
or the LLM on the three-second acknowledgement path. Card creation uses a stable UUID, but ambiguous
creation after Feishu's one-hour deduplication window becomes `NEEDS_REVIEW`; deterministic updates
by provider message ID remain retryable.

## Scheduler lanes

Scheduler runs due sweeps independently rather than as one serial cycle:

- the **core recovery lane** owns ProvisioningJob, initial RawEvent, DecisionJob, Outbox, Feishu
  handoff-notification and XChat recovery;
- the **inspection lane** owns Chatwoot reconciliation, X polling/subscription/webhook inspection,
  Meta health reconciliation and Feishu account health inspection.

A slow inspection does not block recurring core recovery. Each named sweep permits at most one
running instance, missed ticks coalesce, and submission, body and completion failures are isolated
from other due sweeps. `SCHEDULER_CORE_WARN_AFTER_SECONDS` and
`SCHEDULER_INSPECTION_WARN_AFTER_SECONDS` produce soft warnings only: potentially side-effecting
provider work is not force-cancelled. Scheduler reads one settings snapshot at startup, scans due
work at `SCHEDULER_TICK_SECONDS`, runs ordinary core sweeps at
`SCHEDULER_CORE_INTERVAL_SECONDS`, and gives in-flight work a bounded five-second shutdown grace
period without cancelling it.

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
- Inbound facts, their monotonic decision generation and their `DecisionJob` are committed together.
- Older active generations become terminal `SUPERSEDED`; stale claim tokens cannot finalize after reclaim.
- Rules, retrieval and LLM calls hold no PostgreSQL transaction or Conversation lock; finalization is a short fenced transaction.
- A newer inbound cancels only unsent bot-decision Outboxes, and send-time generation validation is the final stale-input fence.
- Decisions and Outbox intent are committed together.
- Local human claims, explicit reply targets and manual/draft Outbox provenance remain tenant-scoped and durable without Chatwoot.
- Feishu handoff routing, operator authorization, card revisions and callback idempotency remain tenant- and app-scoped in PostgreSQL.
- Public sending rechecks takeover state immediately before network I/O.
- Direct sending rechecks tenant, account status, route/platform compatibility, capability, text
  limit and delivery window.
- Direct text commands bind destination and target kind to the source ReplyDecision/Message target
  and Conversation contact before sender resolution; malformed or wrong-recipient rows never call a
  connector.
- Telegram/Meta/WhatsApp/X adapters only emit supported text-message CanonicalEvents. Feishu also
  canonicalizes unsupported attachment metadata so it enters the `UNSUPPORTED_ATTACHMENT`
  human-work path rather than an automated text decision; receipts, reactions and other unsupported
  occurrences remain RawEvent evidence.
- Ambiguous post-send transport failures do not blindly retry.
- Account sender caches are keyed by account config version and, for Meta senders, PlatformApp config version so App Secret rotation replaces the client.
- Scheduler recovery is idempotent; each sweep is exception-isolated, max-one-instance and coalescing, with soft slow-run warnings rather than hard cancellation.

## Design precedents

These public systems informed semantics only; Social Reply does not add them as dependencies:

- [Oban unique jobs](https://hexdocs.pm/oban/unique_jobs.html): enqueue-time uniqueness has a configurable period and state scope; it is not, by itself, an execution-concurrency guarantee.
- [GoodJob](https://github.com/bensheldon/good_job): a PostgreSQL job implementation that uses advisory locks to coordinate run-once execution across processes.
- [APScheduler user guide](https://apscheduler.readthedocs.io/en/3.x/userguide.html#limiting-the-number-of-concurrently-executing-instances-of-a-job): `max_instances` limits concurrent executions, while coalescing controls how accumulated missed runs are submitted.

## Deployment invariants

- API, Worker, and Scheduler must receive the same feature flags, including `FEISHU_ENABLED` and
  `FEISHU_HANDOFF_NOTIFICATIONS_ENABLED`, and the same `PLATFORM_SECRET_KEYS`.
- Only API performs migration; Worker and Scheduler start after the database reaches head.
- PostgreSQL and Redis are private infrastructure endpoints. Only API is exposed through the public
  ingress/tunnel.
- `BOT_DRAFT_ONLY` remains the default for newly connected accounts; full automatic sending is an
  explicit operator decision.
