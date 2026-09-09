#!/usr/bin/env bash
set -Eeuo pipefail

readonly IMAGE_REPO="ghcr.io/junqingyongyuanbusi/reply-core"
readonly RAILWAY_PROJECT_ID="abcf3199-e5ac-415b-a22e-062206390331"
readonly RAILWAY_PROJECT_NAME="reply-core"
readonly RAILWAY_ENVIRONMENT="production"
readonly RAILWAY_ENVIRONMENT_ID="db0d6750-eb77-40ee-8a79-f706cd1f828a"
readonly RAILWAY_REGION="us-east4-eqdc4a"
readonly PUBLIC_BASE_URL="https://relay.nexory.top"
readonly SOURCE_URL="https://github.com/junqingyongyuanbusi/dm"
readonly LEGACY_REVIEW_OUTBOX_CAPABILITY="legacy"
readonly TARGET_REVIEW_OUTBOX_CAPABILITY="review-outbox-dual-read-v1"
readonly LEGACY_BUSINESS_PROMPT_CAPABILITY="legacy"
readonly TARGET_BUSINESS_PROMPT_CAPABILITY="editable-business-prompt-v1"
readonly DEPLOY_TIMEOUT_SECONDS="${DEPLOY_TIMEOUT_SECONDS:-900}"
readonly CI_TIMEOUT_SECONDS="${CI_TIMEOUT_SECONDS:-1200}"
readonly RAILWAY_SERVICES=(api worker scheduler)
readonly RAILWAY_COLOCATED_SERVICES=(api worker scheduler Postgres Redis)

usage() {
  cat <<'EOF'
Verify the CI-published immutable GHCR image, prepare rollback evidence, promote latest, and deploy the same digest to Railway.

Required state:
  - current branch is dev
  - worktree is clean
  - HEAD equals origin/dev
  - every GitHub Actions CI run for HEAD succeeded
  - GHCR, GitHub CLI, and Railway CLI authentication are available

Fixed production targets:
  - GHCR: ghcr.io/junqingyongyuanbusi/reply-core
  - Railway: reply-core / production / api + worker + scheduler
  - Public API: https://relay.nexory.top

Optional environment variables:
  DEPLOY_TIMEOUT_SECONDS     Per-service deployment timeout (default: 900)
  CI_TIMEOUT_SECONDS         CI wait timeout (default: 1200)

Explicit development-only cutover (stops API/Worker/Scheduler):
  --fresh-queue --restore-admin-id UUID --restore-admin-id UUID
  Requires two distinct existing admin IDs. Preserves business data and passwords.
  Does not stop Postgres/Redis. See docs/development-queue-cutover.md for recovery limits.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi
log() {
  printf '[release] %s\n' "$*" >&2
}

fail() {
  printf '[release] ERROR: %s\n' "$*" >&2
  exit 1
}

fresh_queue="false"
fresh_queue_metadata='null'
restore_admin_ids=()
restore_admin_count=0
expect_restore_id="false"
for argument in "$@"; do
  if [[ "$expect_restore_id" == "true" ]]; then
    [[ "$argument" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]] \
      || fail "--restore-admin-id requires a UUID"
    restore_admin_ids+=("$argument")
    restore_admin_count=$((restore_admin_count + 1))
    expect_restore_id="false"
    continue
  fi
  case "$argument" in
    --fresh-queue)
      [[ "$fresh_queue" == "false" ]] || fail "duplicate --fresh-queue"
      fresh_queue="true"
      ;;
    --restore-admin-id) expect_restore_id="true" ;;
    *) fail "unknown release argument: $argument" ;;
  esac
done
[[ "$expect_restore_id" == "false" ]] || fail "missing --restore-admin-id value"
if [[ "$fresh_queue" == "true" ]]; then
  [[ "$restore_admin_count" -eq 2 ]] || fail "fresh queue requires two distinct admin IDs"
elif [[ "$restore_admin_count" -ne 0 ]]; then
  fail "--restore-admin-id requires --fresh-queue"
fi

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

for command_name in git docker railway gh jq curl date awk python3 uv; do
  require_command "$command_name"
done
for timeout_name in DEPLOY_TIMEOUT_SECONDS CI_TIMEOUT_SECONDS; do
  timeout_value="${!timeout_name}"
  [[ "$timeout_value" =~ ^[1-9][0-9]*$ ]] \
    || fail "$timeout_name must be a positive decimal integer"
done

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || fail "not inside a Git repository"
cd "$repo_root"
mkdir -p .run dist
release_lock="$repo_root/.run/publish-railway-release.lock"
script_path="$repo_root/scripts/publish_railway_release.sh"
if [[ "${DM_RELEASE_LOCK_HELD:-}" != "1" ]]; then
  exec python3 - "$release_lock" "$script_path" "$@" <<'PY'
import fcntl
import os
import sys

lock_path, script_path, *args = sys.argv[1:]
with open(lock_path, "a+", encoding="ascii") as lock_file:
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"[release] ERROR: another local Railway release or rollback is running: {lock_path}", file=sys.stderr)
        raise SystemExit(1)
    os.set_inheritable(lock_file.fileno(), True)
    environment = dict(os.environ)
    environment["DM_RELEASE_LOCK_HELD"] = "1"
    environment["DM_RELEASE_LOCK_FD"] = str(lock_file.fileno())
    os.execve(script_path, [script_path, *args], environment)
PY
else
  lock_fd="${DM_RELEASE_LOCK_FD:-}"
  [[ "$lock_fd" =~ ^[0-9]+$ ]] || fail "invalid inherited release lock descriptor"
  python3 - "$lock_fd" "$release_lock" <<'PY'
import fcntl
import os
import sys

fd = int(sys.argv[1])
lock_path = sys.argv[2]
try:
    fd_stat = os.fstat(fd)
    path_stat = os.stat(lock_path)
    if (fd_stat.st_dev, fd_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
        raise OSError("inherited descriptor does not match release lock")
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except (OSError, BlockingIOError) as exc:
    print(f"[release] ERROR: invalid inherited release lock: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
fi

branch="$(git branch --show-current)"
[[ "$branch" == "dev" ]] || fail "release branch must be dev, got: ${branch:-detached}"

revalidate_dev_head() {
  [[ -z "$(git status --porcelain)" ]] || fail "worktree must be clean before release"
  git fetch --quiet origin dev
  local head_sha origin_sha
  head_sha="$(git rev-parse HEAD)"
  origin_sha="$(git rev-parse origin/dev)"
  [[ "$head_sha" == "$origin_sha" ]] || fail "HEAD must equal current origin/dev before release"
  [[ "$head_sha" == "$full_sha" ]] || fail "HEAD changed during release"
}

full_sha="$(git rev-parse HEAD)"
[[ "$full_sha" =~ ^[0-9a-f]{40}$ ]] || fail "invalid Git SHA: $full_sha"
short_sha="${full_sha:0:12}"
sha_ref="${IMAGE_REPO}:${full_sha}"
latest_ref="${IMAGE_REPO}:latest"
rollback_ref="${IMAGE_REPO}:railway-pre-${short_sha}"
rollback_compatible_ref="${IMAGE_REPO}:railway-compat-pre-${short_sha}"
manifest_path="dist/release-${full_sha}.json"
revalidate_dev_head
if [[ -f "$manifest_path" ]]; then
  jq -e . "$manifest_path" >/dev/null || fail "existing release manifest is invalid JSON"
  if [[ "$fresh_queue" == "true" ]] \
    || jq -e '.fresh_queue != null' "$manifest_path" >/dev/null; then
    fail "fresh queue recovery requires explicit operator review of $manifest_path; do not delete it or generate a new cutover ID"
  fi
fi
if [[ "$fresh_queue" == "true" ]]; then
  fresh_queue_metadata="$(python3 - "${restore_admin_ids[@]}" <<'PY'
import json
import sys
import uuid

admin_ids = sorted({str(uuid.UUID(value)) for value in sys.argv[1:]})
if len(admin_ids) != 2:
    raise SystemExit("fresh queue requires two distinct admin IDs")
cutover_id = uuid.uuid4()
print(json.dumps({"phase": "planned", "previous_namespaces": {}, "cutover": {
    "cutover_id": str(cutover_id), "namespace": f"dramatiq-cutover-{cutover_id.hex}",
    "tenant": "default", "restore_admin_ids": admin_ids,
    "before": None, "processes_stopped": False,
}}))
PY
  )" || fail "invalid fresh queue arguments"
fi

wait_for_ci() {
  local deadline=$((SECONDS + CI_TIMEOUT_SECONDS))
  local runs
  while (( SECONDS < deadline )); do
    if ! runs="$(gh run list --commit "$full_sha" --workflow CI --limit 20 \
      --json databaseId,status,conclusion,url)"; then
      fail "GitHub CLI could not read CI runs for $full_sha"
    fi
    if jq -e 'length > 0 and all(.[]; .status == "completed" and .conclusion == "success")' \
      >/dev/null <<<"$runs"; then
      jq -r '.[] | "[release] CI success: \(.databaseId) \(.url)"' <<<"$runs"
      return 0
    fi
    if jq -e 'any(.[]; .status == "completed" and .conclusion != "success")' \
      >/dev/null <<<"$runs"; then
      jq -r '.[] | "[release] CI \(.status)/\(.conclusion): \(.databaseId) \(.url)"' \
        <<<"$runs" >&2
      fail "at least one CI run failed for $full_sha"
    fi
    sleep 10
  done
  fail "timed out waiting for CI on $full_sha"
}

image_state() {
  local reference="$1"
  local output status
  set +e
  output="$(docker buildx imagetools inspect "$reference" 2>&1)"
  status=$?
  set -e
  if [[ $status -eq 0 ]]; then
    printf '%s\n' "exists"
    return 0
  fi
  if grep -Eqi 'not found|manifest unknown|name unknown' <<<"$output"; then
    printf '%s\n' "absent"
    return 0
  fi
  printf '[release] ERROR: could not inspect %s: %s\n' "$reference" "$output" >&2
  return 1
}

image_digest() {
  local reference="$1"
  local output digest
  if ! output="$(docker buildx imagetools inspect "$reference" 2>&1)"; then
    fail "could not inspect image digest for $reference: $output"
  fi
  digest="$(awk '/^Digest:/ {print $2; exit}' <<<"$output")"
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fail "invalid registry digest for $reference: ${digest:-missing}"
  printf '%s\n' "$digest"
}

image_metadata() {
  docker buildx imagetools inspect "$1" --format '{{json .Image}}'
}

image_revision() {
  jq -r '.config.Labels["org.opencontainers.image.revision"] // ""' \
    <<<"$(image_metadata "$1")"
}

image_review_outbox_capability() {
  local reference="$1"
  local capability
  capability="$(jq -r \
    '.config.Labels["com.nexory.reply-core.review-outbox-contract"] // "legacy"' \
    <<<"$(image_metadata "$reference")")"
  case "$capability" in
    "$LEGACY_REVIEW_OUTBOX_CAPABILITY"|"$TARGET_REVIEW_OUTBOX_CAPABILITY")
      printf '%s\n' "$capability"
      ;;
    *) fail "$reference has unknown review Outbox capability: ${capability:-missing}" ;;
  esac
}

