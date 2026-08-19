#!/usr/bin/env bash
set -Eeuo pipefail

readonly IMAGE_REPO="zhiyangxiaozi/reply-core"
readonly RAILWAY_PROJECT_ID="abcf3199-e5ac-415b-a22e-062206390331"
readonly RAILWAY_PROJECT_NAME="reply-core"
readonly RAILWAY_ENVIRONMENT="production"
readonly RAILWAY_REGION="us-east4-eqdc4a"
readonly PUBLIC_BASE_URL="https://relay.nexory.top"
readonly SOURCE_URL="https://github.com/junqingyongyuanbusi/dm"
readonly DEPLOY_TIMEOUT_SECONDS="${DEPLOY_TIMEOUT_SECONDS:-900}"
readonly CI_TIMEOUT_SECONDS="${CI_TIMEOUT_SECONDS:-1200}"
readonly RAILWAY_SERVICES=(api worker scheduler)
readonly RAILWAY_COLOCATED_SERVICES=(api worker scheduler Postgres Redis)

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

image_revision() {
  jq -r '.config.Labels["org.opencontainers.image.revision"] // ""' \
    <<<"$(image_metadata "$1")"
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
  local metadata revision source image_os architecture purpose labeled_base labeled_target database_head
  metadata="$(image_metadata "$reference")"
  revision="$(jq -r '.config.Labels["org.opencontainers.image.revision"] // ""' <<<"$metadata")"
  source="$(jq -r '.config.Labels["org.opencontainers.image.source"] // ""' <<<"$metadata")"
  purpose="$(jq -r '.config.Labels["com.nexory.reply-core.rollback-purpose"] // ""' <<<"$metadata")"
  labeled_base="$(jq -r '.config.Labels["com.nexory.reply-core.rollback-base-digest"] // ""' <<<"$metadata")"
  labeled_target="$(jq -r '.config.Labels["com.nexory.reply-core.rollback-target-release"] // ""' <<<"$metadata")"
  database_head="$(jq -r '.config.Labels["com.nexory.reply-core.database-head"] // ""' <<<"$metadata")"
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
  [[ "$database_head" == "a6f1c3d8e205" ]] \
    || fail "$reference has unexpected database head: $database_head"
  [[ "$image_os/$architecture" == "linux/amd64" ]] \
    || fail "$reference has unexpected platform: $image_os/$architecture"
}

select_builder() {
  if [[ -n "${BUILDX_BUILDER:-}" ]]; then
    printf '%s\n' "$BUILDX_BUILDER"
    return
  fi
  local candidate
  for candidate in orbstack default; do
    if docker buildx inspect "$candidate" >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return
    fi
  done
}

prepare_rollback_compatible_image() {
  local reference="$1"
  local base_digest="$2"
  local app_revision="$3"
  local state builder_name build_date digest
  local -a builder_args
  state="$(image_state "$reference")"
  if [[ "$state" == "exists" ]]; then
    verify_rollback_compatible_image "$reference" "$base_digest" "$app_revision"
  else
    revalidate_dev_head
    builder_name="$(select_builder)"
    builder_args=()
    if [[ -n "$builder_name" ]]; then
      builder_args=(--builder "$builder_name")
    fi
    build_date="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    log "building and pushing migration-compatible rollback image: $reference"
    docker buildx build \
      "${builder_args[@]}" \
      --file deploy/Dockerfile.migration-compatible-rollback \
      --platform linux/amd64 \
      --provenance=false \
      --build-arg "BASE_IMAGE=${IMAGE_REPO}@${base_digest}" \
      --build-arg "APP_REVISION=${app_revision}" \
      --build-arg "TARGET_RELEASE_SHA=${full_sha}" \
      --build-arg "BUILD_DATE=${build_date}" \
      --build-arg "SOURCE_URL=${SOURCE_URL}" \
      --build-arg "BASE_DIGEST=${base_digest}" \
      --tag "$reference" \
      --push \
      . >&2
    verify_rollback_compatible_image "$reference" "$base_digest" "$app_revision"
  fi
  digest="$(image_digest "$reference")"
  [[ "$digest" != "$expected_digest" ]] \
    || fail "rollback-compatible image unexpectedly matches target digest"
  printf '%s\n' "$digest"
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
  local manifest_tmp
  manifest_tmp="$(mktemp "${manifest_path}.tmp.XXXXXX")"
  jq -n \
    --arg status "$release_status" \
    --arg git_sha "$full_sha" \
    --arg image_repository "$IMAGE_REPO" \
    --arg sha_tag "$sha_ref" \
    --arg latest_tag "$latest_ref" \
    --arg digest "${expected_digest:-}" \
    --arg previous_digest "${previous_digest:-}" \
    --arg rollback_tag "$rollback_ref" \
    --arg previous_app_revision "${previous_app_revision:-}" \
    --arg rollback_compatible_tag "$rollback_compatible_ref" \
    --arg rollback_compatible_digest "${rollback_compatible_digest:-}" \
    --arg rollback_schema_head "a6f1c3d8e205" \
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
      migration_compatible_rollback: {
        tag: $rollback_compatible_tag,
        digest: $rollback_compatible_digest,
        predecessor_app_revision: $previous_app_revision,
        database_head: $rollback_schema_head
      },
      release_gates: {
        multilingual_live_enabled: false,
        multilingual_shadow_enabled: false,
        english_knowledge_only_enabled: false
      },
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
  builder_name="$(select_builder)"
  builder_args=()
  if [[ -n "$builder_name" ]]; then
    builder_args=(--builder "$builder_name")
  fi
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

for service in "${RAILWAY_SERVICES[@]}"; do
  case "$service" in
    api) current_digest="$active_api_digest" ;;
    worker) current_digest="$active_worker_digest" ;;
    scheduler) current_digest="$active_scheduler_digest" ;;
  esac
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

