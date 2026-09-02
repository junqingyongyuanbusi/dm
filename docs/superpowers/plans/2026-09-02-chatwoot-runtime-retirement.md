---
title: Chatwoot runtime retirement
status: implemented
created: 2026-09-02
scope: C1 runtime retirement; C2 schema cleanup deferred
---

# Chatwoot runtime retirement plan

## 1. Decision and problem frame

Chatwoot is no longer a product control plane or a production transport path. The canonical system
uses direct platform connectors, PostgreSQL `HumanWorkItem`, local draft review, manual reply, and
the transactional Outbox. Production evidence collected on 2026-09-02 shows:

- `CHATWOOT_ENABLED=false` on API, Worker, and Scheduler;
- all six production platform accounts use `config.delivery_mode="direct"`;
- zero `chatwoot_inbox_id` values, `ConversationMapping` rows, Chatwoot `RawEvent` rows,
  `chatwoot_conversation` Outboxes, `DEFERRED_CHATWOOT` jobs, or messages carrying a Chatwoot ID;
- the production Chatwoot webhook is not registered, and the configured historical Chatwoot host
  no longer resolves to an active service.

The target is therefore to retire Chatwoot as executable runtime behavior without combining that
change with a destructive database migration. Historical columns and published Alembic revisions
remain intact through this phase so an application rollback remains possible.

## 2. Scope

### In scope: C1 runtime retirement

- Remove Chatwoot webhook registration, signature handling, normalization, ingestion, reconciliation,
  queue actors, external API client, and external sending.
- Remove Chatwoot-specific decision deferral and Outbox recovery behavior.
- Make `delivery_mode="direct"` the only accepted active delivery mode. Missing, misspelled, or
  unknown delivery modes must fail closed rather than falling back to Chatwoot.
- Remove Chatwoot runtime settings and release-validation requirements.
- Rewrite tests so their defaults and fixtures model the production direct path.
- Update current architecture, configuration, operations, and reliability documentation.
- Revoke obsolete Chatwoot credentials after the runtime release is verified.

### Out of scope: C2 schema cleanup

- Do not modify published migration files.
- Do not drop `conversation_mappings` or any `chatwoot_*` database column in C1.
- Do not remove historical Chatwoot IDs from backups, audit exports, or old migration documentation.
- Do not rewrite historical files under `docs/superpowers/plans/`.
- Do not combine C1 with a production deployment unless the user separately requests release and
  deployment.

## 3. Success criteria

1. No API route, Worker actor, Scheduler sweep, HTTP client, or Outbox branch can contact Chatwoot.
2. Current platform provisioning continues to create direct accounts only.
3. A persisted account whose delivery mode is missing or not `direct` fails closed as
   `NEEDS_REVIEW` with a stable configuration error; it is never interpreted as Chatwoot.
4. Direct draft review, manual reply, automated reply, handoff, takeover cancellation, delivery
   retry, and outbound message materialization remain unchanged.
5. API, Worker, and Scheduler can start without any `CHATWOOT_*` variables.
6. Current documentation no longer presents Chatwoot as a supported optional integration.
7. The Alembic head remains `f3a7c9e1b5d2`; C1 introduces no migration.
8. Production keeps `CHATWOOT_ENABLED=false` throughout rollout, then obsolete URL/token/secret
   variables are removed or revoked after verification.

## 4. Safety gates before implementation

Re-run these read-only checks immediately before implementation or release if production state may
have changed:

- all three roles still report `CHATWOOT_ENABLED=false`;
- no platform account is non-direct or has `chatwoot_inbox_id`;
- `conversation_mappings` remains empty;
- no Chatwoot `RawEvent`, nonterminal `chatwoot_conversation` Outbox, or `DEFERRED_CHATWOOT` job
  exists;
- no operator, customer contract, external automation, or separate environment depends on
  Chatwoot;
