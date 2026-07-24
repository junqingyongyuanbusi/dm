# Configuration reference

Runtime application settings are defined by `social_reply.shared.config.Settings`. Environment
variables use the uppercase field name. Values are resolved in this order: explicit constructor
arguments, process environment, `.env` in the process current working directory, then code defaults.

Use `.env.example` for local development only. Use `deploy/vps/.env.example` for production and
replace every secret. API, Worker, and Scheduler must use the same application settings unless a
variable is explicitly deployment-role-only.

## Core and security

| Variable | Code default | Requirement / owner |
| --- | --- | --- |
| `DATABASE_URL` | local `social_reply` asyncpg URL | PostgreSQL durable store; `postgres://` and `postgresql://` are normalized to asyncpg |
| `REDIS_URL` | `redis://localhost:6379/0` | Dramatiq, kill switches and OAuth transient state |
| `TENANT_ID` | `default` | Legacy/default tenant input; request and Principal scope remain authoritative |
| `TESTING` | `false` | Enables test-only stubs and relaxed production validation; never true in production |
| `PLATFORM_SECRET_KEYS` | empty | Always required; comma-separated Fernet keys, first encrypts and all decrypt |
| `CONTROL_API_KEY` | empty | Required outside tests; server-to-server Provisioning API only |
| `ADMIN_SESSION_SECRET` | empty | Required outside tests, at least 32 characters, identical on all API instances |
| `ADMIN_USERNAME` | empty | Required outside tests; bootstrap superadmin |
| `ADMIN_PASSWORD` | empty | Required outside tests; bootstrap superadmin |
| `ADMIN_ALLOWED_TENANTS` | `default` | Required outside tests; comma-separated bootstrap-superadmin scope |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Must be HTTPS outside tests; source for callback/webhook URLs |
| `ACCOUNT_SECRETS_ROOT` | `.secrets/accounts` | Legacy `file://` credential migration only |

`PLATFORM_SECRET_KEYS` is validated even in test mode. Losing the key set makes existing encrypted
platform credentials unreadable. Key rotation prepends a new key and retains old keys until every
envelope has been rewritten and backups have aged out.

## Chatwoot bridge

| Variable | Default | Requirement |
| --- | --- | --- |
| `CHATWOOT_ENABLED` | `false` | Must match across API, Worker and Scheduler |
| `CHATWOOT_WEBHOOK_SECRET` | `change-me` | Required and non-default when bridge is enabled outside tests |
| `CHATWOOT_SIGNATURE_TOLERANCE_SECONDS` | `300` | Signed webhook timestamp window |
| `CHATWOOT_BASE_URL` | `http://localhost:3000` | Chatwoot API origin |
| `CHATWOOT_API_TOKEN` | `dev-local-token` | Required and non-default when bridge is enabled outside tests |

## X integration

| Variable | Default | Meaning |
| --- | --- | --- |
| `X_API_KEY` | empty | Deployment-level OAuth 1.0a Consumer Key |
| `X_API_SECRET` | empty | Deployment-level OAuth 1.0a Consumer Secret; must be paired with key |
| `X_LEGACY_DM_ENABLED` | `true` | Legacy DM permission probing and `x_dm` sending |
| `X_ACTIVITY_ENABLED` | `true` | CRC/signed webhook transport and webhook health |
| `XCHAT_ENABLED` | `true` | XChat activation, subscription, webhook processing and sending |
| `X_OAUTH_LEGACY_STATE_WRITE` | `false` | Temporary two-phase OAuth Redis-key rollout switch |

Code defaults preserve upgrades, while both deployment templates explicitly set
`XCHAT_ENABLED=false` for new environments. Legacy DM or XChat requires the X application to have
Read and write and Direct message permission.

Disabling a stack is not credential deletion. Recoverable sends pause and durable account material
is preserved. Legacy DM and XChat polling use PostgreSQL checkpoints, leases and resumable gaps;
a disabled polling stack performs no provider reconciliation until it is re-enabled.

## Meta and Instagram applications

Code defaults keep existing deployments enabled during upgrades. Both environment templates
explicitly set the three platform flags to `false`, so new deployments opt in account by account.
API, Worker and Scheduler must use the same values.

| Variable | Default | Meaning |
| --- | --- | --- |
| `FACEBOOK_MESSENGER_ENABLED` | `true` | Facebook Page text-DM ingress, provisioning, health reconciliation and sending |
| `INSTAGRAM_MESSAGING_ENABLED` | `true` | Instagram professional-account text-DM ingress, provisioning, health reconciliation and sending |
| `WHATSAPP_ENABLED` | `true` | WhatsApp Cloud API ingress, provisioning and sending |
| `FACEBOOK_APP_ID` | empty | Facebook Login App ID |
| `FACEBOOK_APP_SECRET` | empty | Must be paired with Facebook App ID |
| `META_VERIFY_TOKEN` | empty | Shared Meta webhook verify token |
| `INSTAGRAM_APP_ID` | empty | Standalone Instagram Login App ID |
| `INSTAGRAM_APP_SECRET` | empty | Must be paired with Instagram App ID |
| `INSTAGRAM_VERIFY_TOKEN` | empty | Falls back to `META_VERIFY_TOKEN` when empty |
| `META_HEALTH_CHECK_INTERVAL_SECONDS` | `600` | Scheduler token and `messages` subscription reconciliation; range 60-86400 |

