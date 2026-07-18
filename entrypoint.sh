#!/bin/sh
# 单镜像多角色分发：三类进程共用同一镜像，由 SERVICE_ROLE 决定启动命令。
# Railway 对镜像服务不透传 start command，故用可经 CLI 设置的环境变量分发（KISS）。
set -e

ROLE="${SERVICE_ROLE:-api}"
PORT="${PORT:-8000}"

case "$ROLE" in
  api)
    echo "[entrypoint] preparing database and encrypted secrets..."
    python scripts/prepare_database.py
    echo "[entrypoint] starting API on port ${PORT}..."
    exec uvicorn apps.api.main:app --host 0.0.0.0 --port "${PORT}"
    ;;
  worker)
    echo "[entrypoint] verifying database readiness..."
    python scripts/assert_database_ready.py
    echo "[entrypoint] starting dramatiq worker..."
    exec dramatiq apps.worker.main
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
