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
| `META_AUTO_REPLY_ENABLED` | `false` | Allows Meta accounts to use `BOT_ACTIVE`; account-level mode applies to both Facebook DMs and comments |
| `META_COMMENT_REPLY_ENABLED` | `false` | Enables Facebook/Instagram comment OAuth scopes, webhook subscriptions, ingress and public child-comment replies |
| `FACEBOOK_APP_ID` | empty | Facebook Login App ID |
| `FACEBOOK_APP_SECRET` | empty | Must be paired with Facebook App ID |
| `META_VERIFY_TOKEN` | empty | Shared Meta webhook verify token |
| `INSTAGRAM_APP_ID` | empty | Standalone Instagram Login App ID |
| `INSTAGRAM_APP_SECRET` | empty | Must be paired with Instagram App ID |
| `INSTAGRAM_VERIFY_TOKEN` | empty | Falls back to `META_VERIFY_TOKEN` when empty |
| `META_HEALTH_CHECK_INTERVAL_SECONDS` | `600` | Scheduler token, permission and desired subscription reconciliation; range 60-86400 |

`FACEBOOK_APP_*` owns Messenger Pages and Facebook Login Instagram accounts. `INSTAGRAM_APP_*`
owns standalone Instagram Login accounts. The first path requires a Page ID and Page token; the
second forbids a Page ID and stores an Instagram long-lived token. Their generated webhook IDs use
different prefixes, and PostgreSQL enforces uniqueness across both App families.

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
Page/account Graph calls include `appsecret_proof`; the Scheduler repairs missing desired
subscriptions and records sanitized provider health in `PlatformAccount.config`.

Meta comment auto-replies require the platform gate plus `META_COMMENT_REPLY_ENABLED=true` and
`META_AUTO_REPLY_ENABLED=true` on API, Worker, and Scheduler. New Facebook and Instagram
authorizations default to `comments=true` and `BOT_DRAFT_ONLY`; the latter switch only permits an
administrator to promote a tested account explicitly. Facebook OAuth requests
`pages_read_engagement`, `pages_read_user_content`, and `pages_manage_engagement`, validates that
all three permissions target the selected Page, and subscribes the Page to `feed`. Existing Page
tokens must be reauthorized from `/admin/accounts`; missing or wrong-Page permissions produce
`META_COMMENT_PERMISSION_REQUIRED` and health status `REAUTH_REQUIRED`. Replies are always public
child comments on the source comment.

Facebook Login Instagram OAuth requests `pages_read_engagement` and `instagram_manage_comments`;
its linked Page remains subscribed only to `messages`, while the App-level `instagram` webhook
object adds `comments`. Standalone Instagram Login requests
`instagram_business_manage_comments` and adds `comments` to both App-level and account-level
subscriptions. Existing Instagram tokens must be reauthorized through the same login path that
created them.

## Feishu integration

| Variable | Default | Meaning |
| --- | --- | --- |
| `FEISHU_ENABLED` | `false` | Feishu provisioning, normal-event dispatch, health inspection and sending |
| `FEISHU_HANDOFF_NOTIFICATIONS_ENABLED` | `false` | Durable handoff-card creation, updates, card actions and recovery |
| `FEISHU_HEALTH_CHECK_INTERVAL_SECONDS` | `600` | Scheduler credential/Bot health cadence; range 60-86400 |
| `FEISHU_HANDOFF_SWEEP_INTERVAL_SECONDS` | `3` | Scheduler handoff-notification recovery cadence; range 0.5-60 |
| `FEISHU_HANDOFF_SENDER_LEASE_SECONDS` | `30` | Notification sender lease; range 5-600 |
| `FEISHU_HANDOFF_MAX_ATTEMPTS` | `8` | Maximum automatic attempts for deterministic card delivery failures; range 1-100 |

API, Worker and Scheduler must receive the same values, and configuration changes take effect only
after all three roles restart on one flag-aware image. The environment templates keep
`FEISHU_ENABLED=false`. Prepare the self-built application Bot first, deploy the flag-aware image
with Feishu disabled, then enable all three roles together, provision the account and configure the
returned account-specific Callback URL. Handoff cards use a second dark-launch gate: keep
`FEISHU_HANDOFF_NOTIFICATIONS_ENABLED=false` until `/admin/feishu-handoff` has a validated support
chat and operator allowlist and the Feishu console delivers `card.action.trigger` callbacks to the
account-specific Card Action Callback URL. The provider API origin is fixed at
`https://open.feishu.cn` rather than configured by an environment variable.

The Feishu webhook route is always registered. While the feature is disabled, plaintext or encrypted
URL-verification challenges still receive their challenge response. A valid encrypted normal event
is acknowledged and retained as sanitized ignored ingress evidence, but is not dispatched into the
decision pipeline. Provisioning and health work pause, and matching Outbox sends move to recoverable
`NEEDS_REVIEW/FEISHU_DISABLED` without consuming an attempt. Re-enabling all three roles returns
durable work to recovery; disabling never deletes account credentials, callback identity or Outbox
evidence.

## Email integration