image_business_prompt_capability() {
  local reference="$1"
  local capability
  capability="$(jq -r \
    '.config.Labels["com.nexory.reply-core.business-prompt-contract"] // "legacy"' \
    <<<"$(image_metadata "$reference")")"
  case "$capability" in
    "$LEGACY_BUSINESS_PROMPT_CAPABILITY"|"$TARGET_BUSINESS_PROMPT_CAPABILITY")
      printf '%s\n' "$capability"
      ;;
    *) fail "$reference has unknown business Prompt capability: ${capability:-missing}" ;;
  esac
}

verify_sha_image() {
  local reference="$1"
  local metadata revision source image_os architecture capability business_prompt_capability
  metadata="$(image_metadata "$reference")"
  revision="$(jq -r '.config.Labels["org.opencontainers.image.revision"] // ""' <<<"$metadata")"
  source="$(jq -r '.config.Labels["org.opencontainers.image.source"] // ""' <<<"$metadata")"
  image_os="$(jq -r '.os // ""' <<<"$metadata")"
  architecture="$(jq -r '.architecture // ""' <<<"$metadata")"
  capability="$(jq -r \
    '.config.Labels["com.nexory.reply-core.review-outbox-contract"] // ""' \
    <<<"$metadata")"
  business_prompt_capability="$(jq -r \
    '.config.Labels["com.nexory.reply-core.business-prompt-contract"] // ""' \
    <<<"$metadata")"
  [[ "$revision" == "$full_sha" ]] || fail "$reference has unexpected OCI revision: $revision"
  [[ "$source" == "$SOURCE_URL" ]] || fail "$reference has unexpected OCI source: $source"
  [[ "$capability" == "$TARGET_REVIEW_OUTBOX_CAPABILITY" ]] \
    || fail "$reference has unexpected review Outbox capability: ${capability:-missing}"
  [[ "$business_prompt_capability" == "$TARGET_BUSINESS_PROMPT_CAPABILITY" ]] \
    || fail "$reference has unexpected business Prompt capability: ${business_prompt_capability:-missing}"
  [[ "$image_os/$architecture" == "linux/amd64" ]] \
    || fail "$reference has unexpected platform: $image_os/$architecture"
}

verify_predecessor_image() {
  local reference="$1"
  local expected="$2"
  local metadata revision source image_os architecture digest
  digest="$(image_digest "$reference")"
  [[ "$digest" == "$expected" ]] || fail "$reference has unexpected digest: $digest"
  metadata="$(image_metadata "$reference")"
  revision="$(jq -r '.config.Labels["org.opencontainers.image.revision"] // ""' <<<"$metadata")"
  source="$(jq -r '.config.Labels["org.opencontainers.image.source"] // ""' <<<"$metadata")"
  image_os="$(jq -r '.os // ""' <<<"$metadata")"
  architecture="$(jq -r '.architecture // ""' <<<"$metadata")"
  [[ "$revision" =~ ^[0-9a-f]{40}$ ]] || fail "$reference has invalid app revision: $revision"
  [[ "$source" == "$SOURCE_URL" ]] || fail "$reference has unexpected OCI source: $source"
  [[ "$image_os/$architecture" == "linux/amd64" ]] \
    || fail "$reference has unexpected platform: $image_os/$architecture"
}

verify_rollback_compatible_image() {
  local reference="$1"
  local base_digest="$2"
  local app_revision="$3"
  local expected_capability="$4"
  local expected_database_head="$5"
  local expected_business_prompt_capability="$6"
  local metadata revision source image_os architecture purpose labeled_base labeled_target
  local database_head capability business_prompt_capability
  metadata="$(image_metadata "$reference")"
  revision="$(jq -r '.config.Labels["org.opencontainers.image.revision"] // ""' <<<"$metadata")"
  source="$(jq -r '.config.Labels["org.opencontainers.image.source"] // ""' <<<"$metadata")"
  purpose="$(jq -r '.config.Labels["com.nexory.reply-core.rollback-purpose"] // ""' <<<"$metadata")"
  labeled_base="$(jq -r '.config.Labels["com.nexory.reply-core.rollback-base-digest"] // ""' <<<"$metadata")"
  labeled_target="$(jq -r '.config.Labels["com.nexory.reply-core.rollback-target-release"] // ""' <<<"$metadata")"
  database_head="$(jq -r '.config.Labels["com.nexory.reply-core.database-head"] // ""' <<<"$metadata")"
  capability="$(jq -r \
    '.config.Labels["com.nexory.reply-core.review-outbox-contract"] // ""' \
    <<<"$metadata")"
  business_prompt_capability="$(jq -r \
    '.config.Labels["com.nexory.reply-core.business-prompt-contract"] // "legacy"' \
    <<<"$metadata")"
  image_os="$(jq -r '.os // ""' <<<"$metadata")"
  architecture="$(jq -r '.architecture // ""' <<<"$metadata")"
  [[ "$revision" == "$app_revision" ]] \
    || fail "$reference has unexpected predecessor app revision: $revision"
  [[ "$source" == "$SOURCE_URL" ]] || fail "$reference has unexpected OCI source: $source"
  [[ "$purpose" == "migration-compatible-predecessor" ]] \
    || fail "$reference has unexpected rollback purpose: $purpose"
  [[ "$labeled_base" == "$base_digest" ]] \
    || fail "$reference has unexpected rollback base digest: $labeled_base"
  [[ "$labeled_target" == "$full_sha" ]] \
    || fail "$reference has unexpected target release: $labeled_target"
  [[ "$database_head" == "$expected_database_head" ]] \
    || fail "$reference has unexpected database head: $database_head"
  [[ "$capability" == "$expected_capability" ]] \
    || fail "$reference has unexpected review Outbox capability: ${capability:-missing}"
  [[ "$business_prompt_capability" == "$expected_business_prompt_capability" ]] \
    || fail "$reference has unexpected business Prompt capability: ${business_prompt_capability:-missing}"
  [[ "$image_os/$architecture" == "linux/amd64" ]] \
    || fail "$reference has unexpected platform: $image_os/$architecture"
}

