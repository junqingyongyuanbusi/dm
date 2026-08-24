#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <image-reference> <expected-full-sha>" >&2
  exit 2
fi

image="$1"
expected_sha="$2"
[[ "$expected_sha" =~ ^[0-9a-f]{40}$ ]] || {
  echo "expected SHA must be a full lowercase Git SHA" >&2
  exit 2
}

[[ "$(docker image inspect "$image" --format '{{.Config.User}}')" == "appuser" ]]
[[ "$(docker run --rm --entrypoint id "$image" -u)" == "10001" ]]
[[ "$(docker run --rm --entrypoint id "$image" -g)" == "10001" ]]
[[ "$(docker image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" == "$expected_sha" ]]
[[ "$(docker image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.source"}}')" == "https://github.com/junqingyongyuanbusi/dm" ]]
[[ "$(docker image inspect "$image" --format '{{.Architecture}}')" == "amd64" ]]
docker run --rm --entrypoint sh "$image" -c 'test ! -w /app/src && test ! -w /app/.venv'
docker run --rm --entrypoint sh "$image" -c \
  'test ! -e /app/tests && test ! -e /app/docs && test ! -e /app/scripts/publish_railway_release.sh'
docker run --rm \
  -e TESTING=true \
  -e PLATFORM_SECRET_KEYS=Wm5wbamjBFvTmkGIU2NskIKCrJfsb4AdUBDZR-m1-CM= \
  --entrypoint python \
  "$image" \
  -c 'import apps.api.main, apps.worker.main, apps.scheduler.main, scripts.prepare_database; print("runtime imports ok")'
docker run --rm \
  -e TESTING=true \
  -e PLATFORM_SECRET_KEYS=Wm5wbamjBFvTmkGIU2NskIKCrJfsb4AdUBDZR-m1-CM= \
  --entrypoint alembic \
  "$image" \
  heads
if docker run --rm "$image" >/tmp/reply-core-missing-role.log 2>&1; then
  cat /tmp/reply-core-missing-role.log
  exit 1
fi
grep -q "SERVICE_ROLE is required" /tmp/reply-core-missing-role.log
if docker run --rm -e SERVICE_ROLE=invalid "$image" >/tmp/reply-core-invalid-role.log 2>&1; then
  cat /tmp/reply-core-invalid-role.log
  exit 1
fi
grep -q "unknown SERVICE_ROLE" /tmp/reply-core-invalid-role.log