| Variable | Default | Validation / meaning |
| --- | --- | --- |
| `EMAIL_ENABLED` | `false` | Master gate for Email provisioning, Scheduler IMAP polling and delivery; must match across API, Worker and Scheduler |
| `EMAIL_AUTO_REPLY_ENABLED` | `false` | Second gate permitting an administrator to promote a provisioned Email account to `BOT_ACTIVE`; it does not bypass `EMAIL_ENABLED` or account policy |
| `EMAIL_POLL_INTERVAL_SECONDS` | `60` | Scheduler IMAP polling cadence; range 5-3600 seconds |
| `EMAIL_MAX_MESSAGES_PER_POLL` | `100` | Per-account message budget for one poll; range 1-1000 |
| `EMAIL_PER_SENDER_DAILY_REPLY_LIMIT` | `5` | Maximum successful automatic Bot replies in 24 hours per account+sender, shared across threads; range 1-100 |
| `EMAIL_NETWORK_TIMEOUT_SECONDS` | `10` | Timeout applied to IMAP/SMTP network operations; range 1-120 seconds |
| `EMAIL_ALLOWED_HOSTS` | `imap.larksuite.com,smtp.larksuite.com` | Comma-separated exact hostname allowlist; canonicalized to lowercase IDNA hostnames, and required to be nonempty when Email is enabled |

All seven values must be identical on API, Worker and Scheduler running the same image. Host matching
happens before DNS; every resolved address must also be a public global target. IP literals,
localhost, private/link-local/loopback/reserved/multicast/unspecified addresses, mixed public/private
answers and hosts absent from the allowlist fail closed. Add a provider host only after operator
review; wildcards are not supported.

Email uses two deployment gates. `EMAIL_ENABLED=true` allows account provisioning, polling and the
Email delivery route. New accounts are nevertheless forced to `BOT_DRAFT_ONLY` by both the API and
Worker provisioning path. `EMAIL_AUTO_REPLY_ENABLED=true` only unlocks the later administrator
promotion to `BOT_ACTIVE`; actual automatic sending still requires both gates, an active and
provisioning-`READY` account, and the account policy. Keep both gates false for the initial image and
migration rollout, enable the master gate on all three roles for draft-only real smoke, and enable
the auto-reply gate only after explicit approval. There is no periodic Email health reconciler or continuous monitoring. The Admin “接入探测” result
and timestamp record only the most recent provisioning-time credential validation over IMAP/SMTP.

The IMAP client uses verified TLS, readonly `SELECT` and `BODY.PEEK[]`. SMTP accepts only SSL or
strict STARTTLS and never downgrades to plaintext. If `smtp_port` is omitted, SSL defaults to 465 and
STARTTLS defaults to 587; an explicitly supplied valid port is preserved. Email polling RawEvents retain UID, UIDVALIDITY,
size and an optional SHA-256 digest, not the RFC822 body. See
[email-integration.md](email-integration.md) for the complete protocol, Phase 0 and rollout contract.

## Decision, LLM and knowledge

| Variable | Default | Meaning |
| --- | --- | --- |
| `LLM_PROVIDER` | `stub` | `stub` or `openai`; stub is forbidden outside tests |
| `PROMPT_VERSION` | `v1-wikifx-multilingual` | Persisted decision/audit identifier for the immutable prompt plus code-compiled structured voice preferences; saved revisions append `#rN` |
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

## Scheduler and reconciliation settings

The scheduler reads one validated settings snapshot at startup. X reconciliation functions also read
one snapshot per public invocation and retain those cadence and budget values for the full run.
Configuration changes take effect after the relevant process restarts. A zero X interval disables
local throttling, which is useful for direct invocations and tests.

Each sweep allows at most one running instance. Missed intervals are coalesced instead of queued, and
a slow sweep is warned about without hard cancellation because reconciliation may have external side
effects.

| Variable | Default | Validation | Consumer |
| --- | --- | --- | --- |
| `SCHEDULER_TICK_SECONDS` | `0.5` | 0.05-10 | Scheduler due-work scan cadence |
| `SCHEDULER_CORE_INTERVAL_SECONDS` | `3` | 0.5-60 | Durable core recovery cadence |
| `SCHEDULER_CORE_WARN_AFTER_SECONDS` | `30` | 1-3600 | Core slow-run warning threshold |
| `SCHEDULER_INSPECTION_WARN_AFTER_SECONDS` | `300` | 1-7200 | Inspection slow-run warning threshold |
| `CHATWOOT_RECONCILE_INTERVAL_SECONDS` | `3` | 1-3600 | Chatwoot reconciliation cadence |
| `X_DM_POLL_INTERVAL_SECONDS` | `90` | 0-86400 | Legacy DM poll cadence |
| `X_WEBHOOK_CHECK_INTERVAL_SECONDS` | `600` | 0-86400 | X webhook health cadence |
| `XCHAT_POLL_INTERVAL_SECONDS` | `900` | 0-86400 | XChat poll cadence |
| `XCHAT_MAX_CONVERSATIONS_PER_POLL` | `10` | 1-1000 | XChat poll work budget |
| `XCHAT_SUBSCRIPTION_CHECK_INTERVAL_SECONDS` | `600` | 0-86400 | XChat subscription reconciliation cadence |
| `XCHAT_RECOVERY_SWEEP_INTERVAL_SECONDS` | `30` | 0-3600 | XChat RawEvent recovery cadence |
| `XCHAT_READY_PROBE_INTERVAL_SECONDS` | `21600` | 0-604800 | Public-key health probe for ready XChat accounts |
| `XCHAT_PENDING_PROBE_INTERVAL_SECONDS` | `600` | 0-86400 | Public-key health probe for pending XChat accounts |

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
