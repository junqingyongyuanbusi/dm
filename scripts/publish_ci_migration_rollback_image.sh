#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 5 ]]; then
  printf 'usage: %s <image-repository> <target-sha> <local-target-image> <source-url> <evidence-path>\n' "$0" >&2
  exit 2
fi

image_repository="$1"
target_sha="$2"
local_target_image="$3"
source_url="$4"
evidence_path="$5"

fail() {
  printf '[ci-rollback-image] ERROR: %s\n' "$*" >&2
  exit 1
}

log() {
  printf '[ci-rollback-image] %s\n' "$*" >&2
}

for command_name in docker git jq awk date mkdir; do
  command -v "$command_name" >/dev/null 2>&1 \
    || fail "required command not found: $command_name"
done

[[ "$target_sha" =~ ^[0-9a-f]{40}$ ]] || fail "invalid target SHA: $target_sha"
[[ "$image_repository" == ghcr.io/* ]] \
  || fail "image repository must be hosted on GHCR"
git cat-file -e "${target_sha}^{commit}" 2>/dev/null \
  || fail "target commit is not available in the checkout: $target_sha"

short_sha="${target_sha:0:12}"
target_ref="${image_repository}:${target_sha}"
latest_ref="${image_repository}:latest"
compatibility_ref="${image_repository}:railway-compat-pre-${short_sha}"

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
  if [[ "$output" =~ (not[[:space:]]found|manifest[[:space:]]unknown|name[[:space:]]unknown) ]]; then
    printf '%s\n' "absent"
    return 0
  fi
  fail "could not inspect $reference: $output"
}

image_digest() {
  local reference="$1"
  local output digest
  output="$(docker buildx imagetools inspect "$reference")" \
    || fail "could not inspect digest for $reference"
  digest="$(awk '/^Digest:/ {print $2; exit}' <<<"$output")"
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fail "invalid digest for $reference: ${digest:-missing}"
  printf '%s\n' "$digest"
}

image_metadata() {
  docker buildx imagetools inspect "$1" --format '{{json .Image}}'
}

write_evidence() {
  local required="$1"
  local compatibility_digest="$2"
  local predecessor_digest="$3"
  local predecessor_revision="$4"
  local database_head="$5"
  mkdir -p "$(dirname "$evidence_path")"
  jq -n \
    --argjson required "$required" \
    --arg tag "$compatibility_ref" \
    --arg digest "$compatibility_digest" \
    --arg predecessor_digest "$predecessor_digest" \
    --arg predecessor_app_revision "$predecessor_revision" \
    --arg database_head "$database_head" \
    '{
      required: $required,
      tag: (if $required then $tag else "" end),
      digest: $digest,
      predecessor_digest: $predecessor_digest,
      predecessor_app_revision: $predecessor_app_revision,
      database_head: $database_head,
      verification: (if $required then "ci-isolated-database" else "not-required" end)
    }' >"$evidence_path"
}

verify_compatibility_image() {
  local reference="$1"
  local expected_base_digest="$2"
  local expected_app_revision="$3"
  local expected_database_head="$4"
  local expected_capability="$5"
  local metadata revision labeled_source purpose labeled_base labeled_target labeled_head
  local capability image_os architecture
  metadata="$(image_metadata "$reference")"
  revision="$(jq -r '.config.Labels["org.opencontainers.image.revision"] // ""' <<<"$metadata")"
  labeled_source="$(jq -r '.config.Labels["org.opencontainers.image.source"] // ""' <<<"$metadata")"
  purpose="$(jq -r '.config.Labels["com.nexory.reply-core.rollback-purpose"] // ""' <<<"$metadata")"
  labeled_base="$(jq -r '.config.Labels["com.nexory.reply-core.rollback-base-digest"] // ""' <<<"$metadata")"
  labeled_target="$(jq -r '.config.Labels["com.nexory.reply-core.rollback-target-release"] // ""' <<<"$metadata")"
  labeled_head="$(jq -r '.config.Labels["com.nexory.reply-core.database-head"] // ""' <<<"$metadata")"
  capability="$(jq -r '.config.Labels["com.nexory.reply-core.review-outbox-contract"] // ""' <<<"$metadata")"
  image_os="$(jq -r '.os // ""' <<<"$metadata")"
  architecture="$(jq -r '.architecture // ""' <<<"$metadata")"
  [[ "$revision" == "$expected_app_revision" ]] \
    || fail "$reference has unexpected predecessor revision: $revision"
  [[ "$labeled_source" == "$source_url" ]] \
    || fail "$reference has unexpected OCI source: $labeled_source"
  [[ "$purpose" == "migration-compatible-predecessor" ]] \
    || fail "$reference has unexpected rollback purpose: $purpose"
  [[ "$labeled_base" == "$expected_base_digest" ]] \
    || fail "$reference has unexpected predecessor digest: $labeled_base"
  [[ "$labeled_target" == "$target_sha" ]] \
    || fail "$reference has unexpected target release: $labeled_target"
  [[ "$labeled_head" == "$expected_database_head" ]] \
    || fail "$reference has unexpected database head: $labeled_head"
  [[ "$capability" == "$expected_capability" ]] \
    || fail "$reference has unexpected review Outbox capability: $capability"
  [[ "$image_os/$architecture" == "linux/amd64" ]] \
    || fail "$reference has unexpected platform: $image_os/$architecture"
}

target_digest="$(image_digest "$target_ref")"
latest_digest="$(image_digest "$latest_ref")"

# A rerun after rollout can no longer infer the predecessor from latest. The immutable
# compatibility tag is sufficient evidence because the first successful CI run created it before
# latest was allowed to move.
if [[ "$latest_digest" == "$target_digest" ]]; then
  if [[ "$(image_state "$compatibility_ref")" == "absent" ]]; then
    log "latest already points to target and no compatibility image exists; treating this as a non-migration release"
    write_evidence false "" "$target_digest" "$target_sha" ""
    exit 0
  fi
  compatibility_metadata="$(image_metadata "$compatibility_ref")"
  predecessor_digest="$(jq -r '.config.Labels["com.nexory.reply-core.rollback-base-digest"] // ""' <<<"$compatibility_metadata")"
  predecessor_revision="$(jq -r '.config.Labels["org.opencontainers.image.revision"] // ""' <<<"$compatibility_metadata")"
  database_head="$(jq -r '.config.Labels["com.nexory.reply-core.database-head"] // ""' <<<"$compatibility_metadata")"
  predecessor_capability="$(jq -r '.config.Labels["com.nexory.reply-core.review-outbox-contract"] // ""' <<<"$compatibility_metadata")"
  verify_compatibility_image \
    "$compatibility_ref" "$predecessor_digest" "$predecessor_revision" \
    "$database_head" "$predecessor_capability"
  compatibility_digest="$(image_digest "$compatibility_ref")"
  scripts/verify_migration_compatible_rollback.sh \
    "${image_repository}@${target_digest}" \
    "${image_repository}@${compatibility_digest}" \
    "$database_head"
  write_evidence true "$compatibility_digest" "$predecessor_digest" \
    "$predecessor_revision" "$database_head"
  exit 0
fi

predecessor_digest="$latest_digest"
predecessor_metadata="$(image_metadata "${image_repository}@${predecessor_digest}")"
predecessor_revision="$(jq -r '.config.Labels["org.opencontainers.image.revision"] // ""' <<<"$predecessor_metadata")"
predecessor_capability="$(jq -r '.config.Labels["com.nexory.reply-core.review-outbox-contract"] // "legacy"' <<<"$predecessor_metadata")"
[[ "$predecessor_revision" =~ ^[0-9a-f]{40}$ ]] \
  || fail "predecessor image has invalid app revision: ${predecessor_revision:-missing}"
git cat-file -e "${predecessor_revision}^{commit}" 2>/dev/null \
  || fail "predecessor commit is not available in full checkout: $predecessor_revision"

set +e
git diff --quiet "$predecessor_revision" "$target_sha" -- migrations/versions
migration_diff_status=$?
set -e
case "$migration_diff_status" in
  0)
    log "no migration graph change; compatibility image is not required"
    write_evidence false "" "$predecessor_digest" "$predecessor_revision" ""
    exit 0
    ;;
  1) ;;
  *) fail "could not compare migration graph with predecessor $predecessor_revision" ;;
esac

mapfile -t database_heads < <(
  docker run --rm --entrypoint alembic "$local_target_image" heads \
    | awk 'NF {print $1}'
)
[[ ${#database_heads[@]} -eq 1 ]] \
  || fail "target image must expose exactly one Alembic head"
database_head="${database_heads[0]}"
[[ "$database_head" =~ ^[0-9a-f]{12,64}$ ]] \
  || fail "invalid target database head: $database_head"

if [[ "$(image_state "$compatibility_ref")" == "exists" ]]; then
  log "verifying existing immutable compatibility image: $compatibility_ref"
else
  build_date="$(git show -s --format=%cI "$target_sha")"
  log "building migration-compatible predecessor in GitHub Actions: $compatibility_ref"
  docker buildx build \
    --file deploy/Dockerfile.migration-compatible-rollback \
    --platform linux/amd64 \
    --provenance=false \
    --build-arg "BASE_IMAGE=${image_repository}@${predecessor_digest}" \
    --build-arg "APP_REVISION=${predecessor_revision}" \
    --build-arg "TARGET_RELEASE_SHA=${target_sha}" \
    --build-arg "BUILD_DATE=${build_date}" \
    --build-arg "SOURCE_URL=${source_url}" \
    --build-arg "BASE_DIGEST=${predecessor_digest}" \
    --build-arg "DATABASE_HEAD=${database_head}" \
    --build-arg "BASE_REVIEW_OUTBOX_CAPABILITY=${predecessor_capability}" \
    --tag "$compatibility_ref" \
    --push \
    .
fi

verify_compatibility_image \
  "$compatibility_ref" "$predecessor_digest" "$predecessor_revision" \
  "$database_head" "$predecessor_capability"
compatibility_digest="$(image_digest "$compatibility_ref")"
[[ "$compatibility_digest" != "$target_digest" ]] \
  || fail "compatibility image unexpectedly matches target digest"

scripts/verify_migration_compatible_rollback.sh \
  "${image_repository}@${target_digest}" \
  "${image_repository}@${compatibility_digest}" \
  "$database_head"
write_evidence true "$compatibility_digest" "$predecessor_digest" \
  "$predecessor_revision" "$database_head"
log "published and verified $compatibility_ref at $compatibility_digest"