require_ci_rollback_compatible_image() {
  local reference="$1"
  local base_digest="$2"
  local app_revision="$3"
  local base_capability="$4"
  local expected_database_head="$5"
  local base_business_prompt_capability="$6"
  local state digest
  state="$(image_state "$reference")"
  [[ "$state" == "exists" ]] \
    || fail "CI did not publish required migration-compatible image: $reference"
  verify_rollback_compatible_image \
    "$reference" "$base_digest" "$app_revision" "$base_capability" \
    "$expected_database_head" "$base_business_prompt_capability"
  digest="$(image_digest "$reference")"
  [[ "$digest" != "$expected_digest" ]] \
    || fail "rollback-compatible image unexpectedly matches target digest"
  printf '%s\n' "$digest"
}

migration_graph_changed() {
  local predecessor_revision="$1"
  git cat-file -e "${predecessor_revision}^{commit}" 2>/dev/null \
    || fail "predecessor commit is not available locally: $predecessor_revision"
  ! git diff --quiet "$predecessor_revision" "$full_sha" -- migrations/versions
}

target_database_head() {
  local output head_count head
  output="$(uv run --frozen --no-dev alembic heads)" \
    || fail "could not resolve target Alembic head"
  head_count="$(awk 'NF {count += 1} END {print count + 0}' <<<"$output")"
  [[ "$head_count" == "1" ]] || fail "target migration graph must have exactly one head"
  head="$(awk 'NF {print $1; exit}' <<<"$output")"
  [[ "$head" =~ ^[0-9a-f]{12,64}$ ]] || fail "invalid target Alembic head: $head"
  printf '%s\n' "$head"
}

railway_status_json() {
  railway status \
    --project "$RAILWAY_PROJECT_ID" \
    --environment "$RAILWAY_ENVIRONMENT" \
    --json
}

validate_railway_config() {
  if ! uv run --frozen --no-dev python scripts/validate_railway_config.py \
    "$RAILWAY_PROJECT_ID" \
    "$RAILWAY_ENVIRONMENT" \
    "$PUBLIC_BASE_URL"; then
    fail "Railway service variables failed the production consistency check"
  fi
  log "verified Railway role assignment and shared production configuration"
}

validate_railway_target() {
  local status_json
  status_json="$(railway_status_json)"
  [[ "$(jq -r '.id // ""' <<<"$status_json")" == "$RAILWAY_PROJECT_ID" ]] \
    || fail "Railway project ID does not match $RAILWAY_PROJECT_ID"
  [[ "$(jq -r '.name // ""' <<<"$status_json")" == "$RAILWAY_PROJECT_NAME" ]] \
    || fail "Railway project name does not match $RAILWAY_PROJECT_NAME"
  jq -e --arg environment "$RAILWAY_ENVIRONMENT" \
    '.environments.edges | any(.node.name == $environment)' >/dev/null <<<"$status_json" \
    || fail "Railway environment not found: $RAILWAY_ENVIRONMENT"
}

railway_service_node() {
  local service="$1"
  railway_status_json | jq \
    --arg environment "$RAILWAY_ENVIRONMENT" \
    --arg service "$service" '
      .environments.edges[]
      | select(.node.name == $environment)
      | .node.serviceInstances.edges[].node
      | select(.serviceName == $service)
    '
}

railway_source_image() {
  railway_service_node "$1" | jq -r '.source.image // ""'
}

railway_service_variable() {
  local service="$1"
  local variable_name="$2"
  railway variable list \
    --project "$RAILWAY_PROJECT_ID" \
    --environment "$RAILWAY_ENVIRONMENT" \
    --service "$service" \
    --json | jq -er --arg name "$variable_name" '.[$name]'
}

railway_environment_config_json() {
  railway api \
    'query EnvironmentConfig($id: String!, $projectId: String!) { environment(id: $id, projectId: $projectId) { config } }' \
    --variables "$(jq -cn --arg id "$RAILWAY_ENVIRONMENT_ID" --arg projectId "$RAILWAY_PROJECT_ID" '{id: $id, projectId: $projectId}')" \
    | jq -e '.data.environment.config'
}

railway_image_auto_update_type() {
  local service="$1"
  local service_id
  service_id="$(railway_service_node "$service" | jq -r '.serviceId // ""')"
  [[ -n "$service_id" ]] || fail "cannot resolve Railway service ID for $service"
  railway_environment_config_json | jq -er \
    --arg service_id "$service_id" '
      if (.services | has($service_id) | not) then
        error("missing_service_config")
      elif .services[$service_id].source == null then
        error("missing_service_source")
      elif .services[$service_id].source.autoUpdates == null then
        "disabled"
      elif .services[$service_id].source.autoUpdates.type == "disabled" then
        "disabled"
      else
        (.services[$service_id].source.autoUpdates.type // "unknown")
      end
    ' || fail "cannot determine Railway image auto-update state for $service"
}

validate_railway_image_auto_updates() {
  local service auto_update_type
  for service in "${RAILWAY_SERVICES[@]}"; do
    auto_update_type="$(railway_image_auto_update_type "$service")"
    [[ "$auto_update_type" == "disabled" ]] \
      || fail "Railway native image auto-update must be disabled for $service, got: $auto_update_type"
  done
  log "verified Railway native image auto-update is disabled for all application roles"
}

active_region() {
  railway_status_json | python3 scripts/railway_active_region.py "$RAILWAY_ENVIRONMENT" "$1"
}

validate_railway_colocation() {
  local service service_region
  for service in "${RAILWAY_COLOCATED_SERVICES[@]}"; do
    service_region="$(active_region "$service")" \
      || fail "could not determine the sole active Railway region for $service"
    [[ "$service_region" == "$RAILWAY_REGION" ]] \
      || fail "Railway $service region $service_region does not match $RAILWAY_REGION"
  done
  log "verified Railway colocation: ${RAILWAY_COLOCATED_SERVICES[*]} -> $RAILWAY_REGION"
}

active_deployment_json() {
  railway_service_node "$1" | jq '
    .activeDeployments
    | map(select(.status == "SUCCESS"))
    | first
  '
}

deployment_runtime_json() {
  local deployment_id="$1"
  railway api \
    'query DeploymentRuntime($id: String!) { deployment(id: $id) { id status deploymentStopped } }' \
    --variables "$(jq -cn --arg id "$deployment_id" '{id: $id}')" \
    | jq -e '.data.deployment'
}

service_deployment_runtimes_json() {
  local service="$1"
  local service_id after_cursor="" accumulated='[]'
  local response page has_next_page next_cursor
  service_id="$(railway_service_node "$service" | jq -r '.serviceId // ""')" \
    || fail "could not resolve Railway service ID for $service"
  [[ -n "$service_id" ]] || fail "cannot resolve Railway service ID for $service"
  while true; do
    response="$(railway api \
      'query ServiceDeployments($input: DeploymentListInput!, $first: Int!, $after: String) { deployments(input: $input, first: $first, after: $after) { edges { node { id status deploymentStopped } } pageInfo { hasNextPage endCursor } } }' \
      --variables "$(jq -cn \
        --arg projectId "$RAILWAY_PROJECT_ID" \
        --arg environmentId "$RAILWAY_ENVIRONMENT_ID" \
        --arg serviceId "$service_id" \
        --arg after "$after_cursor" \
        '{input:{projectId:$projectId,environmentId:$environmentId,serviceId:$serviceId},first:100,after:(if $after == "" then null else $after end)}')")" \
      || fail "could not list Railway deployments for $service"
    jq -e '.data.deployments.edges and .data.deployments.pageInfo' \
      >/dev/null <<<"$response" || fail "invalid Railway deployment page for $service"
    if [[ "$fresh_queue" == "true" ]]; then
      jq -e '(.errors // [] | length) == 0
        and (.data.deployments.edges | type) == "array"
        and (.data.deployments.pageInfo.hasNextPage | type) == "boolean"' \
        >/dev/null <<<"$response" || fail "incomplete Railway deployment page for $service"
    fi
    page="$(jq '[.data.deployments.edges[].node]' <<<"$response")"
    accumulated="$(jq -cn \
      --argjson accumulated "$accumulated" \
      --argjson page "$page" \
      '$accumulated + $page')"
    has_next_page="$(jq -r '.data.deployments.pageInfo.hasNextPage' <<<"$response")"
    [[ "$has_next_page" == "true" ]] || break
    next_cursor="$(jq -r '.data.deployments.pageInfo.endCursor // ""' <<<"$response")"
    [[ -n "$next_cursor" && "$next_cursor" != "$after_cursor" ]] \
      || fail "invalid Railway deployment cursor for $service"
    after_cursor="$next_cursor"
  done
  printf '%s\n' "$accumulated"
}

unstopped_deployment_ids() {
  service_deployment_runtimes_json "$1" | jq -r '
    .[] | select(.deploymentStopped == false) | .id
  '
}

in_progress_deployment_ids() {
  service_deployment_runtimes_json "$1" | jq -r '
      .[]
      | select(
          .status == "QUEUED"
          or .status == "INITIALIZING"
          or .status == "WAITING"
          or .status == "BUILDING"
          or .status == "DEPLOYING"
          or .status == "NEEDS_APPROVAL"
          or .status == "REMOVING"
        )
      | .id
    '
}

wait_for_replaced_deployments_to_stop() {
  local service="$1"
  local replacement_deployment_id="$2"
  local replaced_deployment_ids="$3"
  local deployment_id deadline runtime status deployment_stopped
  [[ -n "$replaced_deployment_ids" ]] || return 0
  while IFS= read -r deployment_id; do
    [[ -n "$deployment_id" && "$deployment_id" != "$replacement_deployment_id" ]] \
      || continue
    deadline=$((SECONDS + DEPLOY_TIMEOUT_SECONDS))
    while (( SECONDS < deadline )); do
      runtime="$(deployment_runtime_json "$deployment_id")" \
        || fail "could not inspect replaced deployment $deployment_id"
      status="$(jq -r '.status // ""' <<<"$runtime")"
      deployment_stopped="$(jq -r '.deploymentStopped // false' <<<"$runtime")"
      if [[ "$status" == "REMOVED" && "$deployment_stopped" == "true" ]]; then
        log "$service replaced deployment is fully stopped: $deployment_id"
        break
      fi
      case "$status" in
        SUCCESS|REMOVING|REMOVED) ;;
        FAILED|CRASHED|SKIPPED|SLEEPING)
          [[ "$deployment_stopped" == "true" ]] && break
          ;;
        *) fail "$service replaced deployment $deployment_id has unsafe status $status" ;;
      esac
      sleep 5
    done
    [[ "$deployment_stopped" == "true" ]] \
      || fail "$service replaced deployment did not stop: $deployment_id"
  done <<<"$replaced_deployment_ids"
}

