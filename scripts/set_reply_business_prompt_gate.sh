#!/usr/bin/env bash
set -Eeuo pipefail

readonly IMAGE_REPO="ghcr.io/junqingyongyuanbusi/reply-core"
readonly RAILWAY_PROJECT_ID="abcf3199-e5ac-415b-a22e-062206390331"
readonly RAILWAY_ENVIRONMENT="production"
readonly RAILWAY_ENVIRONMENT_ID="db0d6750-eb77-40ee-8a79-f706cd1f828a"
readonly PUBLIC_BASE_URL="https://relay.nexory.top"
readonly LEGACY_BUSINESS_PROMPT_CAPABILITY="legacy"
readonly TARGET_BUSINESS_PROMPT_CAPABILITY="editable-business-prompt-v1"
readonly DEPLOY_TIMEOUT_SECONDS="${DEPLOY_TIMEOUT_SECONDS:-900}"
readonly RECOVERY_TIMEOUT_SECONDS="${RECOVERY_TIMEOUT_SECONDS:-1800}"
readonly RAILWAY_SERVICES=(api worker scheduler)

fail() {
  printf '[prompt-gate] ERROR: %s\n' "$*" >&2
  exit 1
}

log() {
  printf '[prompt-gate] %s\n' "$*" >&2
}

usage() {
  printf 'usage: %s --enable|--disable\n' "$0" >&2
}

[[ $# -eq 1 ]] || { usage; exit 2; }
case "$1" in
  --enable)
    target_value="true"
    rollout_order=(worker scheduler api)
    ;;
  --disable)
    target_value="false"
    rollout_order=(worker api scheduler)
    ;;
  *)
    usage
    exit 2
    ;;
esac

for command_name in git docker railway jq curl uv python3 awk; do
  command -v "$command_name" >/dev/null 2>&1 \
    || fail "required command not found: $command_name"
