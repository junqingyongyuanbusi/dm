# One-off development workspace queue cutover

This is an explicitly authorized development-only operation. The Railway environment
is named `production`, but the owner has identified this instance as development and
authorized a bounded receiving/sending outage. The default release remains unchanged.
The three application roles share one immutable image; Postgres and Redis remain
running and colocated. Never run pytest against the instance.

## Integrated release command

After reviewing the two existing active administrator UUIDs, and only from a clean
`dev` checkout equal to `origin/dev`, use the existing release entrypoint:

```bash
scripts/publish_railway_release.sh --fresh-queue \
  --restore-admin-id "$FIRST_ADMIN_UUID" \
  --restore-admin-id "$SECOND_ADMIN_UUID"
```

Exactly two distinct UUIDs are required; no username guessing, password reset or
new administrator creation occurs. All existing CI, immutable-image, migration
compatibility, configuration, auto-update, digest and colocation gates still apply.
There is no skip-CI option. A legacy review-Outbox capability bridge cannot be mixed
with this minimal path; complete that separately before attempting fresh queue.
The retirement CLI's pinned schema must equal the target migration head.

The script records predecessor deployments/digest and cutover metadata in the same
`dist/release-<full-sha>.json`, then stops Scheduler, Worker and API using explicit
project/environment/service arguments. Every predecessor and every service deployment
must report `deploymentStopped=true`, with no unresolved/unknown runtime or incomplete
deployment-list page. It records UTC `before` with microseconds only after all stops.
The operator must also stop manual maintenance actors and ensure outstanding external
requests have settled; Railway stop evidence cannot retract provider-side sends.

It assigns all three roles one UUID-derived `DRAMATIQ_NAMESPACE` and an identical
non-secret `WORKSPACE_QUEUE_CUTOVER` JSON envelope using `variable set --skip-deploys`.
The envelope contains `cutover_id`, `before`, `tenant`, `restore_admin_ids`, `namespace`
and the explicit boolean `processes_stopped`. Unknown/duplicate fields, invalid types,
wrong roles, missing confirmation or namespace disagreement fail closed before startup.
Do not set this variable by hand to bypass stopped-process evidence.

After normal image promotion, API starts first. Following database preparation but
before HTTP it checks that no Redis key exists at the new namespace or below its prefix,
then reuses the existing locked retirement transaction. It does not flush Redis or
touch old broker/security/kill-switch keys. The same atomic audit includes
`startup_contract=fresh-queue-v1` and the namespace. Worker/Scheduler readiness requires
this exact committed audit before consuming. API restarts with the same envelope skip
retirement and the emptiness check only when that startup-specific audit matches;
ordinary standalone CLI audits are not sufficient evidence. Password hashes, credentials,
business history and platform cursors retain the semantics described below.

The rollout only succeeds after API health, all three runtime digests, shared variables
and all five services' colocation are verified. Keep the envelope on all three roles
for same-cutover restart safety; do not point any role back at the old namespace.

### Failure and recovery boundary

This minimal integration **does not automatically resume an interrupted fresh release**.
Once a manifest exists, another fresh invocation (or a default invocation using a fresh
manifest for the same SHA) fails before deployment mutations. The manifest retains phases,
predecessors, previous namespaces, the one cutover UUID and the cutoff once known.
Never delete/edit it to force a new attempt, never invent another UUID, and never clear
the startup envelope to force normal startup. A partially written variable set must not
be followed by starting consumers.

An operator must reconcile the saved manifest with remote stopped/deployment evidence,
all three variables, immutable registry digests and the database audit before authorizing
a reviewed recovery. If no audit exists, preserve all stops and reuse the same envelope;
if the exact startup audit exists, do not re-retire business data or demand an empty active
queue. A restart of the already-published matching image can use that audit, but restart
is not a substitute for deploying new code. No automated rollback is provided. In
particular, the retained predecessor image may lack the startup gate; the generic
rollback script alone is not a reviewed fresh-cutover recovery procedure. With migrations,
follow `production-migration.md` and retain the compatible rollback image.

## Standalone database maintenance CLI

The older standalone CLI remains available for explicitly reviewed maintenance, but
is not an alternative release entrypoint and does not emit the integrated startup proof.
Run only after API has prepared **exactly `c6f2a9d4e810`**. The CLI does not migrate
schema, deploy, manage Redis, delete business rows, or change password hashes,
credentials, account defaults or sessions. Do not mix its audit ID with an integrated
startup cutover.

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

New unit/integration tests are picked up by existing CI, including startup audit
binding, namespace occupation/failure, role gating and mocked stopped-deployment
query failures. The Dockerfile copies the startup module and runs its unconfigured
validation/import smoke during the CI image build; no database or Redis is needed
for that build check. The actual cutover transaction is not run during image build.
No local tests, Ruff, compile checks or Railway actions were run while authoring
this maintenance step; static review is not evidence that CI has passed.
