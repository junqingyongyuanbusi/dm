#!/usr/bin/env bash
set -Eeuo pipefail

readonly IMAGE_REPO="zhiyangxiaozi/reply-core"
readonly RAILWAY_PROJECT_ID="abcf3199-e5ac-415b-a22e-062206390331"
readonly RAILWAY_PROJECT_NAME="reply-core"
readonly RAILWAY_ENVIRONMENT="production"
readonly PUBLIC_BASE_URL="https://relay.nexory.top"
readonly SOURCE_URL="https://github.com/junqingyongyuanbusi/dm"
readonly DEPLOY_TIMEOUT_SECONDS="${DEPLOY_TIMEOUT_SECONDS:-900}"
readonly CI_TIMEOUT_SECONDS="${CI_TIMEOUT_SECONDS:-1200}"
readonly RAILWAY_SERVICES=(api worker scheduler)

usage() {
  cat <<'EOF'
Publish the current clean dev commit to Docker Hub and deploy it to Railway.

Required state:
  - current branch is dev
  - worktree is clean
  - HEAD equals origin/dev
  - every GitHub Actions CI run for HEAD succeeded
  - Docker Hub, GitHub CLI, and Railway CLI authentication are available

Fixed production targets:
  - Docker Hub: zhiyangxiaozi/reply-core
  - Railway: reply-core / production / api + worker + scheduler
  - Public API: https://relay.nexory.top

Optional environment variables:
  BUILDX_BUILDER             Explicit Docker Buildx builder
  DEPLOY_TIMEOUT_SECONDS     Per-service deployment timeout (default: 900)
  CI_TIMEOUT_SECONDS         CI wait timeout (default: 1200)
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi
if [[ $# -ne 0 ]]; then
  usage >&2
  exit 2
fi

log() {
  printf '[release] %s\n' "$*" >&2
}

fail() {
  printf '[release] ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

for command_name in git docker railway gh jq curl date awk python3; do
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
if [[ "${DM_RELEASE_LOCK_HELD:-}" != "1" ]]; then
  release_lock="$repo_root/.run/publish-railway-release.lock"
  script_path="$repo_root/scripts/publish_railway_release.sh"
  exec python3 - "$release_lock" "$script_path" "$@" <<'PY'
import fcntl
import os
import sys

lock_path, script_path, *args = sys.argv[1:]
with open(lock_path, "a+", encoding="ascii") as lock_file:
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"[release] ERROR: another local Railway release is already running: {lock_path}", file=sys.stderr)
        raise SystemExit(1)
    os.set_inheritable(lock_file.fileno(), True)
    environment = dict(os.environ)
    environment["DM_RELEASE_LOCK_HELD"] = "1"
    environment["DM_RELEASE_LOCK_FD"] = str(lock_file.fileno())
    os.execve(script_path, [script_path, *args], environment)
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
manifest_path="dist/release-${full_sha}.json"
revalidate_dev_head

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
  if [[ "$output" == "ERROR: ${reference}: not found" \
    || "$output" == "ERROR: docker.io/${reference}: not found" ]]; then
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

verify_sha_image() {
  local reference="$1"
  local metadata revision source image_os architecture
  metadata="$(image_metadata "$reference")"
  revision="$(jq -r '.config.Labels["org.opencontainers.image.revision"] // ""' <<<"$metadata")"
  source="$(jq -r '.config.Labels["org.opencontainers.image.source"] // ""' <<<"$metadata")"
  image_os="$(jq -r '.os // ""' <<<"$metadata")"
  architecture="$(jq -r '.architecture // ""' <<<"$metadata")"
  [[ "$revision" == "$full_sha" ]] || fail "$reference has unexpected OCI revision: $revision"
  [[ "$source" == "$SOURCE_URL" ]] || fail "$reference has unexpected OCI source: $source"
  [[ "$image_os/$architecture" == "linux/amd64" ]] \
    || fail "$reference has unexpected platform: $image_os/$architecture"
}

select_builder_args() {
  if [[ -n "${BUILDX_BUILDER:-}" ]]; then
    printf '%s\n' "--builder" "$BUILDX_BUILDER"
    return
  fi
  local candidate
  for candidate in orbstack default; do
    if docker buildx inspect "$candidate" >/dev/null 2>&1; then
      printf '%s\n' "--builder" "$candidate"
      return
    fi
  done
}

railway_status_json() {
  railway status \
    --project "$RAILWAY_PROJECT_ID" \
    --environment "$RAILWAY_ENVIRONMENT" \
    --json
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

active_deployment_json() {
  railway_service_node "$1" | jq '
    .activeDeployments
    | map(select(.status == "SUCCESS"))
    | first
  '
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
      map(select(.status == "SUCCESS" and .meta.imageDigest == $digest))
      | first
    '
}

wait_for_deployment() {
  local service="$1"
  local previous_id="$2"
  local expected_digest="$3"
  local deadline=$((SECONDS + DEPLOY_TIMEOUT_SECONDS))
  local deployment deployment_id deployment_status deployment_digest

  while (( SECONDS < deadline )); do
    deployment="$(latest_deployment_json "$service")"
    deployment_id="$(jq -r '.id // ""' <<<"$deployment")"
    deployment_status="$(jq -r '.status // ""' <<<"$deployment")"
    deployment_digest="$(jq -r '.meta.imageDigest // ""' <<<"$deployment")"
    if [[ -n "$deployment_id" && "$deployment_id" != "$previous_id" ]]; then
      case "$deployment_status" in
        SUCCESS)
          [[ "$deployment_digest" == "$expected_digest" ]] \
            || fail "$service deployed $deployment_digest, expected $expected_digest"
          printf '%s\n' "$deployment_id"
          return 0
          ;;
        FAILED|CRASHED|REMOVED)
          fail "$service deployment $deployment_id ended with $deployment_status"
          ;;
      esac
    fi
    sleep 5
  done
  fail "timed out waiting for Railway service: $service"
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
  jq -n \
    --arg status "$release_status" \
    --arg git_sha "$full_sha" \
    --arg image_repository "$IMAGE_REPO" \
    --arg sha_tag "$sha_ref" \
    --arg latest_tag "$latest_ref" \
    --arg digest "${expected_digest:-}" \
    --arg previous_digest "${previous_digest:-}" \
    --arg rollback_tag "$rollback_ref" \
    --arg previous_api_deployment_id "${previous_api_deployment_id:-}" \
    --arg previous_worker_deployment_id "${previous_worker_deployment_id:-}" \
    --arg previous_scheduler_deployment_id "${previous_scheduler_deployment_id:-}" \
    --arg api_deployment_id "${api_deployment_id:-}" \
    --arg worker_deployment_id "${worker_deployment_id:-}" \
    --arg scheduler_deployment_id "${scheduler_deployment_id:-}" \
    --arg updated_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{
      status: $status,
      git_sha: $git_sha,
      image_repository: $image_repository,
      sha_tag: $sha_tag,
      latest_tag: $latest_tag,
      digest: $digest,
      previous_digest: $previous_digest,
      rollback_tag: $rollback_tag,
      previous_railway: {
        api_deployment_id: $previous_api_deployment_id,
        worker_deployment_id: $previous_worker_deployment_id,
        scheduler_deployment_id: $previous_scheduler_deployment_id
      },
      railway: {
        api_deployment_id: $api_deployment_id,
        worker_deployment_id: $worker_deployment_id,
        scheduler_deployment_id: $scheduler_deployment_id
      },
      updated_at: $updated_at
    }' >"$manifest_path"
}

log "release commit: $full_sha"
wait_for_ci
revalidate_dev_head
validate_railway_target

for service in "${RAILWAY_SERVICES[@]}"; do
  source_image="$(railway_source_image "$service")"
  if [[ "$source_image" != "$latest_ref" && "$source_image" != "docker.io/${latest_ref}" ]]; then
    fail "Railway $service source must be $latest_ref, got: ${source_image:-none}"
  fi
done

sha_image_state="$(image_state "$sha_ref")"
if [[ "$sha_image_state" == "exists" ]]; then
  verify_sha_image "$sha_ref"
  log "verified existing immutable SHA image: $sha_ref"
else
  revalidate_dev_head
  mapfile -t builder_args < <(select_builder_args)
  build_date="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  log "building and pushing $sha_ref for linux/amd64"
  docker buildx build \
    "${builder_args[@]}" \
    --platform linux/amd64 \
    --provenance=false \
    --build-arg "RELEASE_SHA=$full_sha" \
    --build-arg "BUILD_DATE=$build_date" \
    --build-arg "SOURCE_URL=$SOURCE_URL" \
    --tag "$sha_ref" \
    --push \
    .
  verify_sha_image "$sha_ref"
fi

expected_digest="$(image_digest "$sha_ref")"
rollback_image_state="$(image_state "$rollback_ref")"
rollback_digest=""
if [[ "$rollback_image_state" == "exists" ]]; then
  rollback_digest="$(image_digest "$rollback_ref")"
fi
declare -A active_ids=()
declare -A active_digests=()
for service in "${RAILWAY_SERVICES[@]}"; do
  active="$(active_deployment_json "$service")"
  active_ids[$service]="$(jq -r '.id // ""' <<<"$active")"
  active_digests[$service]="$(jq -r '.meta.imageDigest // ""' <<<"$active")"
  [[ -n "${active_ids[$service]}" && "${active_digests[$service]}" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fail "Railway $service has no active successful deployment metadata"
done

if [[ -n "$rollback_digest" ]]; then
  previous_digest="$rollback_digest"
  [[ "$previous_digest" != "$expected_digest" ]] \
    || fail "rollback tag unexpectedly points to the target release"
else
  previous_digest="${active_digests[api]}"
  for service in worker scheduler; do
    [[ "${active_digests[$service]}" == "$previous_digest" ]] \
      || fail "mixed Railway digests require an existing immutable rollback tag"
  done
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

for service in "${RAILWAY_SERVICES[@]}"; do
  current_digest="${active_digests[$service]}"
  if [[ "$current_digest" != "$previous_digest" && "$current_digest" != "$expected_digest" ]]; then
    fail "$service is on unrelated digest $current_digest"
  fi
done

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
write_manifest "prepared"

initial_latest_digest="$(image_digest "$latest_ref")"
if [[ "$initial_latest_digest" != "$previous_digest" && "$initial_latest_digest" != "$expected_digest" ]]; then
  fail "Docker Hub latest changed to unrelated digest $initial_latest_digest"
fi
if [[ "$initial_latest_digest" != "$expected_digest" ]]; then
  revalidate_dev_head
  [[ "$(image_digest "$latest_ref")" == "$initial_latest_digest" ]] \
    || fail "Docker Hub latest changed concurrently before promotion"
  log "promoting the exact SHA image to $latest_ref"
  docker buildx imagetools create --prefer-index=false \
    --tag "$latest_ref" "$sha_ref" >/dev/null
fi
latest_digest="$(image_digest "$latest_ref")"
[[ "$latest_digest" == "$expected_digest" ]] \
  || fail "latest digest $latest_digest does not match SHA digest $expected_digest"
write_manifest "deploying"

deploy_role() {
  local service="$1"
  local active active_id active_digest before_id deployment_id
  active="$(active_deployment_json "$service")"
  active_id="$(jq -r '.id // ""' <<<"$active")"
  active_digest="$(jq -r '.meta.imageDigest // ""' <<<"$active")"
  if [[ "$active_digest" == "$expected_digest" ]]; then
    log "$service already runs the target digest: $active_id"
    printf '%s\n' "$active_id"
    return 0
  fi
  [[ "$active_digest" == "$previous_digest" ]] \
    || fail "$service cannot resume from digest $active_digest"
  before_id="$(jq -r '.id // ""' <<<"$(latest_deployment_json "$service")")"
  log "redeploying Railway $service from source"
  railway redeploy \
    --project "$RAILWAY_PROJECT_ID" \
    --environment "$RAILWAY_ENVIRONMENT" \
    --service "$service" \
    --from-source \
    --yes \
    --json >/dev/null
  deployment_id="$(wait_for_deployment "$service" "$before_id" "$expected_digest")"
  printf '%s\n' "$deployment_id"
}

api_deployment_id="$(deploy_role api)"
wait_for_api_health
log "API ready: $api_deployment_id"
worker_deployment_id="$(deploy_role worker)"
log "Worker ready: $worker_deployment_id"
scheduler_deployment_id="$(deploy_role scheduler)"
log "Scheduler ready: $scheduler_deployment_id"

final_latest_digest="$(image_digest "$latest_ref")"
[[ "$final_latest_digest" == "$expected_digest" ]] \
  || fail "Docker Hub latest changed during Railway rollout"
for service in "${RAILWAY_SERVICES[@]}"; do
  active="$(active_deployment_json "$service")"
  final_id="$(jq -r '.id // ""' <<<"$active")"
  final_digest="$(jq -r '.meta.imageDigest // ""' <<<"$active")"
  [[ -n "$final_id" && "$final_digest" == "$expected_digest" ]] \
    || fail "$service active deployment does not match $expected_digest"
done
revalidate_dev_head
write_manifest "completed"

log "release complete: $full_sha -> $expected_digest"
log "release manifest: $manifest_path"