done
[[ "$DEPLOY_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] \
  || fail "DEPLOY_TIMEOUT_SECONDS must be a positive integer"
[[ "$RECOVERY_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] \
  || fail "RECOVERY_TIMEOUT_SECONDS must be a positive integer"

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" \
  || fail "not inside a Git repository"
cd "$repo_root"
mkdir -p .run dist
release_lock="$repo_root/.run/publish-railway-release.lock"
script_path="$repo_root/scripts/set_reply_business_prompt_gate.sh"
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
        print(
            f"[prompt-gate] ERROR: another local Railway release or rollback is running: {lock_path}",
            file=sys.stderr,
        )
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
fd_stat = os.fstat(fd)
path_stat = os.stat(lock_path)
if (fd_stat.st_dev, fd_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
    raise SystemExit("[prompt-gate] ERROR: inherited descriptor does not match release lock")
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
PY
fi

[[ "$(git branch --show-current)" == "dev" ]] || fail "gate changes require dev"
[[ -z "$(git status --porcelain)" ]] || fail "worktree must be clean"
git fetch --quiet origin dev
full_sha="$(git rev-parse HEAD)"
[[ "$full_sha" == "$(git rev-parse origin/dev)" ]] \
  || fail "HEAD must equal origin/dev"
[[ -f migrations/versions/b9d5e2f7c314_editable_reply_business_prompts.py ]] \
  || fail "editable business Prompt migration is not present"

image_digest() {
  local output digest
  output="$(docker buildx imagetools inspect "$1")" \
    || fail "cannot inspect image: $1"
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
    .activeDeployments | map(select(.status == "SUCCESS")) | first
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
  [[ -n "$service_id" ]] || return 1
  while true; do
    response="$(railway api \
      'query ServiceDeployments($input: DeploymentListInput!, $first: Int!, $after: String) { deployments(input: $input, first: $first, after: $after) { edges { node { id status deploymentStopped } } pageInfo { hasNextPage endCursor } } }' \
      --variables "$(jq -cn \
        --arg projectId "$RAILWAY_PROJECT_ID" \
        --arg environmentId "$RAILWAY_ENVIRONMENT_ID" \
        --arg serviceId "$service_id" \
        --arg after "$after_cursor" \
        '{input:{projectId:$projectId,environmentId:$environmentId,serviceId:$serviceId},first:100,after:(if $after == "" then null else $after end)}')")" \
      || return 1
    jq -e '.data.deployments.edges and .data.deployments.pageInfo' \
      >/dev/null <<<"$response" || return 1
    page="$(jq '[.data.deployments.edges[].node]' <<<"$response")" || return 1
    accumulated="$(jq -cn \
      --argjson accumulated "$accumulated" \
      --argjson page "$page" \
      '$accumulated + $page')" || return 1
    has_next_page="$(jq -r '.data.deployments.pageInfo.hasNextPage' <<<"$response")"
    [[ "$has_next_page" == "true" ]] || break
    next_cursor="$(jq -r '.data.deployments.pageInfo.endCursor // ""' <<<"$response")"
    [[ -n "$next_cursor" && "$next_cursor" != "$after_cursor" ]] || return 1
    after_cursor="$next_cursor"
  done
  printf '%s\n' "$accumulated"
}

unstopped_deployment_ids() {
  service_deployment_runtimes_json "$1" | jq -r '
    .[] | select(.deploymentStopped == false) | .id
  '
}

latest_deployment_json() {
  railway deployment list \
    --project "$RAILWAY_PROJECT_ID" \
    --environment "$RAILWAY_ENVIRONMENT" \
    --service "$1" \
    --limit 1 \
    --json | jq '.[0]'
}

service_variable() {
  railway variable list \
    --project "$RAILWAY_PROJECT_ID" \
    --environment "$RAILWAY_ENVIRONMENT" \
    --service "$1" \
    --json | jq -er '.REPLY_BUSINESS_PROMPT_ENABLED'
}

set_all_variables() {
  local value="$1"
  local service
  for service in "${RAILWAY_SERVICES[@]}"; do
    railway variable set \
      --project "$RAILWAY_PROJECT_ID" \
      --environment "$RAILWAY_ENVIRONMENT" \
      --service "$service" \
      --skip-deploys \
      "REPLY_BUSINESS_PROMPT_ENABLED=${value}" >/dev/null \
      || return 1
  done
}

stored_gate_values_equal() {
  local expected_value="$1"
  local service observed_value
  for service in "${RAILWAY_SERVICES[@]}"; do
    observed_value="$(service_variable "$service")" || return 1
    [[ "$observed_value" == "$expected_value" ]] || return 1
  done
}

restore_stored_gate_values() {
  local expected_value="$1"
  local attempt
  for attempt in 1 2 3; do
    if set_all_variables "$expected_value" \
      && stored_gate_values_equal "$expected_value"; then
      return 0
    fi
    log "stored gate restoration attempt $attempt failed"
    sleep 3
  done
  return 1
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

cancel_deployment() {
  local deployment_id="$1"
  local response
  response="$(railway api \
    'mutation CancelDeployment($id: String!) { deploymentCancel(id: $id) }' \
    --variables "$(jq -cn --arg id "$deployment_id" '{id: $id}')")" \
    || return 1
  jq -e '.data.deploymentCancel == true' >/dev/null <<<"$response"
}

wait_for_deployment_terminal() {
  local service="$1"
  local deployment_id="$2"
  local deadline=$((SECONDS + RECOVERY_TIMEOUT_SECONDS))
  local deployment status
  while (( SECONDS < deadline )); do
    deployment="$(railway deployment list \
      --project "$RAILWAY_PROJECT_ID" \
      --environment "$RAILWAY_ENVIRONMENT" \
      --service "$service" \
      --limit 100 \
      --json | jq --arg id "$deployment_id" 'map(select(.id == $id))[0]')" \
      || return 1
    status="$(jq -r '.status // ""' <<<"$deployment")"
    case "$status" in
      SUCCESS|FAILED|CRASHED|REMOVED|SKIPPED|SLEEPING)
        printf '%s\n' "$status"
        return 0
        ;;
      QUEUED|INITIALIZING|WAITING|BUILDING|DEPLOYING|NEEDS_APPROVAL|REMOVING|"") ;;
      *) return 1 ;;
    esac
    sleep 5
  done
  return 1
}

resolve_in_flight_deployments_for_recovery() {
  local service="$1"
  local deployment_ids deployment_id deployment_status terminal_status
  deployment_ids="$(in_progress_deployment_ids "$service")" || return 1
  [[ -n "$deployment_ids" ]] || return 0
  while IFS= read -r deployment_id; do
    [[ -n "$deployment_id" ]] || continue
    deployment_status="$(jq -r '.status // ""' \
      <<<"$(deployment_runtime_json "$deployment_id")")" || return 1
    if [[ "$deployment_status" != "REMOVING" ]]; then
      log "cancelling in-flight $service deployment before recovery: $deployment_id"
      if ! cancel_deployment "$deployment_id"; then
        log "cancel request was not accepted for $service/$deployment_id; waiting for terminal state"
      fi
    fi
    terminal_status="$(wait_for_deployment_terminal "$service" "$deployment_id")" \
      || return 1
    log "$service deployment $deployment_id reached $terminal_status before recovery"
  done <<<"$deployment_ids"
  [[ -z "$(in_progress_deployment_ids "$service")" ]]
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
      runtime="$(deployment_runtime_json "$deployment_id")" || return 1
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
        *) return 1 ;;
      esac
      sleep 5
    done
    [[ "$deployment_stopped" == "true" ]] || return 1
  done <<<"$replaced_deployment_ids"
}

role_runtime_converged() {
  local service="$1"
  local expected_deployment_id="$2"
  local expected_digest="$3"
  local active_deployments deployment_runtimes
  active_deployments="$(railway_service_node "$service" | jq \
    '[.activeDeployments[] | select(.status == "SUCCESS")]')" || return 1
  [[ "$(jq 'length' <<<"$active_deployments")" == "1" ]] || return 1
  [[ "$(jq -r '.[0].id // ""' <<<"$active_deployments")" == "$expected_deployment_id" ]] \
    || return 1
  [[ "$(jq -r '.[0].meta.imageDigest // ""' <<<"$active_deployments")" == "$expected_digest" ]] \
    || return 1
  deployment_runtimes="$(service_deployment_runtimes_json "$service")" || return 1
  jq -e --arg expected "$expected_deployment_id" '
    [.[] | select(.deploymentStopped == false)]
    | length == 1
      and .[0].id == $expected
      and .[0].status == "SUCCESS"
  ' >/dev/null <<<"$deployment_runtimes" || return 1
  [[ -z "$(in_progress_deployment_ids "$service")" ]]
}

active_runtime_matches_digest() {
  local expected_digest="$1"
  local service active_deployments active_digest active_id
  for service in "${RAILWAY_SERVICES[@]}"; do
    active_deployments="$(railway_service_node "$service" | jq \
      '[.activeDeployments[] | select(.status == "SUCCESS")]')" || return 1
    [[ "$(jq 'length' <<<"$active_deployments")" == "1" ]] || return 1
    active_digest="$(jq -r '.[0].meta.imageDigest // ""' \
      <<<"$active_deployments")" || return 1
    active_id="$(jq -r '.[0].id // ""' <<<"$active_deployments")" || return 1
    [[ "$active_digest" == "$expected_digest" ]] || return 1
    role_runtime_converged "$service" "$active_id" "$expected_digest" || return 1
  done
}

wait_for_deployment() {
  local service="$1"
  local deployment_id="$2"
  local expected_digest="$3"
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
        [[ "$digest" == "$expected_digest" ]] \
          || fail "$service deployed $digest, expected $expected_digest"
        return 0
        ;;
      FAILED|CRASHED|REMOVED|SKIPPED|SLEEPING)
        fail "$service deployment $deployment_id ended with $status"
        ;;
      QUEUED|INITIALIZING|WAITING|BUILDING|DEPLOYING|NEEDS_APPROVAL|REMOVING|"") ;;
      *) fail "$service deployment returned unknown status $status" ;;
    esac
    sleep 5
  done
  fail "timed out waiting for $service deployment $deployment_id"
}