role_runtime_converged() {
  local service="$1"
  local expected_deployment_id="$2"
  local expected_digest="$3"
  local active_deployments deployment_runtimes
  active_deployments="$(railway_service_node "$service" | jq \
    '[.activeDeployments[] | select(.status == "SUCCESS")]')"
  [[ "$(jq 'length' <<<"$active_deployments")" == "1" ]] \
    || fail "$service has multiple active deployments"
  [[ "$(jq -r '.[0].id // ""' <<<"$active_deployments")" == "$expected_deployment_id" ]] \
    || fail "$service active deployment changed unexpectedly"
  [[ "$(jq -r '.[0].meta.imageDigest // ""' <<<"$active_deployments")" == "$expected_digest" ]] \
    || fail "$service active deployment has the wrong digest"
  deployment_runtimes="$(service_deployment_runtimes_json "$service")"
  jq -e --arg expected "$expected_deployment_id" '
    [.[] | select(.deploymentStopped == false)]
    | length == 1
      and .[0].id == $expected
      and .[0].status == "SUCCESS"
  ' >/dev/null <<<"$deployment_runtimes" \
    || fail "$service has another deployment whose instances are not stopped"
  [[ -z "$(in_progress_deployment_ids "$service")" ]] \
    || fail "$service still has an unresolved deployment"
}

latest_deployment_json() {
  local service="$1"
  railway deployment list \
    --project "$RAILWAY_PROJECT_ID" \
    --environment "$RAILWAY_ENVIRONMENT" \
    --service "$service" \
    --limit 1 \
    --json | jq '.[0]'
}

deployment_for_digest_json() {
  local service="$1"
  local digest="$2"
  railway deployment list \
    --project "$RAILWAY_PROJECT_ID" \
    --environment "$RAILWAY_ENVIRONMENT" \
    --service "$service" \
    --limit 100 \
    --json | jq --arg digest "$digest" '
      map(
        select(
          (.status == "SUCCESS" or .status == "REMOVED")
          and .meta.imageDigest == $digest
        )
      )
      | first
    '
}

wait_for_deployment() {
  local service="$1"
  local deployment_id="$2"
  local expected_digest="$3"
  local deadline=$((SECONDS + DEPLOY_TIMEOUT_SECONDS))
  local deployment deployment_status deployment_digest

  while (( SECONDS < deadline )); do
    deployment="$(railway deployment list \
      --project "$RAILWAY_PROJECT_ID" \
      --environment "$RAILWAY_ENVIRONMENT" \
      --service "$service" \
      --limit 100 \
      --json | jq --arg id "$deployment_id" 'map(select(.id == $id))[0]')"
    deployment_status="$(jq -r '.status // ""' <<<"$deployment")"
    deployment_digest="$(jq -r '.meta.imageDigest // ""' <<<"$deployment")"
    case "$deployment_status" in
      SUCCESS)
        [[ "$deployment_digest" == "$expected_digest" ]] \
          || fail "$service deployed $deployment_digest, expected $expected_digest"
        printf '%s\n' "$deployment_id"
        return 0
        ;;
      FAILED|CRASHED|REMOVED|SKIPPED|SLEEPING)
        fail "$service deployment $deployment_id ended with $deployment_status"
        ;;
      QUEUED|INITIALIZING|WAITING|BUILDING|DEPLOYING|NEEDS_APPROVAL|REMOVING|"") ;;
      *) fail "$service deployment $deployment_id returned unknown status $deployment_status" ;;
    esac
    sleep 5
  done
  fail "timed out waiting for Railway $service deployment $deployment_id"
}

wait_for_api_health() {
  local deadline=$((SECONDS + 180))
  while (( SECONDS < deadline )); do
    if [[ "$(curl -fsS "${PUBLIC_BASE_URL}/healthz" 2>/dev/null || true)" == '{"status":"ok"}' ]]; then
      return 0
    fi
    sleep 3
  done
  fail "API health check failed: ${PUBLIC_BASE_URL}/healthz"
}

