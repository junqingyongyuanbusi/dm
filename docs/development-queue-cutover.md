# One-off development workspace queue cutover

This is an operator-only database maintenance step, not a deployment command or
a production migration procedure. Run only in the explicitly authorized development
instance, after the API has prepared **exactly `c6f2a9d4e810`**. Never run pytest
against that instance. The CLI does not migrate schema, deploy, manage Redis, delete
business rows, or change password hashes, credentials, account defaults or sessions.

## Required ordering

1. Wait for the target SHA's CI to pass and record the old deployment IDs/digest.
   The owner has explicitly deferred an additional backup and full rollback engineering
   for this development rollout; see `production-migration.md`. This is not permission
   to delete existing data. Only API prepares schema.
2. Stop ingress/admin writes, API background processing, workers, schedulers and
   manual maintenance actors. Wait for old processes and external requests to stop.
   A kill switch alone is not proof of quiescence. Pick a UTC cutoff **after** the
   final old write, not in the future, and one UUID for the entire cutover.
3. Arrange a fresh, isolated working broker before resuming. **Do not reconnect old
   queued/delayed/dead-letter actor messages to new workers.** Set the same new
   `DRAMATIQ_NAMESPACE` on all three roles after old processes have stopped. This
   isolates broker keys only; retain the existing Redis URL, security configuration
   and kill-switch keys, and verify the new namespace has not been used before.
   Do not flush Redis or delete old evidence to accomplish this.
4. Preview with the two complete, operator-confirmed legacy administrator UUIDs:

   ```bash
   uv run python -m apps.cli.retire_workspace_queue \
     --tenant default --cutover-id "$CUTOVER_ID" --before "$CUTOFF_UTC" \
     --restore-admin-id "$FIRST_ADMIN_UUID" --restore-admin-id "$SECOND_ADMIN_UUID"
   ```

   No real UUID or credential is embedded in this document. Preview uses a read-only
   repeatable-read transaction and writes no audit. Review every count and the
   exact administrator IDs/old roles; missing, disabled, cross-tenant or roles other
   than `AGENT`, `USER`, `WORKSPACE_ADMIN` are refused.
5. While all processing remains stopped, rerun the identical arguments with
   `--apply --confirm-processes-stopped`. Retain the JSON report. A 3-second lock
   timeout and 30-second per-statement timeout bound contention; transaction/table
   locks only protect the maintenance transaction and **do not replace step 2**.
   Same cutover UUID and normalized parameters return the original audit/counts
   without repeating changes; different parameters are refused. After an ambiguous
   connection failure, retry the same UUID, never invent another one.
6. Resume only on the verified fresh broker. Review unknown sends manually using
   provider evidence; never bulk-retry them. Validate role access, retained data,
   health and the three-role digest under the release owner's procedure.

## Database semantics and limits

Only records strictly older than the cutoff are selected (`created_at`, RawEvent
`received_at`; automation additionally checks `updated_at`). Admin restoration is
the explicit UUID-scoped exception. Pending/failed outboxes are cancelled; sending
and review-required outboxes become `NEEDS_REVIEW` with
`WORKSPACE_QUEUE_CUTOVER_UNKNOWN`, preventing feature-reenable sweep recovery.
Decision jobs are superseded, pending drafts rejected, open human work cancelled
with version increments, and their old human automation reset to `BOT_DRAFT_ONLY`
with human owner/timer state cleared. Provisioning claims are invalidated by attempt
increments; durable account/configuration/staging evidence is retained, not undone.
Notification pending work is cancelled; unknown sends become `NEEDS_REVIEW` with
claims cleared (including card updates, which stale-SENDING sweep otherwise retries).
Old notification action nonces expire, including already-synced cards.

Unprocessed RawEvents remain evidence with `RETIRED_BY_CUTOVER` and no claim token,
not a fabricated `PROCESSED`. Current initial-dispatch and XChat recovery/claim
allowlists exclude that status. **Legacy tokenless `process_direct_event` does not
check RawEvent terminal status**: this CLI cannot make replay of the old broker
safe. Fresh broker isolation is a hard prerequisite, not an optional optimization.
Raw events with no attributable tenant/account appear as `unscoped_raw_blockers`
in preview and block apply; investigate their ownership rather than guessing it.

Messages, normalized-event deduplication, contacts, knowledge, credentials,
platform checkpoints and sync gaps are untouched. Open gaps may later download
older messages; existing deduplication remains intact, but previously unseen old
messages may still create new work. This is not a promise to suppress all historical
provider traffic. The audit stores exact parameters, administrator transitions and
per-category counts; it is not an automatic rollback facility.

New unit/integration tests are picked up by existing CI. No local tests, Ruff,
compile checks or Railway actions were run while authoring this maintenance step.