force_redeploy_role() {
  local service="$1"
  local expected_digest="$2"
  local latest latest_id latest_status output deployment_id replaced_deployment_ids
  latest="$(latest_deployment_json "$service")"
  latest_id="$(jq -r '.id // ""' <<<"$latest")"
  latest_status="$(jq -r '.status // ""' <<<"$latest")"
  if [[ "$latest_status" =~ ^(QUEUED|INITIALIZING|WAITING|BUILDING|DEPLOYING|NEEDS_APPROVAL|REMOVING)$ ]]; then
    fail "$service has unresolved deployment $latest_id"
  fi
  replaced_deployment_ids="$(unstopped_deployment_ids "$service")" \
    || fail "could not identify active $service deployments"
  [[ -n "$replaced_deployment_ids" ]] || fail "$service has no active deployment"
  log "forcing $service redeploy with REPLY_BUSINESS_PROMPT_ENABLED=$target_value"
  output="$(railway redeploy \
    --project "$RAILWAY_PROJECT_ID" \
    --environment "$RAILWAY_ENVIRONMENT" \
    --service "$service" \
    --from-source \
    --yes \
    --json)" || fail "could not redeploy $service"
  deployment_id="$(jq -r '.id // .deploymentId // ""' <<<"$output")"
  if [[ -z "$deployment_id" ]]; then
    deployment_id="$(jq -r '.id // ""' <<<"$(latest_deployment_json "$service")")"
    [[ -n "$deployment_id" && "$deployment_id" != "$latest_id" ]] \
      || fail "could not identify $service deployment"
  fi
  wait_for_deployment "$service" "$deployment_id" "$expected_digest"
  wait_for_replaced_deployments_to_stop \
    "$service" "$deployment_id" "$replaced_deployment_ids" \
    || fail "$service replaced deployment did not stop"
  role_runtime_converged "$service" "$deployment_id" "$expected_digest" \
    || fail "$service runtime did not converge on deployment $deployment_id"
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
  return 1
}

