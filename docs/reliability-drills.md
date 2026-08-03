# Reliability fault drills

These drills exercise the durable recovery boundaries without adding deployment services or using a
production database. Pytest refuses database names that do not end in `_test`.

## Run the focused matrix

```bash
uv run pytest -q \
  tests/integration/test_raw_event_recovery.py \
  tests/unit/test_raw_event_actors.py \
  tests/integration/test_processor.py \
  tests/integration/test_decision_job_sweep.py \
  tests/integration/test_decision_generation_fencing.py \
  tests/integration/test_decision_generation_migration.py \
  tests/integration/test_provisioning_jobs.py \
  tests/integration/test_deliver_outbox.py \
  tests/integration/test_outbox_sweep.py \
  tests/integration/test_takeover_cancels_outbox.py \
  tests/integration/test_human_operations.py \
  tests/integration/test_message_history_migration.py \
  tests/integration/test_admin_console.py \
  tests/integration/test_schema.py \
  tests/integration/test_poll_sync.py \
  tests/integration/test_x_dm_poll.py \
  tests/integration/test_xchat_poll.py \
  tests/integration/test_xchat_recovery.py \
  tests/unit/test_runtime_feature_flags.py \
  tests/unit/test_scheduler.py \
  tests/unit/test_feishu_adapter.py \
  tests/unit/test_feishu_provisioning.py \
  tests/unit/test_feishu_security.py \
  tests/unit/test_feishu_sender.py \
  tests/integration/test_feishu_health.py \
  tests/integration/test_feishu_platform_migration.py \
  tests/integration/test_feishu_provisioning_integration.py \
  tests/integration/test_feishu_webhook.py
```

The eight dedicated Feishu files collect 60 cases (39 unit + 21 integration). Cross-platform suites
contain additional Feishu assertions, so 60 is not a repository-wide platform assertion count.

Run the full validation before release:

```bash
uv run ruff check .
uv run pytest -q
uv run alembic check
```

## Drill matrix

