#!/usr/bin/env bash
set -Eeuo pipefail

readonly IMAGE_REPO="ghcr.io/junqingyongyuanbusi/reply-core"
readonly RAILWAY_PROJECT_ID="abcf3199-e5ac-415b-a22e-062206390331"
readonly RAILWAY_PROJECT_NAME="reply-core"
readonly RAILWAY_ENVIRONMENT="production"
readonly RAILWAY_ENVIRONMENT_ID="db0d6750-eb77-40ee-8a79-f706cd1f828a"
readonly PUBLIC_BASE_URL="https://relay.nexory.top"
readonly SOURCE_URL="https://github.com/junqingyongyuanbusi/dm"
readonly RAILWAY_REGION="us-east4-eqdc4a"
readonly RAILWAY_COLOCATED_SERVICES=(api worker scheduler Postgres Redis)
readonly DEPLOY_TIMEOUT_SECONDS="${DEPLOY_TIMEOUT_SECONDS:-900}"
readonly RAILWAY_SERVICES=(api worker scheduler)

fail() {
  printf '[rollback] ERROR: %s\n' "$*" >&2
  exit 1
}

log() {
  printf '[rollback] %s\n' "$*" >&2
}

[[ $# -eq 2 && "$1" == --execute=* ]] \
  || fail "usage: $0 --execute=<target-full-sha> <release-manifest.json>"
confirmation="${1#--execute=}"
manifest_path="$2"
[[ -f "$manifest_path" ]] || fail "release manifest not found: $manifest_path"

for command_name in git docker railway jq curl uv python3 awk; do
  command -v "$command_name" >/dev/null 2>&1 || fail "required command not found: $command_name"
done

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || fail "not inside a Git repository"
cd "$repo_root"
mkdir -p .run dist
release_lock="$repo_root/.run/publish-railway-release.lock"
script_path="$repo_root/scripts/rollback_railway_migration_compatible.sh"
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
        print(f"[rollback] ERROR: another local Railway release or rollback is running: {lock_path}", file=sys.stderr)
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
    print(f"[rollback] ERROR: invalid inherited release lock: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
fi

image_repository="$(jq -r '.image_repository // ""' "$manifest_path")"
release_status="$(jq -r '.status // ""' "$manifest_path")"
compat_ref="$(jq -r '.migration_compatible_rollback.tag // ""' "$manifest_path")"
compat_digest="$(jq -r '.migration_compatible_rollback.digest // ""' "$manifest_path")"
target_digest="$(jq -r '.digest // ""' "$manifest_path")"
previous_digest="$(jq -r '.previous_digest // ""' "$manifest_path")"
target_sha="$(jq -r '.git_sha // ""' "$manifest_path")"
previous_app_revision="$(jq -r '.migration_compatible_rollback.predecessor_app_revision // ""' "$manifest_path")"
database_head="$(jq -r '.migration_compatible_rollback.database_head // ""' "$manifest_path")"
latest_ref="${IMAGE_REPO}:latest"
expected_compat_ref="${IMAGE_REPO}:railway-compat-pre-${target_sha:0:12}"

[[ "$release_status" == "deploying" || "$release_status" == "completed" ]] \
  || fail "release manifest status is not rollback-eligible: $release_status"
[[ "$image_repository" == "$IMAGE_REPO" ]] \
  || fail "release manifest image repository is not the production GHCR repository"
[[ "$confirmation" == "$target_sha" ]] || fail "--execute SHA does not match release manifest"
[[ "$target_sha" =~ ^[0-9a-f]{40}$ ]] || fail "invalid target SHA"
[[ "$compat_ref" == "$expected_compat_ref" ]] \
  || fail "invalid compatibility tag in manifest: $compat_ref"
[[ "$compat_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "invalid compatibility digest"
[[ "$target_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "invalid target digest"
[[ "$previous_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "invalid predecessor digest"
[[ "$previous_app_revision" =~ ^[0-9a-f]{40}$ ]] || fail "invalid predecessor app revision"
[[ "$database_head" == "a7c3e9d1b624" ]] || fail "unexpected compatibility DB head"

image_digest() {
  local output digest
  output="$(docker buildx imagetools inspect "$1")" || fail "cannot inspect image: $1"
  digest="$(awk '/^Digest:/ {print $2; exit}' <<<"$output")"
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "invalid digest for $1"
  printf '%s\n' "$digest"
}

image_metadata() {
  docker buildx imagetools inspect "$1" --format '{{json .Image}}'
}

verify_compatibility_image() {
  local metadata revision source purpose base target head image_os architecture
  metadata="$(image_metadata "${IMAGE_REPO}@${compat_digest}")"
  revision="$(jq -r '.config.Labels["org.opencontainers.image.revision"] // ""' <<<"$metadata")"
  source="$(jq -r '.config.Labels["org.opencontainers.image.source"] // ""' <<<"$metadata")"
  purpose="$(jq -r '.config.Labels["com.nexory.reply-core.rollback-purpose"] // ""' <<<"$metadata")"
  base="$(jq -r '.config.Labels["com.nexory.reply-core.rollback-base-digest"] // ""' <<<"$metadata")"
  target="$(jq -r '.config.Labels["com.nexory.reply-core.rollback-target-release"] // ""' <<<"$metadata")"
  head="$(jq -r '.config.Labels["com.nexory.reply-core.database-head"] // ""' <<<"$metadata")"
  image_os="$(jq -r '.os // ""' <<<"$metadata")"
  architecture="$(jq -r '.architecture // ""' <<<"$metadata")"
  [[ "$revision" == "$previous_app_revision" ]] || fail "compat image app revision mismatch"
  [[ "$source" == "$SOURCE_URL" ]] || fail "compat image source mismatch"
  [[ "$purpose" == "migration-compatible-predecessor" ]] || fail "compat image purpose mismatch"
  [[ "$base" == "$previous_digest" ]] || fail "compat image predecessor digest mismatch"
  [[ "$target" == "$target_sha" ]] || fail "compat image target release mismatch"
  [[ "$head" == "$database_head" ]] || fail "compat image database head mismatch"
  [[ "$image_os/$architecture" == "linux/amd64" ]] || fail "compat image platform mismatch"
}

railway_status_json() {
  railway status \
    --project "$RAILWAY_PROJECT_ID" \
    --environment "$RAILWAY_ENVIRONMENT" \
    --json
}

railway_service_node() {
  railway_status_json | jq \
    --arg environment "$RAILWAY_ENVIRONMENT" \
    --arg service "$1" '
      .environments.edges[]
      | select(.node.name == $environment)
      | .node.serviceInstances.edges[].node
      | select(.serviceName == $service)
    '
}

active_deployment_json() {
  railway_service_node "$1" | jq '
    .activeDeployments
    | map(select(.status == "SUCCESS"))
    | first
  '
}

railway_source_image() {
  railway_service_node "$1" | jq -r '.source.image // ""'
}

railway_environment_config_json() {
  railway api \
    'query EnvironmentConfig($id: String!, $projectId: String!) { environment(id: $id, projectId: $projectId) { config } }' \
    --variables "$(jq -cn --arg id "$RAILWAY_ENVIRONMENT_ID" --arg projectId "$RAILWAY_PROJECT_ID" '{id: $id, projectId: $projectId}')" \
    | jq -e '.data.environment.config'
}

validate_railway_image_auto_updates() {
  local service service_id auto_update_type
  for service in "${RAILWAY_SERVICES[@]}"; do
    service_id="$(railway_service_node "$service" | jq -r '.serviceId // ""')"
    [[ -n "$service_id" ]] || fail "cannot resolve Railway service ID for $service"
    auto_update_type="$(railway_environment_config_json | jq -er \
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
      ')" || fail "cannot determine Railway image auto-update state for $service"
    [[ "$auto_update_type" == "disabled" ]] \
      || fail "Railway native image auto-update must be disabled for $service, got: $auto_update_type"
  done
}

active_region() {
  railway_status_json | python3 scripts/railway_active_region.py "$RAILWAY_ENVIRONMENT" "$1"
}

validate_preflight() {
  local status_json service source region
  status_json="$(railway_status_json)"
  [[ "$(jq -r '.id // ""' <<<"$status_json")" == "$RAILWAY_PROJECT_ID" ]] \
    || fail "Railway project does not match rollback target"
  [[ "$(jq -r '.name // ""' <<<"$status_json")" == "$RAILWAY_PROJECT_NAME" ]] \
    || fail "Railway project name does not match rollback target"
  jq -e --arg environment "$RAILWAY_ENVIRONMENT" \
    '.environments.edges | any(.node.name == $environment)' >/dev/null <<<"$status_json" \
    || fail "Railway environment not found: $RAILWAY_ENVIRONMENT"
  for service in "${RAILWAY_SERVICES[@]}"; do
    source="$(railway_source_image "$service")"
    [[ "$source" == "$latest_ref" ]] \
      || fail "$service source is not $latest_ref: ${source:-missing}"
  done
  for service in "${RAILWAY_COLOCATED_SERVICES[@]}"; do
    region="$(active_region "$service")" || fail "cannot determine Railway region for $service"
    [[ "$region" == "$RAILWAY_REGION" ]] \
      || fail "$service region $region does not match $RAILWAY_REGION"
  done
  validate_railway_image_auto_updates
  uv run --frozen --no-dev python scripts/validate_railway_config.py \
    "$RAILWAY_PROJECT_ID" "$RAILWAY_ENVIRONMENT" "$PUBLIC_BASE_URL"
}

latest_deployment_json() {
  railway deployment list \
    --project "$RAILWAY_PROJECT_ID" \
    --environment "$RAILWAY_ENVIRONMENT" \
    --service "$1" \
    --limit 1 \
    --json | jq '.[0]'
}

validate_current_state() {
  local latest_digest service active digest
  latest_digest="$(image_digest "$latest_ref")"
  [[ "$latest_digest" == "$target_digest" \
    || "$latest_digest" == "$compat_digest" \
    || "$latest_digest" == "$previous_digest" ]] \
    || fail "latest points to unrelated digest: $latest_digest"
  for service in "${RAILWAY_SERVICES[@]}"; do
    active="$(active_deployment_json "$service")"
    digest="$(jq -r '.meta.imageDigest // ""' <<<"$active")"
    [[ "$digest" == "$target_digest" \
      || "$digest" == "$compat_digest" \
      || "$digest" == "$previous_digest" ]] \
      || fail "$service runs unrelated digest: ${digest:-missing}"
  done
}

require_release_mutation() {
  local latest_digest api_digest worker_digest scheduler_digest
  latest_digest="$(image_digest "$latest_ref")"
  api_digest="$(jq -r '.meta.imageDigest // ""' <<<"$(active_deployment_json api)")"
  worker_digest="$(jq -r '.meta.imageDigest // ""' <<<"$(active_deployment_json worker)")"
  scheduler_digest="$(jq -r '.meta.imageDigest // ""' <<<"$(active_deployment_json scheduler)")"
  python3 scripts/rollback_state_guard.py \
    --status "$release_status" \
    --latest "$latest_digest" \
    --api "$api_digest" \
    --worker "$worker_digest" \
    --scheduler "$scheduler_digest" \
    --previous "$previous_digest" \
    --target "$target_digest" \
    --compatibility "$compat_digest"
}

require_compat_latest() {
  local latest_digest
  latest_digest="$(image_digest "$latest_ref")"
  [[ "$latest_digest" == "$compat_digest" ]] \
    || fail "latest changed during rollback: $latest_digest"
}

wait_for_deployment() {
  local service="$1"
  local deployment_id="$2"
  local deadline=$((SECONDS + DEPLOY_TIMEOUT_SECONDS))
  local deployment status digest
  while (( SECONDS < deadline )); do
    deployment="$(railway deployment list \
      --project "$RAILWAY_PROJECT_ID" \
      --environment "$RAILWAY_ENVIRONMENT" \
      --service "$service" \
      --limit 100 \
      --json | jq --arg id "$deployment_id" 'map(select(.id == $id))[0]')"
    status="$(jq -r '.status // ""' <<<"$deployment")"
    digest="$(jq -r '.meta.imageDigest // ""' <<<"$deployment")"
    case "$status" in
      SUCCESS)
        [[ "$digest" == "$compat_digest" ]] \
          || fail "$service deployed $digest, expected $compat_digest"
        printf '%s\n' "$deployment_id"
        return 0
        ;;
      FAILED|CRASHED|REMOVED|SKIPPED|SLEEPING)
        fail "$service rollback deployment $deployment_id ended with $status"
        ;;
      QUEUED|INITIALIZING|WAITING|BUILDING|DEPLOYING|NEEDS_APPROVAL|"") ;;
      *) fail "$service rollback deployment $deployment_id returned unknown status $status" ;;
    esac
    sleep 5
  done
  fail "timed out waiting for rollback deployment: $service/$deployment_id"
}

redeploy_role() {
  local service="$1"
  local active active_id active_digest latest latest_id latest_status latest_digest
  local redeploy_output deployment_id
  validate_current_state
  require_compat_latest
  active="$(active_deployment_json "$service")"
  active_id="$(jq -r '.id // ""' <<<"$active")"
  active_digest="$(jq -r '.meta.imageDigest // ""' <<<"$active")"
  if [[ "$active_digest" == "$compat_digest" ]]; then
    printf '%s\n' "$active_id"
    return 0
  fi
  [[ "$active_digest" == "$target_digest" || "$active_digest" == "$previous_digest" ]] \
    || fail "$service cannot rollback from $active_digest"

  latest="$(latest_deployment_json "$service")"
  latest_id="$(jq -r '.id // ""' <<<"$latest")"
  latest_status="$(jq -r '.status // ""' <<<"$latest")"
  latest_digest="$(jq -r '.meta.imageDigest // ""' <<<"$latest")"
  if [[ -n "$latest_id" && "$latest_id" != "$active_id" ]]; then
    if [[ "$latest_digest" == "$compat_digest" ]]; then
      case "$latest_status" in
        SUCCESS)
          printf '%s\n' "$latest_id"
          return 0
          ;;
        QUEUED|INITIALIZING|WAITING|BUILDING|DEPLOYING|NEEDS_APPROVAL)
          log "resuming in-flight $service rollback deployment: $latest_id"
          wait_for_deployment "$service" "$latest_id" >/dev/null
          printf '%s\n' "$latest_id"
          return 0
          ;;
        FAILED|CRASHED|REMOVED|SKIPPED|SLEEPING) ;;
        *) fail "$service rollback deployment has unknown status $latest_status" ;;
      esac
    elif [[ "$latest_status" =~ ^(QUEUED|INITIALIZING|WAITING|BUILDING|DEPLOYING|NEEDS_APPROVAL)$ ]]; then
      fail "$service has unresolved in-flight rollback deployment $latest_id with digest ${latest_digest:-unknown}"
    fi
  fi

  require_compat_latest
  log "redeploying $service from migration-compatible latest"
  redeploy_output="$(railway redeploy \
    --project "$RAILWAY_PROJECT_ID" \
    --environment "$RAILWAY_ENVIRONMENT" \
    --service "$service" \
    --from-source \
    --yes \
    --json)" || fail "Railway rollback redeploy request failed for $service; inspect state before retrying"
  deployment_id="$(jq -r '.id // .deploymentId // ""' <<<"$redeploy_output")"
  if [[ -z "$deployment_id" ]]; then
    deployment_id="$(jq -r '.id // ""' <<<"$(latest_deployment_json "$service")")"
    [[ -n "$deployment_id" && "$deployment_id" != "$latest_id" ]] \
      || fail "Railway rollback redeploy returned no trackable deployment ID for $service"
  fi
  wait_for_deployment "$service" "$deployment_id" >/dev/null
  printf '%s\n' "$deployment_id"
}

wait_for_api_health() {
  local deadline=$((SECONDS + 180))
  while (( SECONDS < deadline )); do
    if [[ "$(curl -fsS "${PUBLIC_BASE_URL}/healthz" 2>/dev/null || true)" == '{"status":"ok"}' ]]; then
      return 0
    fi
    sleep 3
  done
  fail "API health check failed after rollback"
}

[[ "$(image_digest "$compat_ref")" == "$compat_digest" ]] \
  || fail "compatibility tag digest changed"
verify_compatibility_image
validate_preflight
validate_current_state
require_release_mutation
log "retagging latest to migration-compatible predecessor digest"
docker buildx imagetools create --prefer-index=false \
  --tag "$latest_ref" "${IMAGE_REPO}@${compat_digest}" >/dev/null
[[ "$(image_digest "$latest_ref")" == "$compat_digest" ]] \
  || fail "latest does not match compatibility digest"
validate_current_state

api_deployment_id="$(redeploy_role api)"
wait_for_api_health
worker_deployment_id="$(redeploy_role worker)"
scheduler_deployment_id="$(redeploy_role scheduler)"
require_compat_latest
validate_current_state

for service in "${RAILWAY_SERVICES[@]}"; do
  deployment="$(active_deployment_json "$service")"
  [[ "$(jq -r '.status // ""' <<<"$deployment")" == "SUCCESS" ]] \
    || fail "$service is not SUCCESS after rollback"
  [[ "$(jq -r '.meta.imageDigest // ""' <<<"$deployment")" == "$compat_digest" ]] \
    || fail "$service does not run compatibility digest after rollback"
done

uv run --frozen --no-dev python scripts/validate_railway_config.py \
  "$RAILWAY_PROJECT_ID" "$RAILWAY_ENVIRONMENT" "$PUBLIC_BASE_URL"
validate_railway_image_auto_updates
require_compat_latest

rollback_manifest="dist/rollback-${target_sha}.json"
jq -n \
  --arg target_sha "$target_sha" \
  --arg target_digest "$target_digest" \
  --arg compatibility_ref "$compat_ref" \
  --arg compatibility_digest "$compat_digest" \
  --arg predecessor_app_revision "$previous_app_revision" \
  --arg database_head "$database_head" \
  --arg api_deployment_id "$api_deployment_id" \
  --arg worker_deployment_id "$worker_deployment_id" \
  --arg scheduler_deployment_id "$scheduler_deployment_id" \
  '{
    status: "completed",
    target_sha: $target_sha,
    target_digest: $target_digest,
    compatibility_ref: $compatibility_ref,
    compatibility_digest: $compatibility_digest,
    predecessor_app_revision: $predecessor_app_revision,
    database_head: $database_head,
    railway: {
      api_deployment_id: $api_deployment_id,
      worker_deployment_id: $worker_deployment_id,
      scheduler_deployment_id: $scheduler_deployment_id
    }
  }' >"$rollback_manifest"

log "rollback complete: $compat_ref -> $compat_digest"
log "rollback manifest: $rollback_manifest"
