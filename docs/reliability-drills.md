# Reliability fault drills

These drills exercise the durable recovery boundaries without adding deployment services or using a
production database. Pytest refuses database names that do not end in `_test`.

## Run the focused matrix

```bash
uv run pytest -q \
  tests/integration/test_raw_event_recovery.py \
  tests/unit/test_raw_event_actors.py \
  tests/integration/test_decision_job_sweep.py \
  tests/integration/test_provisioning_jobs.py \
  tests/integration/test_deliver_outbox.py \
  tests/integration/test_outbox_sweep.py \
  tests/integration/test_takeover_cancels_outbox.py \
  tests/integration/test_poll_sync.py \
  tests/integration/test_x_dm_poll.py \
  tests/integration/test_xchat_poll.py \
  tests/integration/test_xchat_recovery.py \
  tests/unit/test_runtime_feature_flags.py
```

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
| DecisionJob | Queue message is lost or `PROCESSING` becomes stale | Scheduler re-enqueues the durable job. |
| DecisionJob | Pipeline fails eight times or an old worker returns after lease recovery | Job and linked RawEvent become operator-visible `NEEDS_REVIEW` / `DECISION_NEEDS_REVIEW`; the attempt fence rejects stale finalization and no ninth retry is scheduled. |
| ProvisioningJob | Platform gate closes | Job pauses without consuming an attempt and resumes after coordinated re-enable. |
| ProvisioningJob | Retryable provider failure or stale worker reaches attempt eight | Job becomes `NEEDS_ACTION` with `RETRY_EXHAUSTED`; an old attempt cannot overwrite a newer owner. An explicit operator retry or credential resubmission starts a new attempt budget. |
| ProvisioningJob | XChat worker dies after receiving a PIN | Stored PIN is removed and the operator must resubmit it. |
| Outbox | First broker dispatch in a sweep fails | Later durable rows are still dispatched; the failed row remains eligible for the next sweep. |
| Outbox | Human takeover or actor cancellation races an external send | One conversation advisory lock orders the operations: committed takeover prevents a new send; cancellation drains the bounded provider call and durably records success, failure, or ambiguity before takeover commits. |
| Outbox | Connect failure or connect timeout | The attempt is retryable because no request was established; duplicate actor messages cannot bypass `next_attempt_at` backoff. |
| Outbox | Read timeout, transport ambiguity, unknown post-dispatch failure, or provider 5xx | Row becomes `NEEDS_REVIEW`; the system does not automatically risk a duplicate send. |
| Outbox | Retryable send reaches attempt five | Row becomes `NEEDS_REVIEW`; no sixth automatic send is scheduled. |
| X polling | Lease contention, page cap, invalid resume token, pagination failure, or decrypt gap | Stable checkpoint does not advance; the fenced run resumes or falls back from the stable checkpoint. |
| XChat webhook recovery | Worker lease expires eight times | RawEvent becomes `XCHAT_RETRY_EXHAUSTED` and stops automatic replay. |
| Scheduler | One sweep raises | Remaining sweeps in the same cycle still run. |

## Release invariants

A drill run is acceptable only when all of these remain true:

- Durable rows are committed before broker dispatch and can be recovered without reconstructing
  arguments from provider payloads.
- Claims are token-, lease-, or attempt-fenced; stale workers cannot overwrite a newer owner.
- Business-event and Outbox idempotency prevent duplicate durable effects under at-least-once
  dispatch.
- An automated public send cannot begin after `HUMAN_ACTIVE` takeover has committed.
- Ambiguous external-send outcomes stop in an operator-visible state instead of being retried.
- Feature flags pause accepted durable work without consuming attempts and recover it after a
  coordinated API, Worker, and Scheduler restart.
- One failed item or sweep does not prevent unrelated tenants, accounts, conversations, or recovery
  stages from progressing.

Connector send timeouts are at most 20 seconds, below the 30-second cancellation drain and the
120-second actor-loop timeout. Keep that ordering when adding or changing a connector.

The Admin overview summarizes unresolved ingestion, decision, delivery, provisioning, sync, and
account states. It is an operational signal, not a replacement for the fault drills or provider-side
delivery verification.
