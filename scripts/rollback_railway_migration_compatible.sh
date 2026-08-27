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
readonly LEGACY_BUSINESS_PROMPT_CAPABILITY="legacy"
readonly TARGET_BUSINESS_PROMPT_CAPABILITY="editable-business-prompt-v1"
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

for command_name in git docker railway jq curl uv python3 awk mktemp rm; do
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
compat_required="$(jq -r '.migration_compatible_rollback.required // false' "$manifest_path")"
target_digest="$(jq -r '.digest // ""' "$manifest_path")"
previous_digest="$(jq -r '.previous_digest // ""' "$manifest_path")"
target_sha="$(jq -r '.git_sha // ""' "$manifest_path")"
previous_app_revision="$(jq -r '.migration_compatible_rollback.predecessor_app_revision // ""' "$manifest_path")"
database_head="$(jq -r '.migration_compatible_rollback.database_head // ""' "$manifest_path")"
previous_business_prompt_capability="$(
  jq -r '.business_prompt_contract.previous // ""' "$manifest_path"
)"
target_business_prompt_capability="$(
  jq -r '.business_prompt_contract.target // ""' "$manifest_path"
)"
latest_ref="${IMAGE_REPO}:latest"
expected_compat_ref="${IMAGE_REPO}:railway-compat-pre-${target_sha:0:12}"
business_prompt_retirement_evidence_path="dist/rollback-${target_sha}-business-prompt-retirement.json"

[[ "$release_status" == "deploying" || "$release_status" == "completed" ]] \
  || fail "release manifest status is not rollback-eligible: $release_status"
[[ "$compat_required" == "true" ]] \
  || fail "release manifest does not require a migration-compatible rollback"
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
[[ "$database_head" =~ ^[0-9a-f]{12,64}$ ]] || fail "unexpected compatibility DB head"
case "$previous_business_prompt_capability" in
  "$LEGACY_BUSINESS_PROMPT_CAPABILITY"|"$TARGET_BUSINESS_PROMPT_CAPABILITY") ;;
  *) fail "invalid predecessor business Prompt capability in release manifest" ;;
esac
[[ "$target_business_prompt_capability" == "$TARGET_BUSINESS_PROMPT_CAPABILITY" ]] \
  || fail "release manifest does not describe the editable business Prompt target"

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

verify_compatibility_image() {
  local metadata revision source purpose base target head image_os architecture
  local business_prompt_capability
  metadata="$(image_metadata "${IMAGE_REPO}@${compat_digest}")"
  revision="$(jq -r '.config.Labels["org.opencontainers.image.revision"] // ""' <<<"$metadata")"
  source="$(jq -r '.config.Labels["org.opencontainers.image.source"] // ""' <<<"$metadata")"
  purpose="$(jq -r '.config.Labels["com.nexory.reply-core.rollback-purpose"] // ""' <<<"$metadata")"
  base="$(jq -r '.config.Labels["com.nexory.reply-core.rollback-base-digest"] // ""' <<<"$metadata")"
  target="$(jq -r '.config.Labels["com.nexory.reply-core.rollback-target-release"] // ""' <<<"$metadata")"
  head="$(jq -r '.config.Labels["com.nexory.reply-core.database-head"] // ""' <<<"$metadata")"
  image_os="$(jq -r '.os // ""' <<<"$metadata")"
  architecture="$(jq -r '.architecture // ""' <<<"$metadata")"
  business_prompt_capability="$(jq -r \
    '.config.Labels["com.nexory.reply-core.business-prompt-contract"] // "legacy"' \
    <<<"$metadata")"
  [[ "$revision" == "$previous_app_revision" ]] || fail "compat image app revision mismatch"
  [[ "$source" == "$SOURCE_URL" ]] || fail "compat image source mismatch"
  [[ "$purpose" == "migration-compatible-predecessor" ]] || fail "compat image purpose mismatch"
  [[ "$base" == "$previous_digest" ]] || fail "compat image predecessor digest mismatch"
  [[ "$target" == "$target_sha" ]] || fail "compat image target release mismatch"
  [[ "$head" == "$database_head" ]] || fail "compat image database head mismatch"
  [[ "$business_prompt_capability" == "$previous_business_prompt_capability" ]] \
    || fail "compat image business Prompt capability mismatch"
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
  service_id="$(railway_service_node "$service" | jq -r '.serviceId // ""')"
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

business_prompt_retirement_required() {
  local observed_target_capability observed_compatibility_capability
  observed_target_capability="$(image_business_prompt_capability "${IMAGE_REPO}@${target_digest}")"
  observed_compatibility_capability="$(
    image_business_prompt_capability "${IMAGE_REPO}@${compat_digest}"
  )"
  [[ "$observed_target_capability" == "$target_business_prompt_capability" ]] \
    || fail "target image business Prompt capability changed"
  [[ "$observed_compatibility_capability" == "$previous_business_prompt_capability" ]] \
    || fail "compatibility image business Prompt capability changed"
  case "${observed_compatibility_capability}:${observed_target_capability}" in
    "${LEGACY_BUSINESS_PROMPT_CAPABILITY}:${TARGET_BUSINESS_PROMPT_CAPABILITY}")
      return 0
      ;;
    "${TARGET_BUSINESS_PROMPT_CAPABILITY}:${TARGET_BUSINESS_PROMPT_CAPABILITY}")
      return 1
      ;;
    *) fail "unsupported business Prompt rollback capability transition" ;;
  esac
}

