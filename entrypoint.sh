#!/bin/sh
# 单镜像多角色分发：三类进程共用同一镜像，由 SERVICE_ROLE 决定启动命令。
# Railway 对镜像服务不透传 start command，故用可经 CLI 设置的环境变量分发（KISS）。
set -e

ROLE="${SERVICE_ROLE:-api}"
PORT="${PORT:-8000}"

validate_worker_integer() {
  name="$1"
  value="$2"
  minimum="$3"
  maximum="$4"
  case "$value" in
    ''|0[0-9]*|*[!0-9]*|??????????*)
      echo "[entrypoint] $name must be an integer between $minimum and $maximum" >&2
      exit 1
      ;;
  esac
  if [ "$value" -lt "$minimum" ] || [ "$value" -gt "$maximum" ]; then
    echo "[entrypoint] $name must be an integer between $minimum and $maximum" >&2
    exit 1
  fi
}

case "$ROLE" in
  api)
    echo "[entrypoint] preparing database and encrypted secrets..."
    python scripts/prepare_database.py
    echo "[entrypoint] starting API on port ${PORT}..."
    exec uvicorn apps.api.main:app --host 0.0.0.0 --port "${PORT}"
    ;;
  worker)
    DRAMATIQ_PROCESSES="${DRAMATIQ_PROCESSES:-4}"
    DRAMATIQ_THREADS="${DRAMATIQ_THREADS:-8}"
    DRAMATIQ_WORKER_TIMEOUT_MS="${DRAMATIQ_WORKER_TIMEOUT_MS:-250}"
    validate_worker_integer DRAMATIQ_PROCESSES "$DRAMATIQ_PROCESSES" 1 32
    validate_worker_integer DRAMATIQ_THREADS "$DRAMATIQ_THREADS" 1 32
    validate_worker_integer DRAMATIQ_WORKER_TIMEOUT_MS "$DRAMATIQ_WORKER_TIMEOUT_MS" 50 5000
    if [ $((DRAMATIQ_PROCESSES * DRAMATIQ_THREADS)) -gt 128 ]; then
      echo "[entrypoint] DRAMATIQ_PROCESSES * DRAMATIQ_THREADS must not exceed 128" >&2
      exit 1
    fi
    export dramatiq_worker_timeout="$DRAMATIQ_WORKER_TIMEOUT_MS"
    echo "[entrypoint] verifying database readiness..."
    python scripts/assert_database_ready.py
    echo "[entrypoint] starting dramatiq worker (${DRAMATIQ_PROCESSES} processes, ${DRAMATIQ_THREADS} threads/process, ${DRAMATIQ_WORKER_TIMEOUT_MS}ms idle timeout)..."
    exec dramatiq --processes "$DRAMATIQ_PROCESSES" --threads "$DRAMATIQ_THREADS" apps.worker.main
    ;;
  scheduler)
    echo "[entrypoint] verifying database readiness..."
    python scripts/assert_database_ready.py
    echo "[entrypoint] starting recovery scheduler..."
    exec python -m apps.scheduler.main
    ;;
  *)
    echo "[entrypoint] unknown SERVICE_ROLE='$ROLE' (expected api|worker|scheduler)" >&2
    exit 1
    ;;
esac