write_manifest() {
  local release_status="$1"
  local rollout_phase="${2:-${recorded_rollout_phase:-previous}}"
  local manifest_tmp
  manifest_tmp="$(mktemp "${manifest_path}.tmp.XXXXXX")"
  jq -n \
    --arg status "$release_status" \
    --argjson fresh_queue "$fresh_queue_metadata" \
    --arg rollout_phase "$rollout_phase" \
    --arg git_sha "$full_sha" \
    --arg image_repository "$IMAGE_REPO" \
    --arg sha_tag "$sha_ref" \
    --arg latest_tag "$latest_ref" \
    --arg digest "${expected_digest:-}" \
    --arg previous_digest "${previous_digest:-}" \
    --arg rollback_tag "$rollback_ref" \
    --arg previous_app_revision "${previous_app_revision:-}" \
    --argjson migration_compatibility_required "${migration_compatibility_required:-false}" \
    --arg rollback_compatible_tag "$rollback_compatible_ref" \
    --arg rollback_compatible_digest "${rollback_compatible_digest:-}" \
    --arg rollback_schema_head "${rollback_database_head:-}" \
    --arg previous_review_outbox_capability "${previous_review_outbox_capability:-}" \
    --arg target_review_outbox_capability "${target_review_outbox_capability:-}" \
    --arg previous_business_prompt_capability "${previous_business_prompt_capability:-}" \
    --arg target_business_prompt_capability "${target_business_prompt_capability:-}" \
    --arg previous_api_deployment_id "${previous_api_deployment_id:-}" \
    --arg previous_worker_deployment_id "${previous_worker_deployment_id:-}" \
    --arg previous_scheduler_deployment_id "${previous_scheduler_deployment_id:-}" \
    --arg compatibility_api_deployment_id "${compatibility_api_deployment_id:-}" \
    --arg compatibility_worker_deployment_id "${compatibility_worker_deployment_id:-}" \
    --arg compatibility_scheduler_deployment_id "${compatibility_scheduler_deployment_id:-}" \
    --arg api_deployment_id "${api_deployment_id:-}" \
    --arg worker_deployment_id "${worker_deployment_id:-}" \
    --arg scheduler_deployment_id "${scheduler_deployment_id:-}" \
    --arg updated_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{
      status: $status,
      fresh_queue: $fresh_queue,
      rollout_phase: $rollout_phase,
      git_sha: $git_sha,
      image_repository: $image_repository,
      sha_tag: $sha_tag,
      latest_tag: $latest_tag,
      digest: $digest,
      previous_digest: $previous_digest,
      rollback_tag: $rollback_tag,
      migration_compatible_rollback: {
        required: $migration_compatibility_required,
        tag: (if $migration_compatibility_required then $rollback_compatible_tag else "" end),
        digest: $rollback_compatible_digest,
        predecessor_app_revision: $previous_app_revision,
        database_head: $rollback_schema_head
      },
      review_outbox_contract: {
        previous: $previous_review_outbox_capability,
        target: $target_review_outbox_capability
      },
      business_prompt_contract: {
        previous: $previous_business_prompt_capability,
        target: $target_business_prompt_capability
      },
      previous_railway: {
        api_deployment_id: $previous_api_deployment_id,
        worker_deployment_id: $previous_worker_deployment_id,
        scheduler_deployment_id: $previous_scheduler_deployment_id
      },
      compatibility_railway: {
        api_deployment_id: $compatibility_api_deployment_id,
        worker_deployment_id: $compatibility_worker_deployment_id,
        scheduler_deployment_id: $compatibility_scheduler_deployment_id
      },
      railway: {
        api_deployment_id: $api_deployment_id,
        worker_deployment_id: $worker_deployment_id,
        scheduler_deployment_id: $scheduler_deployment_id
      },
      updated_at: $updated_at
    }' >"$manifest_tmp" \
    || { rm -f "$manifest_tmp"; fail "could not render release manifest"; }
  jq -e . "$manifest_tmp" >/dev/null \
    || { rm -f "$manifest_tmp"; fail "release manifest is not valid JSON"; }
  python3 - "$manifest_tmp" "$manifest_path" <<'PY'
import os
import sys

source, destination = sys.argv[1:]
with open(source, "rb") as manifest_file:
    os.fsync(manifest_file.fileno())
os.replace(source, destination)
directory_fd = os.open(os.path.dirname(destination) or ".", os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
}

log "release commit: $full_sha"
wait_for_ci
revalidate_dev_head
validate_railway_target
validate_railway_config
validate_railway_colocation

for service in "${RAILWAY_SERVICES[@]}"; do
  source_image="$(railway_source_image "$service")"
  if [[ "$source_image" != "$latest_ref" ]]; then
    fail "Railway $service source must be $latest_ref, got: ${source_image:-none}"
  fi
done
validate_railway_image_auto_updates

sha_image_state="$(image_state "$sha_ref")"
[[ "$sha_image_state" == "exists" ]] \
  || fail "verified GHCR SHA image is missing; CI must publish $sha_ref before Railway release"
verify_sha_image "$sha_ref"
log "verified CI-published immutable SHA image: $sha_ref"

expected_digest="$(image_digest "$sha_ref")"
rollback_image_state="$(image_state "$rollback_ref")"
rollback_digest=""
if [[ "$rollback_image_state" == "exists" ]]; then
  rollback_digest="$(image_digest "$rollback_ref")"
fi
active_api="$(active_deployment_json api)"
active_worker="$(active_deployment_json worker)"
active_scheduler="$(active_deployment_json scheduler)"
active_api_id="$(jq -r '.id // ""' <<<"$active_api")"
active_worker_id="$(jq -r '.id // ""' <<<"$active_worker")"
active_scheduler_id="$(jq -r '.id // ""' <<<"$active_scheduler")"
active_api_digest="$(jq -r '.meta.imageDigest // ""' <<<"$active_api")"
active_worker_digest="$(jq -r '.meta.imageDigest // ""' <<<"$active_worker")"
active_scheduler_digest="$(jq -r '.meta.imageDigest // ""' <<<"$active_scheduler")"
for active_value in \
  "$active_api_id" \
  "$active_worker_id" \
  "$active_scheduler_id" \
  "$active_api_digest" \
  "$active_worker_digest" \
  "$active_scheduler_digest"; do
  [[ -n "$active_value" ]] || fail "Railway has incomplete active deployment metadata"
done
for active_digest in "$active_api_digest" "$active_worker_digest" "$active_scheduler_digest"; do
  [[ "$active_digest" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fail "Railway has an invalid active image digest: $active_digest"
done

if [[ -n "$rollback_digest" ]]; then
  previous_digest="$rollback_digest"
  [[ "$previous_digest" != "$expected_digest" ]] \
    || fail "rollback tag unexpectedly points to the target release"
else
  previous_digest="$active_api_digest"
  [[ "$active_worker_digest" == "$previous_digest" \
    && "$active_scheduler_digest" == "$previous_digest" ]] \
    || fail "mixed Railway digests require an existing immutable rollback tag"
  [[ "$previous_digest" != "$expected_digest" ]] \
    || fail "cannot reconstruct the predecessor after all roles reached the target digest"
  revalidate_dev_head
  rollback_image_state="$(image_state "$rollback_ref")"
  if [[ "$rollback_image_state" == "exists" ]]; then
    rollback_digest="$(image_digest "$rollback_ref")"
    [[ "$rollback_digest" == "$previous_digest" ]] \
      || fail "rollback tag appeared concurrently with an unexpected digest"
  else
    log "retaining immutable rollback image: $rollback_ref -> $previous_digest"
    docker buildx imagetools create --prefer-index=false \
      --tag "$rollback_ref" "${IMAGE_REPO}@${previous_digest}" >/dev/null
    rollback_digest="$(image_digest "$rollback_ref")"
    [[ "$rollback_digest" == "$previous_digest" ]] \
      || fail "rollback tag digest does not match the active predecessor"
  fi
fi

previous_api_deployment_id="$(jq -r '.id // ""' \
  <<<"$(deployment_for_digest_json api "$previous_digest")")"
previous_worker_deployment_id="$(jq -r '.id // ""' \
  <<<"$(deployment_for_digest_json worker "$previous_digest")")"
previous_scheduler_deployment_id="$(jq -r '.id // ""' \
  <<<"$(deployment_for_digest_json scheduler "$previous_digest")")"
for previous_id in \
  "$previous_api_deployment_id" \
  "$previous_worker_deployment_id" \
  "$previous_scheduler_deployment_id"; do
  [[ -n "$previous_id" ]] || fail "could not retain all predecessor Railway deployment IDs"
done

verify_predecessor_image "${IMAGE_REPO}@${previous_digest}" "$previous_digest"
previous_app_revision="$(image_revision "${IMAGE_REPO}@${previous_digest}")"
[[ "$previous_app_revision" =~ ^[0-9a-f]{40}$ ]] \
  || fail "predecessor image has invalid OCI revision: ${previous_app_revision:-missing}"
previous_review_outbox_capability="$(
  image_review_outbox_capability "${IMAGE_REPO}@${previous_digest}"
)"
target_review_outbox_capability="$(
  image_review_outbox_capability "${IMAGE_REPO}@${expected_digest}"
)"
previous_business_prompt_capability="$(
  image_business_prompt_capability "${IMAGE_REPO}@${previous_digest}"
)"
target_business_prompt_capability="$(
  image_business_prompt_capability "${IMAGE_REPO}@${expected_digest}"
)"
business_prompt_transition="false"
case "${previous_business_prompt_capability}:${target_business_prompt_capability}" in
  "${LEGACY_BUSINESS_PROMPT_CAPABILITY}:${TARGET_BUSINESS_PROMPT_CAPABILITY}")
    business_prompt_transition="true"
    for service in "${RAILWAY_SERVICES[@]}"; do
      [[ "$(railway_service_variable "$service" REPLY_BUSINESS_PROMPT_ENABLED)" == "false" ]] \
        || fail "first editable Prompt rollout requires REPLY_BUSINESS_PROMPT_ENABLED=false on $service"
    done
    log "verified editable Prompt rollout remains disabled on all roles"
    ;;
  "${TARGET_BUSINESS_PROMPT_CAPABILITY}:${TARGET_BUSINESS_PROMPT_CAPABILITY}") ;;
  *)
    fail "unsupported business Prompt capability transition: ${previous_business_prompt_capability} -> ${target_business_prompt_capability}"
    ;;
esac
bridge_required="false"
case "${previous_review_outbox_capability}:${target_review_outbox_capability}" in
  "${LEGACY_REVIEW_OUTBOX_CAPABILITY}:${TARGET_REVIEW_OUTBOX_CAPABILITY}")
    bridge_required="true"
    ;;
  "${TARGET_REVIEW_OUTBOX_CAPABILITY}:${TARGET_REVIEW_OUTBOX_CAPABILITY}") ;;
  *)
    fail "unsupported review Outbox capability transition: ${previous_review_outbox_capability} -> ${target_review_outbox_capability}"
    ;;
