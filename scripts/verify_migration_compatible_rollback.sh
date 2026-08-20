#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 2 ]]; then
  printf 'usage: %s <target-image> <migration-compatible-rollback-image>\n' "$0" >&2
  exit 2
fi

target_image="$1"
compat_image="$2"
expected_head="b7d2e4f6a901"
postgres_image="pgvector/pgvector@sha256:d2ef61f42ef767baa5a1475393303cc235bcd92febd9d7014eddb48b41f3bad0"
run_id="${RANDOM}-$$-$(date +%s)"
network="reply-core-rollback-${run_id}"
postgres="reply-core-rollback-postgres-${run_id}"

cleanup() {
  docker rm -f "$postgres" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create "$network" >/dev/null
docker run -d \
  --platform linux/amd64 \
  --name "$postgres" \
  --network "$network" \
  --network-alias postgres \
  -e POSTGRES_USER=dev \
  -e POSTGRES_PASSWORD=dev \
  -e POSTGRES_DB=social_reply_test \
  "$postgres_image" >/dev/null

for _ in $(seq 1 60); do
  if docker exec "$postgres" pg_isready -U dev -d social_reply_test >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
if ! docker exec "$postgres" pg_isready -U dev -d social_reply_test >/dev/null 2>&1; then
  echo "rollback verification PostgreSQL did not become ready" >&2
  exit 1
fi

common_env=(
  -e TESTING=true
  -e DATABASE_URL=postgresql+asyncpg://dev:dev@postgres:5432/social_reply_test
  -e REDIS_URL=redis://localhost:6379/0
  -e PLATFORM_SECRET_KEYS=Wm5wbamjBFvTmkGIU2NskIKCrJfsb4AdUBDZR-m1-CM=
  -e ADMIN_SESSION_SECRET=test-admin-session-secret-at-least-32-bytes
  -e ADMIN_USERNAME=admin
  -e ADMIN_PASSWORD=test-admin-password
  -e PUBLIC_BASE_URL=https://reply.example.com
  -e CHATWOOT_WEBHOOK_SECRET=change-me
  -e CONTROL_API_KEY=test-control-key
  -e LLM_PROVIDER=stub
)

run_python_module() {
  local image="$1"
  local module="$2"
  docker run --rm \
    --platform linux/amd64 \
    --network "$network" \
    "${common_env[@]}" \
    --entrypoint python \
    "$image" \
    -m "$module"
}

run_alembic() {
  local image="$1"
  shift
  docker run --rm \
    --platform linux/amd64 \
    --network "$network" \
    "${common_env[@]}" \
    --entrypoint alembic \
    "$image" \
    "$@"
}

run_python_module "$target_image" scripts.prepare_database

target_current="$(run_alembic "$target_image" current | awk '{print $1; exit}')"
compat_head="$(run_alembic "$compat_image" heads | awk '{print $1; exit}')"
compat_current="$(run_alembic "$compat_image" current | awk '{print $1; exit}')"
[[ "$target_current" == "$expected_head" ]] || {
  echo "target image prepared unexpected DB revision: $target_current" >&2
  exit 1
}
[[ "$compat_head" == "$expected_head" ]] || {
  echo "compat image has unexpected Alembic head: $compat_head" >&2
  exit 1
}
[[ "$compat_current" == "$expected_head" ]] || {
  echo "compat image sees unexpected DB revision: $compat_current" >&2
  exit 1
}

run_python_module "$compat_image" scripts.prepare_database
run_python_module "$compat_image" scripts.assert_database_ready

compat_current_after="$(run_alembic "$compat_image" current | awk '{print $1; exit}')"
[[ "$compat_current_after" == "$expected_head" ]] || {
  echo "compat image changed DB revision unexpectedly: $compat_current_after" >&2
  exit 1
}

printf 'migration-compatible rollback verified: target=%s compat=%s head=%s\n' \
  "$target_image" "$compat_image" "$expected_head"