- the Redis broker has no meaningful backlog for `process_initial_chatwoot_event_v1` or the legacy
  `process_chatwoot_event` actor.

If any database or business dependency is found, stop C1 and return to a bounded compatibility-drain
plan. Do not silently convert Chatwoot-backed history to direct delivery: historical inbound
messages may lack a valid direct `reply_target`.

## 5. Implementation units

### Unit 1: Add retirement contracts before deleting behavior

Purpose: establish the new fail-closed boundary and prevent Chatwoot from being reintroduced by an
old fixture or compatibility default.

Files:

- add `tests/unit/test_chatwoot_retirement_contract.py`;
- update `tests/unit/test_route_contract.py`;
- update `tests/unit/test_scheduler.py`;
- update `tests/unit/test_runtime_feature_flags.py`;
- update `tests/unit/test_config.py`;
- update `tests/unit/test_railway_config.py`;
- update `tests/integration/test_decision_persist.py`;
- update `tests/integration/test_outbox_sweep.py`.

Required scenarios:

1. The application has no `/webhooks/chatwoot` route even if a stale environment variable named
   `CHATWOOT_ENABLED` is present.
2. Scheduler sweep specifications never include `reconcile_chatwoot_messages`.
3. Settings construct successfully outside tests without Chatwoot URL, token, or webhook secret.
4. Railway validation does not require or compare any `CHATWOOT_*` variable.
5. `delivery_mode="direct"` remains accepted.
6. Missing, blank, `chatwoot`, or unknown delivery mode is rejected with one stable configuration
   error before an Outbox is created.
7. Outbox recovery never requeues `CHATWOOT_DISABLED` work.
8. A release-contract assertion confirms that active application and connector modules do not
   import `social_reply.connectors.chatwoot` and do not register a Chatwoot route or actor.

Execution posture: write these assertions first and observe the focused failures before removing
runtime behavior.

### Unit 2: Remove Chatwoot ingress and reconciliation

Delete:

- `src/social_reply/connectors/chatwoot/client.py`;
- `src/social_reply/connectors/chatwoot/signature.py`;
- `src/social_reply/connectors/chatwoot/normalizer.py`;
- `src/social_reply/application/event_ingestion/router.py`;
- `src/social_reply/application/event_ingestion/processor.py`;
- `src/social_reply/application/event_ingestion/reconcile.py`;
- `src/social_reply/application/event_ingestion/actors.py`;
- `scripts/send_test_webhook.py`.

Modify:

- `apps/api/main.py`: remove conditional Chatwoot router registration;
- `apps/worker/main.py`: remove Chatwoot actor registration after the broker-backlog safety gate;
- `apps/scheduler/main.py`: remove Chatwoot reconciliation sweep construction;
- `src/social_reply/application/event_ingestion/raw_recovery.py`: remove
  `chatwoot_dispatch_context`, Chatwoot dispatch validation, and Chatwoot actor selection; preserve
  the versioned direct-event reservation, lease, retry, and dead-letter behavior unchanged.

Delete or rewrite tests:

- delete `tests/unit/test_chatwoot_client.py`;
- delete `tests/unit/test_chatwoot_signature.py`;
- delete `tests/unit/test_chatwoot_classify.py`;
- delete `tests/integration/test_chatwoot_reconcile.py`;
- delete `tests/integration/test_webhook_endpoint.py` if it contains no non-Chatwoot contract;
- remove Chatwoot-only cases from `tests/unit/test_health.py` and
  `tests/unit/test_raw_event_actors.py`;
- rewrite `tests/integration/test_raw_event_recovery.py` so only current direct dispatch kinds are
  accepted and old `kind="chatwoot"` metadata fails closed as invalid/dead rather than dispatching.

Required scenarios:

1. Direct webhook `RawEvent` commit-to-dispatch recovery still survives broker loss and worker
   lease expiry.