esac
migration_compatibility_required="false"
rollback_compatible_digest=""
rollback_database_head=""
if migration_graph_changed "$previous_app_revision"; then
  migration_compatibility_required="true"
  rollback_database_head="$(target_database_head)"
  rollback_compatible_digest="$(require_ci_rollback_compatible_image \
    "$rollback_compatible_ref" \
    "$previous_digest" \
    "$previous_app_revision" \
    "$previous_review_outbox_capability" \
    "$rollback_database_head" \
    "$previous_business_prompt_capability")"
  [[ "$rollback_compatible_digest" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fail "invalid migration-compatible rollback digest: $rollback_compatible_digest"
  log "verified CI-published migration-compatible rollback image: $rollback_compatible_ref"
else
  log "migration graph unchanged; raw predecessor digest remains rollback-compatible"
fi
if [[ "$bridge_required" == "true" && "$migration_compatibility_required" != "true" ]]; then
  fail "review Outbox capability bridge requires a CI-published migration-compatible image"
fi
if [[ "$business_prompt_transition" == "true" \
  && "$migration_compatibility_required" != "true" ]]; then
  fail "editable business Prompt capability transition requires a migration-compatible image"
fi

for service in "${RAILWAY_SERVICES[@]}"; do
  case "$service" in
    api) current_digest="$active_api_digest" ;;
    worker) current_digest="$active_worker_digest" ;;
    scheduler) current_digest="$active_scheduler_digest" ;;
  esac
  if [[ "$current_digest" != "$previous_digest" \
    && "$current_digest" != "$expected_digest" ]]; then
    if [[ "$migration_compatibility_required" != "true" \
      || "$current_digest" != "$rollback_compatible_digest" ]]; then
      fail "$service is on unrelated digest $current_digest"
    fi
  fi
  if [[ "$migration_compatibility_required" == "true" \
    && "$bridge_required" != "true" \
    && "$current_digest" == "$rollback_compatible_digest" ]]; then
    fail "$service unexpectedly runs the compatibility digest without a capability bridge"
  fi
done

recorded_rollout_phase=""
manifest_status=""
if [[ -f "$manifest_path" ]]; then
  jq -e . "$manifest_path" >/dev/null || fail "existing release manifest is invalid JSON"
  manifest_sha="$(jq -r '.git_sha // ""' "$manifest_path")"
  manifest_digest="$(jq -r '.digest // ""' "$manifest_path")"
  manifest_previous="$(jq -r '.previous_digest // ""' "$manifest_path")"
  manifest_compatibility_required="$(
    jq -r '.migration_compatible_rollback.required // false' "$manifest_path"
  )"
  manifest_compatibility="$(
    jq -r '.migration_compatible_rollback.digest // ""' "$manifest_path"
  )"
  manifest_previous_business_prompt_capability="$(
    jq -r '.business_prompt_contract.previous // ""' "$manifest_path"
  )"
  manifest_target_business_prompt_capability="$(
    jq -r '.business_prompt_contract.target // ""' "$manifest_path"
  )"
  [[ "$manifest_sha" == "$full_sha" \
    && "$manifest_digest" == "$expected_digest" \
    && "$manifest_previous" == "$previous_digest" \
    && "$manifest_compatibility_required" == "$migration_compatibility_required" \
    && "$manifest_compatibility" == "$rollback_compatible_digest" \
    && "$manifest_previous_business_prompt_capability" == "$previous_business_prompt_capability" \
    && "$manifest_target_business_prompt_capability" == "$target_business_prompt_capability" ]] \
    || fail "existing release manifest does not match this rollout"
  manifest_status="$(jq -r '.status // ""' "$manifest_path")"
  recorded_rollout_phase="$(jq -r '.rollout_phase // ""' "$manifest_path")"
  case "$manifest_status" in
    prepared|deploying|completed) ;;
    *) fail "existing release manifest has unknown status: ${manifest_status:-missing}" ;;
  esac
fi

compatibility_api_deployment_id=""
compatibility_worker_deployment_id=""
compatibility_scheduler_deployment_id=""
if [[ "$migration_compatibility_required" == "true" ]]; then
  compatibility_api_deployment_id="$(jq -r '.id // ""' \
    <<<"$(deployment_for_digest_json api "$rollback_compatible_digest")")"
  compatibility_worker_deployment_id="$(jq -r '.id // ""' \
    <<<"$(deployment_for_digest_json worker "$rollback_compatible_digest")")"
  compatibility_scheduler_deployment_id="$(jq -r '.id // ""' \
    <<<"$(deployment_for_digest_json scheduler "$rollback_compatible_digest")")"
fi
api_deployment_id="$(jq -r '.id // ""' \
  <<<"$(deployment_for_digest_json api "$expected_digest")")"
worker_deployment_id="$(jq -r '.id // ""' \
  <<<"$(deployment_for_digest_json worker "$expected_digest")")"
scheduler_deployment_id="$(jq -r '.id // ""' \
  <<<"$(deployment_for_digest_json scheduler "$expected_digest")")"

if [[ -z "$manifest_status" ]]; then
  manifest_status="prepared"
  recorded_rollout_phase="previous"
  write_manifest "$manifest_status" "$recorded_rollout_phase"
fi

require_latest_digest() {
  local expected="$1"
  local stage="$2"
  local observed
  observed="$(image_digest "$latest_ref")"
  [[ "$observed" == "$expected" ]] \
    || fail "GHCR latest changed during $stage: $observed"
}

promote_latest_digest() {
  local expected_current="$1"
  local desired_digest="$2"
  local desired_ref="$3"
  local stage="$4"
  revalidate_dev_head
  [[ "$(image_digest "$desired_ref")" == "$desired_digest" ]] \
    || fail "$stage source reference changed before promotion"
  [[ "$(image_digest "$latest_ref")" == "$expected_current" ]] \
    || fail "GHCR latest changed concurrently before $stage promotion"
  log "promoting $stage digest to $latest_ref"
  docker buildx imagetools create --prefer-index=false \
    --tag "$latest_ref" "${IMAGE_REPO}@${desired_digest}" >/dev/null
  require_latest_digest "$desired_digest" "$stage promotion"
}

deploy_role() {
  local service="$1"
  local desired_digest="$2"
  local stage="$3"
  local active active_id active_digest latest latest_id latest_status latest_digest
  local redeploy_output deployment_id replaced_deployment_ids
  require_latest_digest "$desired_digest" "$stage"
  active="$(active_deployment_json "$service")"
  active_id="$(jq -r '.id // ""' <<<"$active")"
  active_digest="$(jq -r '.meta.imageDigest // ""' <<<"$active")"
  if [[ "$active_digest" == "$desired_digest" ]]; then
    replaced_deployment_ids="$(unstopped_deployment_ids "$service")"
    wait_for_replaced_deployments_to_stop \
      "$service" "$active_id" "$replaced_deployment_ids"
    role_runtime_converged "$service" "$active_id" "$desired_digest"
    log "$service already runs the $stage digest: $active_id"
    printf '%s\n' "$active_id"
    return 0
  fi
  if [[ -z "$active_id" || -z "$active_digest" ]]; then
    [[ "$fresh_queue" == "true" ]] \
      || fail "$service has no active deployment; stopped-predecessor recovery is fresh-only"
    assert_fresh_role_stopped "$service"
    validate_fresh_queue_variables
  elif [[ "$active_digest" != "$previous_digest" \
    && "$active_digest" != "$rollback_compatible_digest" \
    && "$active_digest" != "$expected_digest" ]]; then
    fail "$service cannot resume from digest $active_digest"
  fi

  latest="$(latest_deployment_json "$service")"
  latest_id="$(jq -r '.id // ""' <<<"$latest")"
  latest_status="$(jq -r '.status // ""' <<<"$latest")"
  latest_digest="$(jq -r '.meta.imageDigest // ""' <<<"$latest")"
  replaced_deployment_ids="$(unstopped_deployment_ids "$service")"
  if [[ -n "$latest_id" && "$latest_id" != "$active_id" ]]; then
    if [[ "$latest_digest" == "$desired_digest" ]]; then
      case "$latest_status" in
        SUCCESS)
          wait_for_replaced_deployments_to_stop \
            "$service" "$latest_id" "$replaced_deployment_ids"
          role_runtime_converged "$service" "$latest_id" "$desired_digest"
          printf '%s\n' "$latest_id"
          return 0
          ;;
        QUEUED|INITIALIZING|WAITING|BUILDING|DEPLOYING|NEEDS_APPROVAL)
          log "resuming in-flight $stage $service deployment: $latest_id"
          wait_for_deployment "$service" "$latest_id" "$desired_digest" >/dev/null
          wait_for_replaced_deployments_to_stop \
            "$service" "$latest_id" "$replaced_deployment_ids"
          role_runtime_converged "$service" "$latest_id" "$desired_digest"
          printf '%s\n' "$latest_id"
          return 0
          ;;
        FAILED|CRASHED|REMOVED|SKIPPED|SLEEPING) ;;
        REMOVING)
          fail "$service desired deployment is being removed: $latest_id"
          ;;
        *) fail "$service latest deployment has unknown status $latest_status" ;;
      esac
    elif [[ "$latest_status" =~ ^(QUEUED|INITIALIZING|WAITING|BUILDING|DEPLOYING|NEEDS_APPROVAL|REMOVING)$ ]]; then
      fail "$service has unresolved in-flight deployment $latest_id with digest ${latest_digest:-unknown}; refusing duplicate redeploy"
    fi
  fi

  require_latest_digest "$desired_digest" "$stage"
  log "redeploying Railway $service from $stage source"
  redeploy_output="$(railway redeploy \
    --project "$RAILWAY_PROJECT_ID" \
    --environment "$RAILWAY_ENVIRONMENT" \
    --service "$service" \
    --from-source \
    --yes \
    --json)" || fail "Railway redeploy request failed for $service; inspect deployment state before retrying"
  deployment_id="$(jq -r '.id // .deploymentId // ""' <<<"$redeploy_output")"
  if [[ -z "$deployment_id" ]]; then
    deployment_id="$(jq -r '.id // ""' <<<"$(latest_deployment_json "$service")")"
    [[ -n "$deployment_id" && "$deployment_id" != "$latest_id" ]] \
      || fail "Railway redeploy returned no trackable deployment ID for $service"
  fi
  wait_for_deployment "$service" "$deployment_id" "$desired_digest" >/dev/null
  wait_for_replaced_deployments_to_stop \
    "$service" "$deployment_id" "$replaced_deployment_ids"
  role_runtime_converged "$service" "$deployment_id" "$desired_digest"
  printf '%s\n' "$deployment_id"
}

