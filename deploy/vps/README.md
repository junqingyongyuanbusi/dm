# Reply Core · VPS 部署手册(Ubuntu + Docker + Cloudflare Tunnel)

拓扑:`cloudflared`(出站穿透)→ `api` → `worker`/`scheduler` + `postgres(pgvector)` + `redis`。
全栈跑在 compose 内网,**零入站端口**——VPS 防火墙只需开 SSH。

## 一次性准备

### 1. 装 Docker(Ubuntu)

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker
```

### 2. 建 Cloudflare Tunnel

Cloudflare Zero Trust 控制台 → Networks → Tunnels → Create tunnel(Cloudflared 类型):

1. 复制 token(形如 `eyJh...`)填入 `.env` 的 `CLOUDFLARE_TUNNEL_TOKEN`
2. Public Hostname 添加:`relay.nexory.top` → Service 选 **HTTP**,URL 填 `api:8000`
   (cloudflared 与 api 同 compose 网络,容器名直连)
3. Cloudflare 会自动创建该域名的 DNS 记录——**先把现有指向 Railway 的 CNAME 删掉**,否则冲突

### 3. 准备目录与配置

```bash
mkdir -p ~/reply-core && cd ~/reply-core
# 拷入本目录的 docker-compose.yml 与 .env.example(scp 或 git clone 后 cp)
cp .env.example .env && chmod 600 .env
vim .env   # 逐项填写;标 [Railway 原样复制] 的项必须与 Railway Variables 一致
```

⚠ **`PLATFORM_SECRET_KEYS` 是命门**:数据库里所有平台凭证(X/Telegram 四元组、
webhook 密钥)都用它加密,填错或丢失则全部凭证不可解密、服务拒绝启动。

### 4. 迁移生产数据(在启动应用之前!)

顺序必须是:先起数据库 → restore → 再起应用。反过来 api 会在空库跑迁移,restore 必冲突。

```bash
# 只起数据库
docker compose up -d postgres
# 从 Railway 导出(本机执行;DATABASE_PUBLIC_URL 从 Railway Postgres 服务 Variables 取)
docker run --rm -e PGURL="<railway 的 DATABASE_PUBLIC_URL>" -v "$PWD:/backup" \
  postgres:18-alpine sh -c 'pg_dump "$PGURL" -Fc -f /backup/railway.dump'
# 导入 VPS(dump 拷到 VPS 后)
docker compose cp railway.dump postgres:/tmp/railway.dump
docker compose exec postgres pg_restore -U app -d social_reply --no-owner /tmp/railway.dump
```

全新部署(无历史数据)可跳过本节,api 首次启动会自动建表。

## 启动与验证

```bash
docker compose up -d
docker compose ps                          # 六个容器全部 Up
docker compose logs api | tail -20         # 应见 prepare_database + uvicorn 启动
docker compose logs scheduler | tail -5    # 应见 sweep 循环,无 traceback
curl -s https://relay.nexory.top/healthz   # {"status":"ok"}(经 Cloudflare 全链路)
```

使用 `.env` 中的 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 作为 bootstrap 超级管理员登录 `https://relay.nexory.top/admin`。在“用户”页创建一个绑定到单一 Tenant 的普通用户，确认其首次登录被强制修改密码，且改密后只能看到该 Tenant 数据；再发一条 Telegram 消息验证端到端自动回复。
稳定运行几天后再停掉 Railway 服务(保留作回滚退路)。

### X OAuth 配置

`.env` 中的 `X_API_KEY` / `X_API_SECRET` 必须来自 X Developer Portal 的
**Consumer Keys**，不能填写 OAuth 2.0 Client ID / Client Secret。User authentication
settings 使用 **OAuth 1.0a**，App permissions 设为 **Read and Write**，App type 使用
**Web App**，Callback URI 必须与 `PUBLIC_BASE_URL` 完全一致：

```text
https://relaytest.nexory.top/admin/oauth/x/callback
```

修改 `.env` 后需重建应用容器，而不是只重启进程：

```bash
docker compose pull
# 强制让 api/worker/scheduler 读取新镜像和新环境变量
docker compose up -d --force-recreate api worker scheduler
```

## 日常运维

**更新发布**(本机构建推送后,VPS 两条命令):

```bash
docker compose pull && docker compose up -d
```

**每日自动备份**(crontab -e):

```cron
0 4 * * * cd ~/reply-core && docker compose exec -T postgres pg_dump -U app -Fc social_reply | gzip > backup-$(date +\%F).dump.gz && ls backup-*.gz | head -n -14 | xargs -r rm
```

**主机加固**:

```bash
sudo ufw default deny incoming && sudo ufw allow OpenSSH && sudo ufw enable   # 仅开 SSH
# SSH 改密钥登录、装 unattended-upgrades 自动安全更新
```

**内核参数(Redis 必做)**:Redis AOF/RDB 后台保存依赖 fork,内核默认
`vm.overcommit_memory=0` 在低内存时会使 fork 失败(启动日志会有 WARNING 提示):

```bash
sudo sysctl vm.overcommit_memory=1                                        # 立即生效
echo 'vm.overcommit_memory = 1' | sudo tee /etc/sysctl.d/99-redis.conf    # 重启后仍生效
```

**监控**:用 UptimeRobot(免费)盯 `https://relay.nexory.top/healthz`;
`df -h` 留意磁盘(日志已限 20MB×5/容器,pgdata 随业务增长)。

## 回滚

- 应用回滚:`.env` 不动,compose 里镜像 tag 改回上一版(如 `x-fastpath`)→ `docker compose up -d`
- 整体回滚:DNS 侧删除 Tunnel 的 hostname、恢复 Railway CNAME(Railway 服务未删即可秒切)

## 与 Railway 的差异备忘

| 项 | Railway | VPS |
|----|---------|-----|
| TLS/域名 | 平台自动 | Cloudflare Tunnel 终结,零入站端口 |
| 迁移执行 | api 容器启动时 | 相同(entrypoint 不变) |
| 平台 webhook / OAuth 回调 | 指向域名 | 域名不变,**全部无需重新配置** |
| 备份 | 手动 | cron 每日 pg_dump(见上) |
| 日志 | 平台收集 | json-file 限额 + `docker compose logs` |