| Boundary | Injected fault | Required outcome |
| --- | --- | --- |
| RawEvent initial dispatch | Broker send fails after RawEvent commit | Reservation is released with retry state; Scheduler redispatches from immutable `initial_dispatch` metadata. |
| RawEvent worker | Worker dies or a stale token resumes | Expired lease is reclaimed; stale token cannot normalize or finalize the event. |
| Decision generation (M1/M2) | M1's LLM call is blocked while a newer M2 inbound commits | M2 advances the generation without waiting for M1; M1 becomes `SUPERSEDED`, cannot persist a decision, and only M2 may create a current bot Outbox. |
| DecisionJob | Queue message is lost or `PROCESSING` becomes stale | Scheduler re-enqueues the durable job with a fresh random `claim_token`; a stale token cannot finalize after reclaim. |
| DecisionJob | Pipeline fails eight times or an old worker returns after lease recovery | Job and linked RawEvent become operator-visible `NEEDS_REVIEW` / `DECISION_NEEDS_REVIEW`; the attempt fence rejects stale finalization and no ninth retry is scheduled. |
| Decision migration | A legacy writer omits generation fields during the mixed-version window | Compatibility triggers reserve the inbound generation, attach it to the job/decision, and reject a stale legacy decision writer. |
| RawEvent aggregation | Two DecisionJob finalizers aggregate concurrently, including `SUPERSEDED` work | RawEvent locking produces one deterministic terminal status; `SUPERSEDED` counts as settled and priority ordering is preserved. |
| ProvisioningJob | Platform gate closes | Job pauses without consuming an attempt and resumes after coordinated re-enable. |
| ProvisioningJob | Retryable provider failure or stale worker reaches attempt eight | Job becomes `NEEDS_ACTION` with `RETRY_EXHAUSTED`; an old attempt cannot overwrite a newer owner. An explicit operator retry or credential resubmission starts a new attempt budget. |
| ProvisioningJob | XChat worker dies after receiving a PIN | Stored PIN is removed and the operator must resubmit it. |
| Feishu ingress dispatch | Redis queue delivery is lost after a valid callback commits | Scheduler rebuilds dispatch only from the immutable account-scoped RawEvent contract; one normalized event and decision lineage results. |
| Feishu duplicate event | The same `im.message.receive_v1` callback is delivered more than once | RawEvent evidence may record delivery attempts as designed, while normalized/provider event idempotency prevents duplicate messages, decisions and Outboxes. |
| Feishu callback security | Signature is stale/replayed, a required `X-Lark-*` header is invalid, or token/App ID/AES validation fails | Request is rejected and no RawEvent is stored; a valid URL-verification challenge remains side-effect free. |
| HumanWork tenant migration | Legacy work tenant or claim attribution disagrees with the Conversation tenant | Tenant ownership is repaired; invalid claims return to `WAITING`, assignment fields clear, and `version` advances so stale inbox mutations fail. |
| Local inbox manual send | Operator replies from the built-in inbox to an explicit inbound target | A tenant-scoped claim/create and `HUMAN_ACTIVE` transition commit with a `MANUAL_REPLY/ADMIN_HUMAN` Outbox; successful delivery writes an outbound `Message.source_outbox_id` without requiring a ReplyDecision. Direct accounts need no Chatwoot mapping; Chatwoot-routed accounts still fail closed without one. |
| Outbox | First broker dispatch in a sweep fails | Later durable rows are still dispatched; the failed row remains eligible for the next sweep. |
| Outbox | A newer inbound arrives while an older bot decision is pending or failed | Only stale `DECISION/BOT` rows become `CANCELLED/STALE_CONVERSATION_INPUT`; manual replies and draft approvals survive, and delivery rechecks generation before provider I/O. |
| Outbox | Human takeover or actor cancellation races an external send | One conversation advisory lock orders the operations: committed takeover prevents a new send; cancellation drains the bounded provider call and durably records success, failure, or ambiguity before takeover commits. |
| Outbox | Connect failure or connect timeout | The attempt is retryable because no request was established; duplicate actor messages cannot bypass `next_attempt_at` backoff. |
| Outbox | Read timeout, transport ambiguity, unknown post-dispatch failure, or provider 5xx | Row becomes `NEEDS_REVIEW`; the system does not automatically risk a duplicate send. |
| Feishu ambiguous send | Feishu accepts a request but the response is lost, malformed or returns a post-dispatch 5xx | The row becomes `NEEDS_REVIEW/AMBIGUOUS_SEND`; the stable Outbox UUID is retained and no blind retry risks a duplicate reply. |
| Feishu token refresh | Send returns provider code `99991663` | The sender invalidates the cached tenant token, refreshes once and repeats the request with the same Outbox UUID. |
| Feishu health gate | Bot becomes inactive, identity drifts, credentials fail, or health is non-ready | Scheduler records sanitized health; sends pause without consuming an attempt and resume after a later `READY` inspection. |
| Outbox | Retryable send reaches attempt five | Row becomes `NEEDS_REVIEW`; no sixth automatic send is scheduled. |
| X polling | Lease contention, page cap, invalid resume token, pagination failure, or decrypt gap | Stable checkpoint does not advance; the fenced run resumes or falls back from the stable checkpoint. |
| XChat webhook recovery | Worker lease expires eight times | RawEvent becomes `XCHAT_RETRY_EXHAUSTED` and stops automatic replay. |
| Scheduler | One sweep raises or submission fails | Other due sweeps still run and the failed runtime becomes eligible for a later interval. |
| Scheduler lanes | An inspection sweep remains slow across several core intervals | The inspection sweep stays max-one-instance and emits one soft warning; core recovery continues recurring independently and missed inspection ticks coalesce. |
| Scheduler future/shutdown | A submitted Future is cancelled/fails, or shutdown begins with work in flight | Completion is observed and runtime state clears; shutdown uses a bounded five-second grace snapshot and does not hard-cancel provider work. |

## Release invariants

A drill run is acceptable only when all of these remain true:

- Durable rows are committed before broker dispatch and can be recovered without reconstructing
  arguments from provider payloads.
- Claims are token-, lease-, or attempt-fenced; stale workers cannot overwrite a newer owner.
- Decision LLM/network work holds no database transaction, and only the current generation plus current claim token may finalize.
- Business-event and Outbox idempotency prevent duplicate durable effects under at-least-once
  dispatch.
- An automated public send cannot begin after `HUMAN_ACTIVE` takeover has committed or after its decision generation becomes stale.
- Local human work, explicit reply targets and manual-send provenance remain durable and tenant-scoped; direct-account operations do not require Chatwoot, while configured Chatwoot destinations still require their mapping.
- Ambiguous external-send outcomes stop in an operator-visible state instead of being retried.
- Feature flags pause accepted durable work without consuming attempts and recover it after a
  coordinated API, Worker, and Scheduler restart; Feishu URL-verification remains available while
  disabled, while valid normal events are acknowledged as sanitized ignored evidence.
- One failed item or sweep does not prevent unrelated tenants, accounts, conversations, or recovery
  stages from progressing; slow inspection work does not serialize core recovery.
- Scheduler sweeps are max-one-instance and coalescing; warning thresholds are observability signals,
  not hard cancellation deadlines.

Connector send timeouts are at most 20 seconds, below the 30-second cancellation drain and the
120-second actor-loop timeout. Keep that ordering when adding or changing a connector.

The Admin overview summarizes unresolved ingestion, decision, delivery, provisioning, sync, and
account states. It is an operational signal, not a replacement for the fault drills or provider-side
delivery verification.