uv run --frozen --no-dev python scripts/validate_railway_config.py \
  "$RAILWAY_PROJECT_ID" "$RAILWAY_ENVIRONMENT" "$PUBLIC_BASE_URL" \
  || fail "Railway configuration is invalid before gate rollout"

old_value="$(service_variable api)"
[[ "$old_value" == "true" || "$old_value" == "false" ]] \
  || fail "invalid current gate value"
for service in worker scheduler; do
  [[ "$(service_variable "$service")" == "$old_value" ]] \
    || fail "current gate value differs across services"
done

variable_mutation_started="false"
rollout_completed="false"
recover_previous_gate() {
  local exit_status=$?
  local recovery_failed="false"
  if [[ "$exit_status" -eq 0 || "$variable_mutation_started" != "true" \
    || "$rollout_completed" == "true" ]]; then
    return "$exit_status"
  fi
  trap - EXIT
  log "gate rollout failed; restoring stored value $old_value and redeploying all roles"
  if restore_stored_gate_values "$old_value"; then
    if [[ "$old_value" == "true" ]]; then
      recovery_order=(worker scheduler api)
    else
      recovery_order=(worker api scheduler)
    fi
    for recovery_service in "${recovery_order[@]}"; do
      if ! resolve_in_flight_deployments_for_recovery "$recovery_service"; then
        log "automatic recovery could not resolve deployments for $recovery_service"
        recovery_failed="true"
        continue
      fi
      if ! (
          target_value="$old_value"
          force_redeploy_role "$recovery_service" "$expected_digest" >/dev/null
        ); then
        log "automatic recovery redeploy failed for $recovery_service"
        recovery_failed="true"
      fi
      if [[ "$recovery_service" == "api" ]]; then
        if ! wait_for_api_health; then
          log "automatic recovery API health check failed"
          recovery_failed="true"
        fi
      fi
    done
  else
    log "automatic recovery could not restore all stored variables"
    recovery_failed="true"
  fi
  if ! stored_gate_values_equal "$old_value"; then
    log "automatic recovery stored gate values are not consistent"
    recovery_failed="true"
  fi
  if ! active_runtime_matches_digest "$expected_digest"; then
    log "automatic recovery runtime deployments did not converge"
    recovery_failed="true"
  fi
  if ! uv run --frozen --no-dev python scripts/validate_railway_config.py \
    "$RAILWAY_PROJECT_ID" "$RAILWAY_ENVIRONMENT" "$PUBLIC_BASE_URL"; then
    log "automatic recovery Railway configuration validation failed"
    recovery_failed="true"
  fi
  if [[ "$recovery_failed" == "true" ]]; then
    log "ERROR: gate recovery is incomplete; production requires manual reconciliation"
  else
    log "gate recovery completed: all roles restored to $old_value on $expected_digest"
  fi
  exit "$exit_status"
}
trap recover_previous_gate EXIT

