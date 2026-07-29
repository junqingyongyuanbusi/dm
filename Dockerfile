# syntax=docker/dockerfile:1
# Reply Core 运行镜像：单镜像承载 API / worker / scheduler 三类进程，
# 由部署平台（Railway）为每个服务覆盖启动命令。KISS：不为每个进程单独出镜像。

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS base

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

# --- 项目层：拷贝源码后安装本项目 ---
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# 可执行入口置于 PATH，后续命令无需 uv run 前缀
ENV PATH="/app/.venv/bin:$PATH"

# 入口脚本按 SERVICE_ROLE 分发（api/worker/scheduler），三服务共用本镜像
RUN chmod +x /app/entrypoint.sh

# 非 root 运行，降低容器逃逸后的影响面
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 默认角色 api；worker/scheduler 由 Railway 设置 SERVICE_ROLE 环境变量切换。
# $PORT 由 Railway 注入，本地未设时回退 8000。
CMD ["/app/entrypoint.sh"]