2. Persisted malformed or retired Chatwoot dispatch metadata cannot invoke a removed actor.
3. API startup and route enumeration succeed without importing any Chatwoot module.
4. Worker startup succeeds without an unknown or duplicate Actor registration.

### Unit 3: Remove Chatwoot decision semantics

Modify:

- `src/social_reply/application/reply_decision/persist.py`;
- `src/social_reply/application/reply_decision/jobs.py`;
- `src/social_reply/application/account_management/admin_console.py`;
- `src/social_reply/domain/reply/decision.py`.

Decisions:

- Remove `ChatwootDecisionDeferred`.
- Replace `ensure_decision_delivery_available()` with an explicit active-delivery-mode validator or
  fold that validation into the existing account-scope load. Only exact `direct` is accepted.
- Remove `DEFERRED_CHATWOOT` from active job sets, status transitions, sweep recovery, health
  warnings, and logging.
- Direct `DRAFT` remains a durable `ReplyDecision` with no Outbox until explicit approval.
- Update the domain comment so `DRAFT` describes local review semantics rather than a Chatwoot
  private note.

Tests to rewrite:

- `tests/integration/test_decision_job_sweep.py`;
- `tests/integration/test_direct_actor_status.py`;
- `tests/integration/test_processor_decision.py`;
- `tests/integration/test_decision_generation_fencing.py`;
- `tests/integration/test_knowledge_retrieval.py`;
- `tests/integration/test_reviewed_localization.py`;
- `tests/integration/test_reply_business_prompt_retirement.py`.

Required scenarios:

1. Direct decisions still complete and persist release, generation, Prompt, knowledge, and RAG
   provenance.
2. Direct drafts create no customer-facing Outbox before approval.
3. Unsupported delivery modes settle deterministically as `NEEDS_REVIEW`; they do not remain in a
   recoverable deferred state.
4. New inbound generations still supersede older jobs and cancel only eligible bot Outboxes.
5. Human-active and handoff states continue to produce terminal ignore/handoff behavior without
   Chatwoot.

### Unit 4: Remove Chatwoot delivery semantics

Modify:

- `src/social_reply/application/message_delivery/intents.py`;
- `src/social_reply/application/message_delivery/outbox.py`;
- `src/social_reply/application/message_delivery/sweep.py`;
- `src/social_reply/application/message_delivery/recovery.py` only where it writes new Chatwoot
  delivery evidence;
- `src/social_reply/application/account_management/provisioning.py` to stop writing the retained
  `chatwoot_inbox_id` field for new rows when the model default already leaves it null.

Decisions:

- `create_or_get_outbox_intent()` must derive a destination exclusively through
  `build_direct_reply_destination()`.
- Remove implicit `destination_type="chatwoot_conversation"`.
- Remove `ConversationMapping` target resolution, `get_chatwoot_client()`, private-note delivery,
  `CHATWOOT_DISABLED`, `NO_MAPPING` Chatwoot handling, and Chatwoot message-ID writes.
- Preserve all send-time tenant, account, capability, message-window, takeover, generation, Prompt,
  knowledge, kill-switch, idempotency, and ambiguity protections for direct senders.
- Retain legacy database model fields in C1, but do not create new values in them.

Tests to rewrite or delete:

- delete `tests/integration/test_end_to_end_delivery.py` if it is entirely Chatwoot-specific;
- rewrite `tests/integration/test_deliver_outbox.py` around direct sender fakes;
- rewrite `tests/integration/test_decision_enqueues_delivery.py` around
  `platform_message_id` rather than `chatwoot_message_id`;
- rewrite `tests/integration/test_takeover_cancels_outbox.py` with a direct destination;
- update `tests/integration/test_fetch_history.py` to retain generic historical-ID reading only if
  that compatibility is intentionally kept through C2;
- update `tests/integration/test_telegram_direct.py` and
  `tests/integration/test_x_dm_poll.py` only where they assert an obsolete Chatwoot field write.

Required scenarios:

1. Automated direct replies, approved drafts, manual replies, and system notices select the correct
   destination contract.
2. Direct private-note attempts remain impossible.
3. Provider connect failures are retryable; ambiguous post-dispatch failures remain manual-review
   cases; deterministic provider errors remain permanent-review cases.
4. Successful sends write `platform_message_id`, create one outbound `Message`, and preserve
   `source_outbox_id` idempotency.
5. Claim/takeover cancellation and final send-time revalidation retain current ordering and locks.

### Unit 5: Align fixtures and synthetic evaluation with the real architecture

Modify:

- `tests/conftest.py`: remove Chatwoot defaults; production-like test defaults must not enable a
  retired integration;
- `tests/integration/conftest.py`: delete `chatwoot_payload` and Chatwoot account/mapping helpers;
  replace shared decision/delivery setup with direct account, inbound `reply_target`, and sender
  fixtures;
- `src/social_reply/application/evaluation/contracts.py`: remove
  `EvaluationDeliverySurface.CHATWOOT` if no stored evaluation rows use it, otherwise mark it as a
  historical read value and prevent creation of new Chatwoot evaluations until C2;
- `tests/integration/test_evaluation_runner.py` and
  `tests/integration/test_evaluation_foundation_migration.py`: test only supported active surfaces
  while preserving published migration compatibility.

Decision for evaluation history:

- Query production `evaluation_decisions.delivery_surface` before changing the enum.
- If no `chatwoot` values exist, remove the runtime enum in C1 while leaving the historical database
  check constraint for C2.
- If values exist, retain a parse-only legacy enum value that cannot be selected by new workloads.

### Unit 6: Remove configuration and documentation contracts

Modify:

- `src/social_reply/shared/config.py`;
- `scripts/validate_railway_config.py`;
- `scripts/verify_migration_compatible_rollback.sh`;
- `.env.example`;
- `AGENTS.md`;
- `README.md`;
- `docs/architecture.md`;
- `docs/admin-control-plane.md`;
- `docs/configuration.md`;
- `docs/production-migration.md`;
- `docs/reliability-drills.md`.

Required changes:

- Remove all active `CHATWOOT_*` settings, defaults, validation, and three-role consistency rules.
- Remove Chatwoot from current connector and process-ownership descriptions.
- Describe direct platform transports and the local operations path as the only supported runtime.
- Preserve a short migration-history note explaining that Chatwoot columns remain temporarily for
  rollback and historical data compatibility until C2.
- Do not edit archived implementation plans to make history appear cleaner than it was.

## 6. Verification plan

### Focused checks during implementation

Run the smallest relevant test group after each unit:

- retirement/config/route/scheduler contract tests after Units 1-2;
- decision persistence, job sweep, generation fencing, draft review, and handoff tests after Unit 3;
- direct Outbox delivery, recovery, takeover, manual reply, and outbound history tests after Unit 4;
- evaluation and shared fixture tests after Unit 5.

Run targeted Ruff or compilation checks only for changed Python files when needed to keep the change
reviewable. The repository's GitHub Actions remain authoritative for full Ruff, empty-database
Alembic upgrade/check, full pytest, and the production `linux/amd64` image contract.

### Final local review before commit

- `git status --short`
- `git diff --check`
- inspect all deleted files and every remaining `chatwoot` reference;
- confirm remaining references are limited to published migrations, historical plans, intentional
  rollback history, or temporarily retained model columns;
- confirm `git diff --cached --check` before commit.

### CI acceptance

- Ruff passes.
- Empty test database upgrades to the unchanged head `f3a7c9e1b5d2` and `alembic check` passes.
- Full pytest passes with Chatwoot disabled and no Chatwoot credentials.
- Production image API/Worker/Scheduler entrypoint contracts pass.
- No test obtains coverage by restoring a hidden Chatwoot default.

### Optional post-deployment smoke, only when separately authorized