business_prompt_retirement_evidence_valid() {
  [[ -f "$business_prompt_retirement_evidence_path" ]] || return 1
  jq -e \
    --arg target_sha "$target_sha" \
    --arg target_digest "$target_digest" \
    --arg compatibility_digest "$compat_digest" \
    --arg previous_capability "$previous_business_prompt_capability" \
    --arg target_capability "$target_business_prompt_capability" '
      .status == "completed"
      and .target_sha == $target_sha
      and .target_digest == $target_digest
      and .compatibility_digest == $compatibility_digest
      and .previous_capability == $previous_capability
      and .target_capability == $target_capability
    ' "$business_prompt_retirement_evidence_path" >/dev/null
}

write_business_prompt_retirement_evidence() {
  local method="$1"
  local report_json="$2"
  local evidence_tmp
  evidence_tmp="$(mktemp "${business_prompt_retirement_evidence_path}.tmp.XXXXXX")"
  jq -n \
    --arg status completed \
    --arg target_sha "$target_sha" \
    --arg target_digest "$target_digest" \
    --arg compatibility_digest "$compat_digest" \
    --arg previous_capability "$previous_business_prompt_capability" \
    --arg target_capability "$target_business_prompt_capability" \
    --arg method "$method" \
    --argjson report "$report_json" \
    '{
      status: $status,
      target_sha: $target_sha,
      target_digest: $target_digest,
      compatibility_digest: $compatibility_digest,
      previous_capability: $previous_capability,
      target_capability: $target_capability,
      method: $method,
      report: $report
    }' >"$evidence_tmp" \
    || { rm -f "$evidence_tmp"; fail "could not render Prompt retirement evidence"; }
  python3 - "$evidence_tmp" "$business_prompt_retirement_evidence_path" <<'PY'
import os
import sys

source, destination = sys.argv[1:]
with open(source, "rb") as evidence_file:
    os.fsync(evidence_file.fileno())
os.replace(source, destination)
directory_fd = os.open(os.path.dirname(destination) or ".", os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
}

retire_business_prompt_work() {
  local gate_value retirement_service service active_digest output
  local existing_evidence_valid="false"
  if [[ -e "$business_prompt_retirement_evidence_path" ]]; then
    business_prompt_retirement_evidence_valid \
      || fail "existing Prompt retirement evidence is invalid"
    existing_evidence_valid="true"
  fi

  gate_value="$(railway_service_variable api REPLY_BUSINESS_PROMPT_ENABLED)"
  case "$gate_value" in
    true|false) ;;
    *) fail "invalid stored REPLY_BUSINESS_PROMPT_ENABLED value" ;;
  esac

  retirement_service=""
  for service in api worker scheduler; do
    active_digest="$(jq -r '.meta.imageDigest // ""' <<<"$(active_deployment_json "$service")")"
    if [[ "$active_digest" == "$target_digest" ]]; then
      retirement_service="$service"
      break
    fi
  done

  if [[ -n "$retirement_service" && "$gate_value" == "true" ]]; then
    log "disabling editable Prompt runtime on the target digest before rollback"
    scripts/set_reply_business_prompt_gate.sh --disable
    validate_current_state
    gate_value="false"
  fi

  if [[ -n "$retirement_service" ]]; then
    if [[ "$existing_evidence_valid" == "true" ]]; then
      log "target runtime is active; refreshing Prompt retirement evidence"
    fi
    log "retiring all unsent Prompt-derived Outboxes and pending drafts"
    output="$(railway ssh \
      --project "$RAILWAY_PROJECT_ID" \
      --environment "$RAILWAY_ENVIRONMENT" \
      --service "$retirement_service" \
      python -m apps.cli.retire_reply_business_prompt_work)" \
      || fail "could not retire Prompt-derived work before rollback"
    jq -e '.status == "ok"' >/dev/null <<<"$output" \
      || fail "Prompt work retirement returned invalid evidence"
    write_business_prompt_retirement_evidence \
      "target-${retirement_service}-retirement" "$output"
    log "Prompt work retirement evidence: $output"
    return 0
  fi

  [[ "$gate_value" == "false" ]] \
    || fail "editable Prompt gate is enabled but no target runtime can retire work"
  if [[ "$existing_evidence_valid" == "true" ]]; then
    log "reusing completed Prompt retirement evidence after target runtime became unavailable"
    return 0
  fi

  if [[ "$release_status" == "deploying" && "$gate_value" == "false" ]]; then
    write_business_prompt_retirement_evidence "pre-activation-gate-disabled" null
    log "Prompt retirement is unnecessary before activation completed"
    return 0
  fi
  fail "no target service is available and no completed Prompt retirement evidence exists"
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
      QUEUED|INITIALIZING|WAITING|BUILDING|DEPLOYING|NEEDS_APPROVAL|REMOVING|"") ;;
      *) fail "$service rollback deployment $deployment_id returned unknown status $status" ;;
    esac
    sleep 5
  done
  fail "timed out waiting for rollback deployment: $service/$deployment_id"
}