bridge_rollout_directive() {
  local latest_digest api_digest worker_digest scheduler_digest
  local -a recorded_args
  latest_digest="$(image_digest "$latest_ref")"
  api_digest="$(jq -r '.meta.imageDigest // ""' <<<"$(active_deployment_json api)")"
  worker_digest="$(jq -r '.meta.imageDigest // ""' <<<"$(active_deployment_json worker)")"
  scheduler_digest="$(jq -r '.meta.imageDigest // ""' <<<"$(active_deployment_json scheduler)")"
  recorded_args=()
  if [[ -n "$recorded_rollout_phase" ]]; then
    recorded_args=(--recorded-phase "$recorded_rollout_phase")
  fi
  python3 scripts/release_rollout_state.py \
    --latest "$latest_digest" \
    --api "$api_digest" \
    --worker "$worker_digest" \
    --scheduler "$scheduler_digest" \
    --previous "$previous_digest" \
    --target "$expected_digest" \
    --compatibility "$rollback_compatible_digest" \
    "${recorded_args[@]}"
}

converge_observed_runtime_state() {
  local service active active_id active_digest runtime_deployment_ids
  for service in "${RAILWAY_SERVICES[@]}"; do
    active="$(active_deployment_json "$service")"
    active_id="$(jq -r '.id // ""' <<<"$active")"
    active_digest="$(jq -r '.meta.imageDigest // ""' <<<"$active")"
    [[ -n "$active_id" && -n "$active_digest" ]] \
      || fail "$service has no active deployment while recovering rollout state"
    runtime_deployment_ids="$(unstopped_deployment_ids "$service")"
    wait_for_replaced_deployments_to_stop \
      "$service" "$active_id" "$runtime_deployment_ids"
    role_runtime_converged "$service" "$active_id" "$active_digest"
  done
}

run_capability_bridge_rollout() {
  local directive observed_phase next_phase action
  while true; do
    directive="$(bridge_rollout_directive)" \
      || fail "Railway rollout state is not one of the nine safe bridge checkpoints"
    observed_phase="$(jq -r '.observed_phase' <<<"$directive")"
    next_phase="$(jq -r '.next_phase' <<<"$directive")"
    action="$(jq -r '.action' <<<"$directive")"

    if [[ "$recorded_rollout_phase" != "$observed_phase" \
      && !( "$recorded_rollout_phase" == "complete" && "$action" == "complete" ) ]]; then
      log "observed rollout is ahead of its manifest; verifying deployment drain before resume"
      converge_observed_runtime_state
      recorded_rollout_phase="$observed_phase"
      manifest_status="deploying"
      write_manifest "$manifest_status" "$recorded_rollout_phase"
    fi

    case "$action" in
      promote_compatibility_latest)
        manifest_status="deploying"
        write_manifest "$manifest_status" "$observed_phase"
        promote_latest_digest \
          "$previous_digest" \
          "$rollback_compatible_digest" \
          "$rollback_compatible_ref" \
          "compatibility"
        ;;
      deploy_compatibility_api)
        compatibility_api_deployment_id="$(
          deploy_role api "$rollback_compatible_digest" compatibility
        )"
        wait_for_api_health
        ;;
      deploy_compatibility_worker)
        compatibility_worker_deployment_id="$(
          deploy_role worker "$rollback_compatible_digest" compatibility
        )"
        ;;
      deploy_compatibility_scheduler)
        compatibility_scheduler_deployment_id="$(
          deploy_role scheduler "$rollback_compatible_digest" compatibility
        )"
        ;;
      promote_target_latest)
        promote_latest_digest \
          "$rollback_compatible_digest" \
          "$expected_digest" \
          "$sha_ref" \
          "target"
        ;;
      deploy_target_worker)
        worker_deployment_id="$(deploy_role worker "$expected_digest" target)"
        ;;
      deploy_target_api)
        api_deployment_id="$(deploy_role api "$expected_digest" target)"
        wait_for_api_health
        ;;
      deploy_target_scheduler)
        scheduler_deployment_id="$(deploy_role scheduler "$expected_digest" target)"
        ;;
      complete)
        return 0
        ;;
      *) fail "rollout validator returned unknown action: $action" ;;
    esac

    recorded_rollout_phase="$next_phase"
    manifest_status="deploying"
    write_manifest "$manifest_status" "$recorded_rollout_phase"
  done
}

run_standard_rollout() {
  local initial_latest_digest
  initial_latest_digest="$(image_digest "$latest_ref")"
  if [[ "$initial_latest_digest" != "$previous_digest" \
    && "$initial_latest_digest" != "$expected_digest" ]]; then
    fail "GHCR latest changed to unrelated digest $initial_latest_digest"
  fi
  manifest_status="deploying"
  recorded_rollout_phase="previous"
  write_manifest "$manifest_status" "$recorded_rollout_phase"
  if [[ "$initial_latest_digest" != "$expected_digest" ]]; then
    promote_latest_digest "$previous_digest" "$expected_digest" "$sha_ref" target
  fi
  recorded_rollout_phase="target_latest"
  write_manifest "$manifest_status" "$recorded_rollout_phase"

  api_deployment_id="$(deploy_role api "$expected_digest" target)"
  wait_for_api_health
  recorded_rollout_phase="standard_target_api"
  write_manifest "$manifest_status" "$recorded_rollout_phase"
  worker_deployment_id="$(deploy_role worker "$expected_digest" target)"
  recorded_rollout_phase="standard_target_worker"
  write_manifest "$manifest_status" "$recorded_rollout_phase"
  scheduler_deployment_id="$(deploy_role scheduler "$expected_digest" target)"
  recorded_rollout_phase="standard_target_scheduler"
  write_manifest "$manifest_status" "$recorded_rollout_phase"
}