expected_digest="$(image_digest "${IMAGE_REPO}:latest")"
latest_business_prompt_capability="$(
  image_business_prompt_capability "${IMAGE_REPO}@${expected_digest}"
)"
if [[ "$target_value" == "true" ]]; then
  immutable_sha_digest="$(image_digest "${IMAGE_REPO}:${full_sha}")"
  [[ "$immutable_sha_digest" == "$expected_digest" ]] \
    || fail "latest digest does not match the immutable HEAD image"
  [[ "$latest_business_prompt_capability" == "$TARGET_BUSINESS_PROMPT_CAPABILITY" ]] \
    || fail "cannot enable editable Prompt on a legacy application image"
  latest_revision="$(jq -r '.config.Labels["org.opencontainers.image.revision"] // ""' \
    <<<"$(image_metadata "${IMAGE_REPO}@${expected_digest}")")"
  [[ "$latest_revision" == "$full_sha" ]] \
    || fail "enable requires deployed latest revision to equal HEAD"
fi
for service in "${RAILWAY_SERVICES[@]}"; do
  source_image="$(railway_service_node "$service" | jq -r '.source.image // ""')"
  active="$(active_deployment_json "$service")"
  active_id="$(jq -r '.id // ""' <<<"$active")"
  active_digest="$(jq -r '.meta.imageDigest // ""' <<<"$active")"
  [[ "$source_image" == "${IMAGE_REPO}:latest" ]] \
    || fail "$service source is not ${IMAGE_REPO}:latest"
  [[ "$active_digest" == "$expected_digest" ]] \
    || fail "$service is not running the current latest digest"
  role_runtime_converged "$service" "$active_id" "$expected_digest" \
    || fail "$service has overlapping or unresolved deployments before gate rollout"
done

variable_mutation_started="true"
if ! set_all_variables "$target_value"; then
  fail "could not stage the gate consistently"
fi
uv run --frozen --no-dev python scripts/validate_railway_config.py \
  "$RAILWAY_PROJECT_ID" "$RAILWAY_ENVIRONMENT" "$PUBLIC_BASE_URL" \
  || fail "staged gate configuration is invalid"

api_deployment_id=""
worker_deployment_id=""
scheduler_deployment_id=""
for service in "${rollout_order[@]}"; do
  deployment_id="$(force_redeploy_role "$service" "$expected_digest")"
  case "$service" in
    api)
      api_deployment_id="$deployment_id"
      wait_for_api_health || fail "API health check failed"
      ;;
    worker) worker_deployment_id="$deployment_id" ;;
    scheduler) scheduler_deployment_id="$deployment_id" ;;
  esac
done

for service in "${RAILWAY_SERVICES[@]}"; do
  [[ "$(service_variable "$service")" == "$target_value" ]] \
    || fail "$service stored gate value changed during rollout"
  case "$service" in
    api) final_deployment_id="$api_deployment_id" ;;
    worker) final_deployment_id="$worker_deployment_id" ;;
    scheduler) final_deployment_id="$scheduler_deployment_id" ;;
  esac
  role_runtime_converged "$service" "$final_deployment_id" "$expected_digest" \
    || fail "$service runtime changed or overlapped during gate rollout"
done
rollout_completed="true"

manifest_path="dist/reply-business-prompt-gate-${full_sha}-${target_value}.json"
jq -n \
  --arg status completed \
  --arg git_sha "$full_sha" \
  --arg digest "$expected_digest" \
  --arg gate "$target_value" \
  --arg previous_gate "$old_value" \
  --arg api_deployment_id "$api_deployment_id" \
  --arg worker_deployment_id "$worker_deployment_id" \
  --arg scheduler_deployment_id "$scheduler_deployment_id" \
  '{
    status: $status,
    git_sha: $git_sha,
    digest: $digest,
    reply_business_prompt_enabled: ($gate == "true"),
    previous_reply_business_prompt_enabled: ($previous_gate == "true"),
    railway: {
      api_deployment_id: $api_deployment_id,
      worker_deployment_id: $worker_deployment_id,
      scheduler_deployment_id: $scheduler_deployment_id
    }
  }' >"$manifest_path"

log "gate rollout complete: $old_value -> $target_value on $expected_digest"
log "gate manifest: $manifest_path"