redeploy_role() {
  local service="$1"
  local active active_id active_digest latest latest_id latest_status latest_digest
  local redeploy_output deployment_id replaced_deployment_ids
  validate_current_state
  require_compat_latest
  active="$(active_deployment_json "$service")"
  active_id="$(jq -r '.id // ""' <<<"$active")"
  active_digest="$(jq -r '.meta.imageDigest // ""' <<<"$active")"
  if [[ "$active_digest" == "$compat_digest" ]]; then
    replaced_deployment_ids="$(unstopped_deployment_ids "$service")"
    wait_for_replaced_deployments_to_stop \
      "$service" "$active_id" "$replaced_deployment_ids"
    role_runtime_converged "$service" "$active_id" "$compat_digest"
    printf '%s\n' "$active_id"
    return 0
  fi
  [[ "$active_digest" == "$target_digest" || "$active_digest" == "$previous_digest" ]] \
    || fail "$service cannot rollback from $active_digest"

  latest="$(latest_deployment_json "$service")"
  latest_id="$(jq -r '.id // ""' <<<"$latest")"
  latest_status="$(jq -r '.status // ""' <<<"$latest")"
  latest_digest="$(jq -r '.meta.imageDigest // ""' <<<"$latest")"
  replaced_deployment_ids="$(unstopped_deployment_ids "$service")"
  if [[ -n "$latest_id" && "$latest_id" != "$active_id" ]]; then
    if [[ "$latest_digest" == "$compat_digest" ]]; then
      case "$latest_status" in
        SUCCESS)
          wait_for_replaced_deployments_to_stop \
            "$service" "$latest_id" "$replaced_deployment_ids"
          role_runtime_converged "$service" "$latest_id" "$compat_digest"
          printf '%s\n' "$latest_id"
          return 0
          ;;
        QUEUED|INITIALIZING|WAITING|BUILDING|DEPLOYING|NEEDS_APPROVAL)
          log "resuming in-flight $service rollback deployment: $latest_id"
          wait_for_deployment "$service" "$latest_id" >/dev/null
          wait_for_replaced_deployments_to_stop \
            "$service" "$latest_id" "$replaced_deployment_ids"
          role_runtime_converged "$service" "$latest_id" "$compat_digest"
          printf '%s\n' "$latest_id"
          return 0
          ;;
        FAILED|CRASHED|REMOVED|SKIPPED|SLEEPING) ;;
        REMOVING) fail "$service compatibility deployment is being removed: $latest_id" ;;
        *) fail "$service rollback deployment has unknown status $latest_status" ;;
      esac
    elif [[ "$latest_status" =~ ^(QUEUED|INITIALIZING|WAITING|BUILDING|DEPLOYING|NEEDS_APPROVAL|REMOVING)$ ]]; then
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
  wait_for_replaced_deployments_to_stop \
    "$service" "$deployment_id" "$replaced_deployment_ids"
  role_runtime_converged "$service" "$deployment_id" "$compat_digest"
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
if business_prompt_retirement_required; then
  retire_business_prompt_work
elif ! business_prompt_retirement_evidence_valid; then
  [[ ! -e "$business_prompt_retirement_evidence_path" ]] \
    || fail "existing Prompt retirement evidence is invalid"
  write_business_prompt_retirement_evidence "same-capability-not-required" null
fi
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
  role_runtime_converged \
    "$service" "$(jq -r '.id // ""' <<<"$deployment")" "$compat_digest"
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
  --slurpfile business_prompt_retirement "$business_prompt_retirement_evidence_path" \
  '{
    status: "completed",
    target_sha: $target_sha,
    target_digest: $target_digest,
    compatibility_ref: $compatibility_ref,
    compatibility_digest: $compatibility_digest,
    predecessor_app_revision: $predecessor_app_revision,
    database_head: $database_head,
    business_prompt_retirement: $business_prompt_retirement[0],
    railway: {
      api_deployment_id: $api_deployment_id,
      worker_deployment_id: $worker_deployment_id,
      scheduler_deployment_id: $scheduler_deployment_id
    }
  }' >"$rollback_manifest"

log "rollback complete: $compat_ref -> $compat_digest"
log "rollback manifest: $rollback_manifest"