assert_fresh_role_stopped() {
  local service="$1"
  local predecessor_variable="previous_${service}_deployment_id"
  local predecessor_id="${!predecessor_variable}"
  local deployment_runtimes
  [[ "$fresh_queue" == "true" && -n "$predecessor_id" ]] \
    || fail "$service has no recorded fresh cutover predecessor"
  jq -e --arg service "$service" --arg predecessor "$predecessor_id" \
    '.previous_railway[($service + "_deployment_id")] == $predecessor
      and .fresh_queue != null' "$manifest_path" >/dev/null \
    || fail "$service stopped predecessor differs from release evidence"
  deployment_runtime_json "$predecessor_id" \
    | jq -e '.deploymentStopped == true' >/dev/null \
    || fail "$service predecessor is not confirmed stopped"
  deployment_runtimes="$(service_deployment_runtimes_json "$service")" \
    || fail "$service deployment stop query failed"
  jq -e --arg predecessor "$predecessor_id" '
    type == "array" and length > 0 and any(.[]; .id == $predecessor)
    and all(.[]; (.id | type) == "string" and (.id | length) > 0
      and .deploymentStopped == true
      and (.status == "SUCCESS" or .status == "REMOVED" or .status == "FAILED"
        or .status == "CRASHED" or .status == "SKIPPED" or .status == "SLEEPING"))
  ' >/dev/null <<<"$deployment_runtimes" \
    || fail "$service has unstopped, unresolved or unknown deployment state"
}

validate_fresh_queue_variables() {
  local service envelope namespace
  envelope="$(jq -c '.cutover' <<<"$fresh_queue_metadata")"
  namespace="$(jq -r '.namespace' <<<"$envelope")"
  for service in "${RAILWAY_SERVICES[@]}"; do
    railway variable list --project "$RAILWAY_PROJECT_ID" \
      --environment "$RAILWAY_ENVIRONMENT" --service "$service" --json \
      | jq -e --arg envelope "$envelope" --arg namespace "$namespace" --arg role "$service" \
        '.WORKSPACE_QUEUE_CUTOVER == $envelope and .DRAMATIQ_NAMESPACE == $namespace
          and .SERVICE_ROLE == $role and (.TENANT_ID // "default") == "default"' >/dev/null \
      || fail "$service fresh cutover variables are missing or inconsistent"
  done
}

preflight_fresh_queue_cutover() {
  local service predecessor_variable predecessor_id variables old_namespace schema_head
  [[ "$bridge_required" == "false" ]] \
    || fail "fresh queue cannot be combined with a review Outbox capability bridge"
  schema_head="$(uv run --frozen --no-dev python -c \
    'from apps.cli.retire_workspace_queue import EXPECTED_SCHEMA; print(EXPECTED_SCHEMA)')"
  [[ "$(target_database_head)" == "$schema_head" ]] \
    || fail "fresh queue requires the retirement command's exact schema head"
  [[ "$(image_digest "$latest_ref")" == "$previous_digest" ]] \
    || fail "fresh queue must start before target promotion"
  for service in "${RAILWAY_SERVICES[@]}"; do
    predecessor_variable="previous_${service}_deployment_id"
    predecessor_id="${!predecessor_variable}"
    role_runtime_converged "$service" "$predecessor_id" "$previous_digest"
    variables="$(railway variable list --project "$RAILWAY_PROJECT_ID" \
      --environment "$RAILWAY_ENVIRONMENT" --service "$service" --json)"
    jq -e 'has("WORKSPACE_QUEUE_CUTOVER") | not' >/dev/null <<<"$variables" \
      || fail "$service already has a cutover envelope; operator recovery review required"
    jq -e '(.TENANT_ID // "default") == "default"' >/dev/null <<<"$variables" \
      || fail "fresh queue requires the default tenant on every role"
    old_namespace="$(jq -r '.DRAMATIQ_NAMESPACE // "dramatiq"' <<<"$variables")"
    fresh_queue_metadata="$(jq --arg service "$service" --arg namespace "$old_namespace" \
      '.previous_namespaces[$service] = $namespace' <<<"$fresh_queue_metadata")"
  done
  write_manifest "prepared" "$recorded_rollout_phase"
}

prepare_fresh_queue_cutover() {
  local service predecessor_variable predecessor_id deadline runtime stopped
  local before envelope namespace
  preflight_fresh_queue_cutover
  for service in scheduler worker api; do
    fresh_queue_metadata="$(jq --arg phase "stopping_${service}" \
      '.phase = $phase' <<<"$fresh_queue_metadata")"
    write_manifest "deploying" "$recorded_rollout_phase"
    log "fresh cutover: stopping $service; receiving/sending downtime is intentional"
    railway down --project "$RAILWAY_PROJECT_ID" --environment "$RAILWAY_ENVIRONMENT" \
      --service "$service" --yes \
      || fail "could not stop $service; retain manifest and review before recovery"
    predecessor_variable="previous_${service}_deployment_id"
    predecessor_id="${!predecessor_variable}"
    deadline=$((SECONDS + DEPLOY_TIMEOUT_SECONDS))
    stopped="false"
    while (( SECONDS < deadline )); do
      runtime="$(deployment_runtime_json "$predecessor_id")"
      stopped="$(jq -r '.deploymentStopped // false' <<<"$runtime")"
      [[ "$stopped" == "true" ]] && break
      sleep 5
    done
    [[ "$stopped" == "true" ]] || fail "$service stop timed out; cutover not confirmed"
    assert_fresh_role_stopped "$service"
    fresh_queue_metadata="$(jq --arg phase "stopped_${service}" \
      '.phase = $phase' <<<"$fresh_queue_metadata")"
    write_manifest "deploying" "$recorded_rollout_phase"
  done
  for service in "${RAILWAY_SERVICES[@]}"; do
    assert_fresh_role_stopped "$service"
  done
  before="$(python3 -c \
    'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat(timespec="microseconds"))')"
  fresh_queue_metadata="$(jq --arg before "$before" \
    '.phase = "all_stopped" | .cutover.before = $before | .cutover.processes_stopped = true' \
    <<<"$fresh_queue_metadata")"
  write_manifest "deploying" "$recorded_rollout_phase"
  envelope="$(jq -c '.cutover' <<<"$fresh_queue_metadata")"
  namespace="$(jq -r '.namespace' <<<"$envelope")"
  for service in "${RAILWAY_SERVICES[@]}"; do
    assert_fresh_role_stopped "$service"
    railway variable set "DRAMATIQ_NAMESPACE=$namespace" "WORKSPACE_QUEUE_CUTOVER=$envelope" \
      --project "$RAILWAY_PROJECT_ID" --environment "$RAILWAY_ENVIRONMENT" \
      --service "$service" --skip-deploys \
      || fail "$service cutover variables failed; do not start any role"
  done
  validate_fresh_queue_variables
  validate_railway_config
  fresh_queue_metadata="$(jq '.phase = "configured"' <<<"$fresh_queue_metadata")"
  write_manifest "deploying" "$recorded_rollout_phase"
}

if [[ "$fresh_queue" == "true" ]]; then
  prepare_fresh_queue_cutover
fi

if [[ "$bridge_required" == "true" ]]; then
  log "activating review Outbox capability bridge: ${previous_review_outbox_capability} -> ${target_review_outbox_capability}"
  run_capability_bridge_rollout
else
  log "review Outbox capability unchanged; using the standard API-first rollout"
  run_standard_rollout
fi

api_deployment_id="$(jq -r '.id // ""' <<<"$(active_deployment_json api)")"
worker_deployment_id="$(jq -r '.id // ""' <<<"$(active_deployment_json worker)")"
scheduler_deployment_id="$(jq -r '.id // ""' <<<"$(active_deployment_json scheduler)")"
require_latest_digest "$expected_digest" "final verification"

final_latest_digest="$(image_digest "$latest_ref")"
[[ "$final_latest_digest" == "$expected_digest" ]] \
  || fail "GHCR latest changed during Railway rollout"
for service in "${RAILWAY_SERVICES[@]}"; do
  active="$(active_deployment_json "$service")"
  final_id="$(jq -r '.id // ""' <<<"$active")"
  final_digest="$(jq -r '.meta.imageDigest // ""' <<<"$active")"
  [[ -n "$final_id" && "$final_digest" == "$expected_digest" ]] \
    || fail "$service active deployment does not match $expected_digest"
  role_runtime_converged "$service" "$final_id" "$expected_digest"
done
validate_railway_config
validate_railway_colocation
validate_railway_image_auto_updates
revalidate_dev_head
require_latest_digest "$expected_digest" "final verification"
recorded_rollout_phase="complete"
if [[ "$fresh_queue" == "true" ]]; then
  validate_fresh_queue_variables
  fresh_queue_metadata="$(jq '.phase = "completed"' <<<"$fresh_queue_metadata")"
fi
write_manifest "completed" "$recorded_rollout_phase"

log "release complete: $full_sha -> $expected_digest"
if [[ "$migration_compatibility_required" == "true" ]]; then
  log "migration-compatible rollback: $rollback_compatible_ref -> $rollback_compatible_digest"
else
  log "rollback uses retained raw predecessor: $rollback_ref -> $previous_digest"
fi
log "release manifest: $manifest_path"