- API `/healthz` succeeds.
- API, Worker, and Scheduler run the same expected digest.
- `/webhooks/chatwoot` returns 404.
- One authorized direct inbound flow reaches local conversation/inbox state.
- One approved draft or manual reply reaches the provider through the direct Outbox path.
- Scheduler logs contain no Chatwoot sweep and Worker logs contain no Chatwoot actor registration
  or unknown-actor backlog.
- Production database remains free of new Chatwoot rows or IDs.

## 7. Release and rollback posture

C1 is a code-only release because it does not change Alembic metadata or the migration graph.

During the first deployed release:

- keep `CHATWOOT_ENABLED=false` as a harmless stale Railway variable through the immediate rollback
  window if desired;
- revoke and remove `CHATWOOT_API_TOKEN`, `CHATWOOT_WEBHOOK_SECRET`, and `CHATWOOT_BASE_URL` only
  after the new digest is healthy and direct smoke succeeds;
- retain the predecessor image and deployment IDs under the normal release contract;
- do not drop database fields, mappings, or historical IDs.

An application rollback to the predecessor remains schema-compatible. With
`CHATWOOT_ENABLED=false`, the predecessor also does not require a usable Chatwoot token or endpoint.

## 8. C2 entry criteria

Create a separate migration plan only after at least one stable release cycle proves:

- no old Worker image or broker message still reads the retired actor or fields;
- no audit, export, support process, or external system requires Chatwoot IDs;
- production and retained environments have zero Chatwoot rows and destinations;
- rollback no longer needs a predecessor that reads `ConversationMapping` or `chatwoot_*` columns.

C2 may then add one forward-only Alembic migration to remove the table, columns, indexes, status
values, and evaluation constraint values, with the migration-compatible rollback process described
in `docs/production-migration.md`.

## 9. Execution evidence

C1 implementation completed with C2 schema cleanup still deferred:

- RED retirement contract run: 4 failed.
- GREEN unit suite: 1329 passed.
- Focused integration runs: 246 passed, then 14 passed.
- Direct draft, delivery recovery, takeover, and channel regression run: 72 passed.
- Legacy schema and rollback-compatibility regression run: 34 passed.
- Complete Outbox delivery and initial RawEvent recovery run: 126 passed.
- Final retirement, stale-finalizer, RawEvent recovery, Outbox, and Admin health run: 135 passed.
- Security review RED/GREEN: the new send-time delivery-mode test failed with `SENT`, then passed
  after `DELIVERY_MODE_UNSUPPORTED` was added to the final direct-send validation.
- Retired RawEvent actor metadata and legacy destination fail-closed checks: 3 passed.
- Full test collection: 2117 tests collected.
- Full repository Ruff verification passed, `git diff --check` passed, and IDE diagnostics reported
  no remaining errors.
- Full pytest and production image build remain the responsibility of GitHub Actions under the
  repository's normal validation contract; no production release or Railway variable mutation was
  performed in this implementation session.

## 10. Principal risks and mitigations

### Risk: direct-path tests accidentally depended on Chatwoot fixtures

Mitigation: replace global Chatwoot defaults and shared seeded Chatwoot accounts first; require
explicit direct `reply_target` and sender fakes in feature tests.

### Risk: unknown historical delivery mode becomes silently sendable

Mitigation: exact `direct` allowlist only; all unknown values fail closed before Outbox creation.

### Risk: stale Redis actor message appears after Actor removal

Mitigation: verify the broker backlog before removal. If it cannot be proven empty, keep a bounded
tombstone Actor for one release that validates the referenced RawEvent and exits without external
I/O, then remove it in the following release.

### Risk: schema cleanup makes rollback unsafe

Mitigation: no schema cleanup in C1. Treat C2 as a coordinated migration release with its own plan.

### Risk: obsolete credentials remain exploitable

Mitigation: revoke the token and webhook secret after C1 health verification; do not merely leave
the bridge disabled indefinitely.
