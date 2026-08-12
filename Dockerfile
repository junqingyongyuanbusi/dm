# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
# Reply Core 运行镜像：单镜像承载 API / worker / scheduler 三类进程，
# 由部署平台（Railway）为每个服务覆盖启动命令。KISS：不为每个进程单独出镜像。

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca AS base

ARG RELEASE_SHA=unknown
ARG BUILD_DATE=unknown
ARG SOURCE_URL=https://github.com/junqingyongyuanbusi/dm
LABEL org.opencontainers.image.revision="${RELEASE_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="${SOURCE_URL}"

# uv 行为：不做符号链接（容器内无缓存卷）、字节码预编译加速冷启动
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RELEASE_SHA="${RELEASE_SHA}"

WORKDIR /app

# --- 依赖层：仅随 lock 文件变化失效，最大化构建缓存命中 ---
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# --- 项目层：只复制运行期所需内容，避免把测试、文档和运维脚本带入镜像 ---
COPY src ./src
COPY apps ./apps
COPY migrations ./migrations
COPY alembic.ini entrypoint.sh ./
COPY scripts/__init__.py scripts/assert_database_ready.py scripts/migrate_legacy_secrets.py \
    scripts/prepare_database.py ./scripts/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# 可执行入口置于 PATH，后续命令无需 uv run 前缀
ENV PATH="/app/.venv/bin:$PATH"

# 运行进程只读代码与虚拟环境；入口脚本保留执行权限。
RUN chmod -R a=rX /app/src /app/apps /app/migrations /app/scripts /app/.venv \
    && chmod 755 /app/entrypoint.sh

# 非 root 运行，降低容器逃逸后的影响面
RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid 10001 --home-dir /home/appuser \
      --create-home --shell /usr/sbin/nologin appuser
USER appuser

EXPOSE 8000

# Railway 必须为每个服务显式设置 SERVICE_ROLE=api|worker|scheduler。
# $PORT 由 Railway 注入，本地未设时回退 8000。
CMD ["/app/entrypoint.sh"]
