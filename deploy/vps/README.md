# Social Reply · VPS 部署手册(Ubuntu + Docker + Cloudflare Tunnel)

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

### Chatwoot 开关

直连部署保持 `CHATWOOT_ENABLED=false`，此时无需配置 Chatwoot token/secret，API 不注册 `/webhooks/chatwoot`，Scheduler 也不执行 Chatwoot reconcile。Worker 仍注册兼容 Actor，用于排空切换前已进入队列的事件；这些事件的回复决策会等待重新启用 Chatwoot，不会被静默完成。

若迁移环境仍使用 Chatwoot Bridge，必须为 `api`、`worker`、`scheduler` 同时设置 `CHATWOOT_ENABLED=true`，并填写 `CHATWOOT_BASE_URL`、`CHATWOOT_API_TOKEN`、`CHATWOOT_WEBHOOK_SECRET`。滚动发布期间不要让三个角色长期使用不同开关值。

### X OAuth 配置

`.env` 中的 `X_API_KEY` / `X_API_SECRET` 必须来自 X Developer Portal 的
**Consumer Keys**，不能填写 OAuth 2.0 Client ID / Client Secret。User authentication
settings 使用 **OAuth 1.0a**，App permissions 设为
**Read and write and Direct message**，App type 使用 **Web App**。Callback URI 必须与
`PUBLIC_BASE_URL` 完全一致，使用 HTTPS、无结尾斜杠、无 query：

```text
${PUBLIC_BASE_URL}/admin/oauth/x/callback
```

修改 `.env` 后需重建应用容器，而不是只重启进程：

```bash
docker compose pull
# 强制让 api/worker/scheduler 读取新镜像和新环境变量
docker compose up -d --force-recreate api worker scheduler
```

#### X OAuth Redis Key 两阶段发布

从旧版 `oauth:x:<raw-token>` 迁移到
`x:oauth1:transaction:<sha256(oauth_token)>` 时，必须避免新 writer 与旧 reader
在滚动窗口中交叉：

1. Phase 1：临时设置 `X_OAUTH_LEGACY_STATE_WRITE=true`，部署新镜像。此时新代码仍写旧
   key，但可以读取并一次性删除新旧两类 key；等待所有旧 API 副本退出。
2. Phase 2：设置 `X_OAUTH_LEGACY_STATE_WRITE=false`，再次滚动 API。此时所有在途副本都已
   具备双读能力，新请求只写 SHA-256 key。
3. 确认至少经过一个 10 分钟 OAuth state TTL 后，再删除该临时变量或保持 `false`。

Phase 2 后若需要回滚，只能回滚到具备双读能力的版本；若必须回滚到更老版本，应先恢复
`true` 并等待 10 分钟排空 hash-key transaction，避免回调随机丢失。

## 日常运维

**更新发布**（API 先迁移，Worker/Scheduler 后启动）：

```bash
cd ~/reply-core
mkdir -p backups
docker compose exec -T postgres pg_dump -U app -Fc social_reply \
  > "backups/pre-upgrade-$(date +%F-%H%M%S).dump"

docker compose stop worker scheduler
docker compose pull api worker scheduler
docker compose up -d --no-deps --force-recreate api
docker compose logs --tail=80 api

# 确认迁移到当前 head 后再启动后台进程
docker compose exec -T postgres psql -U app -d social_reply -Atc \
  'SELECT version_num FROM alembic_version;'
docker compose up -d --no-deps --force-recreate worker scheduler
curl -fsS http://127.0.0.1:8000/healthz
```

当前 Alembic head 为 `f3a6c1d8e250`。升级会短暂锁定 `messages` 与
`outbox_messages`，为现有消息回填稳定历史序号，并把已确认 `SENT` 的文本 Outbox
补成 assistant 历史。若 VPS 曾应用早期的 `da4e19c7b203` 但缺少
`admin_sessions.credential_fingerprint`，前一修复 revision 也会自动补列。
不要只执行 `docker compose restart`：它不会拉取 Docker Hub 上更新后的标签。

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