Changing one of these flags is a coordinated three-role operation, not an ordinary mixed-version
rolling update. Old images do not understand the flags and can still accept or send traffic. Use
this sequence:

1. Deploy the flag-aware image to API, Worker and Scheduler with the existing values still `true`.
2. Confirm all old containers have exited and all three roles report the same configuration.
3. Stop API, Worker and Scheduler together, change the flag to `false`, then start all three roles.
   This brief coordinated restart is required because an old Worker can still send queued work.
4. To re-enable, restart all three roles with the flag set to `true`; paused provisioning and Outbox
   work will return to their durable queues automatically.

A disabled signed webhook stores only a tenant/app-scoped audit summary and SHA-256 body digest. It
does not copy message text, names or phone numbers into the gate audit row. Enabled Messenger and
Instagram requests store one minimal verified-request record plus account-scoped occurrence
RawEvents, so replay and tenant ownership do not depend on an account-unscoped multi-entry payload.
Page/account Graph calls include `appsecret_proof`; the Scheduler repairs missing `messages`
subscriptions and records sanitized provider health in `PlatformAccount.config`.

## Decision, LLM and knowledge

| Variable | Default | Meaning |
| --- | --- | --- |
| `LLM_PROVIDER` | `stub` | `stub` or `openai`; stub is forbidden outside tests |
| `PROMPT_VERSION` | `v0-stub` | Persisted decision/audit prompt identifier |
| `OPENAI_API_KEY` | empty | Required outside tests when provider is `openai` |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible API base |
| `OPENAI_MODEL` | `gpt-4o-mini` | Chat completion model |
| `OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small` | Knowledge embedding model/version |
| `OPENAI_TIMEOUT_SECONDS` | `30` | HTTP timeout for chat and embedding calls |
| `KNOWLEDGE_RETRIEVAL_ENABLED` | `false` | Enables knowledge retrieval |
| `KNOWLEDGE_MIN_SIMILARITY` | `0.5` | Minimum retrieval score |
| `KNOWLEDGE_TOP_K` | `3` | Maximum retrieved chunks |
| `KNOWLEDGE_VERBATIM_REPLY` | `false` | Return matched template text without LLM rewriting |
| `REQUIRE_KNOWLEDGE` | `false` | Handoff without calling LLM when retrieval has no match |
| `CONVERSATION_HISTORY_LIMIT` | `20` | Prior messages sent to decision context; range 0-50 |
| `CONVERSATION_HISTORY_MAX_CHARS` | `12000` | Total history character budget; range 0-50000 |

## Module-level reconciliation variables

These variables are not yet `Settings` fields. Their modules read them once at import, so changing
them requires recreating API/Worker/Scheduler containers as applicable.

| Variable | Default | Consumer |
| --- | --- | --- |
| `X_DM_POLL_INTERVAL_SECONDS` | `90` | Scheduler legacy DM poll |
| `X_WEBHOOK_CHECK_INTERVAL_SECONDS` | `600` | Scheduler X webhook health |
| `XCHAT_POLL_INTERVAL_SECONDS` | `900` | Scheduler XChat poll |
| `XCHAT_MAX_CONVERSATIONS_PER_POLL` | `10` | XChat poll work budget |
| `XCHAT_SUBSCRIPTION_CHECK_INTERVAL_SECONDS` | `600` | Scheduler XChat subscription reconciliation |
| `XCHAT_RECOVERY_SWEEP_INTERVAL_SECONDS` | `30` | Scheduler XChat RawEvent recovery sweep |
| `XCHAT_READY_PROBE_INTERVAL_SECONDS` | `21600` | Public-key health probe for ready XChat accounts |
| `XCHAT_PENDING_PROBE_INTERVAL_SECONDS` | `600` | Public-key health probe for pending XChat accounts |

## Deployment-only variables

These are consumed by container orchestration or `entrypoint.sh`, not by `Settings`.

| Variable | Owner | Meaning |
| --- | --- | --- |
| `SERVICE_ROLE` | entrypoint | `api`, `worker`, or `scheduler` |
| `PORT` | entrypoint/API | API listen port, default 8000 |
| `PG_PASSWORD` | VPS compose | PostgreSQL application password |
| `CLOUDFLARE_TUNNEL_TOKEN` | VPS compose | Cloudflare Tunnel authentication |

VPS compose injects `DATABASE_URL`, `REDIS_URL`, `SERVICE_ROLE`, and `PORT` into containers. Do not
add role-specific copies of feature flags; divergent values can accept work that another role will
not process or recover.

## Template policy

- `.env.example`: executable single-process smoke profile with `TESTING=true`, inline actor
  fallbacks, StubBroker, stub LLM, knowledge retrieval disabled, localhost callbacks and public
  development-only secrets. It does not validate the production Redis/Dramatiq boundary.
- `deploy/vps/.env.example`: production profile with `TESTING=false`; every blank secret must be
  generated or copied from the current production environment.
- Repository test configuration: always points at a database whose name ends in `_test`; pytest
  refuses to collect against any other database.