verify_predecessor_image "${IMAGE_REPO}@${previous_digest}" "$previous_digest"
previous_app_revision="$(image_revision "${IMAGE_REPO}@${previous_digest}")"
[[ "$previous_app_revision" =~ ^[0-9a-f]{40}$ ]] \
  || fail "predecessor image has invalid OCI revision: ${previous_app_revision:-missing}"
rollback_compatible_digest="$(prepare_rollback_compatible_image \
  "$rollback_compatible_ref" "$previous_digest" "$previous_app_revision")"
[[ "$rollback_compatible_digest" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || fail "invalid migration-compatible rollback digest: $rollback_compatible_digest"
log "verifying target and migration-compatible predecessor against an isolated a6 database"
scripts/verify_migration_compatible_rollback.sh \
  "${IMAGE_REPO}@${expected_digest}" \
  "${IMAGE_REPO}@${rollback_compatible_digest}"
[[ "$(image_digest "$rollback_compatible_ref")" == "$rollback_compatible_digest" ]] \
  || fail "migration-compatible rollback tag changed during smoke test"
verify_rollback_compatible_image \
  "${IMAGE_REPO}@${rollback_compatible_digest}" "$previous_digest" "$previous_app_revision"
write_manifest "prepared"

initial_latest_digest="$(image_digest "$latest_ref")"
if [[ "$initial_latest_digest" != "$previous_digest" && "$initial_latest_digest" != "$expected_digest" ]]; then
  fail "Docker Hub latest changed to unrelated digest $initial_latest_digest"
fi
write_manifest "deploying"
if [[ "$initial_latest_digest" != "$expected_digest" ]]; then
  revalidate_dev_head
  [[ "$(image_digest "$latest_ref")" == "$initial_latest_digest" ]] \
    || fail "Docker Hub latest changed concurrently before promotion"
  verify_sha_image "$sha_ref"
  [[ "$(image_digest "$sha_ref")" == "$expected_digest" ]] \
    || fail "target SHA tag changed before promotion"
  log "promoting the exact target digest to $latest_ref"
  docker buildx imagetools create --prefer-index=false \
    --tag "$latest_ref" "${IMAGE_REPO}@${expected_digest}" >/dev/null
fi
latest_digest="$(image_digest "$latest_ref")"
[[ "$latest_digest" == "$expected_digest" ]] \
  || fail "latest digest $latest_digest does not match SHA digest $expected_digest"
require_target_latest() {
  local latest_digest
  latest_digest="$(image_digest "$latest_ref")"
  [[ "$latest_digest" == "$expected_digest" ]] \
    || fail "Docker Hub latest changed during release: $latest_digest"
}

deploy_role() {
  local service="$1"
  local active active_id active_digest before_id deployment_id
  require_target_latest
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
  require_target_latest
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
require_target_latest

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
validate_railway_config
validate_railway_colocation
revalidate_dev_head
require_target_latest
write_manifest "completed"

log "release complete: $full_sha -> $expected_digest"
log "migration-compatible rollback: $rollback_compatible_ref -> $rollback_compatible_digest"
log "release manifest: $manifest_path"
