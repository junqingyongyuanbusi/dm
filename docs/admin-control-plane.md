# Platform Account Control Plane

## Decision

Platform accounts are provisioned through a Web administration surface and a durable Provisioning API. Account creation CLIs are not part of the production workflow.

The system is split into two independently deployable concerns:

- **Control Plane**: administrator authentication, credential intake, account provisioning, webhook setup, capability checks, health, enable/disable, retry, and audit.
- **Data Plane**: signed platform webhooks, `CanonicalEvent`, durable decision jobs, the shared Rules/RAG/LLM/Final Guard pipeline, transactional Outbox, and account-scoped senders.

A Control Plane outage must not stop already-connected accounts from receiving or sending messages.
Already-created Worker and Scheduler work continues from PostgreSQL, although operators cannot make
new local claims, approvals or manual replies until Admin is available again.

## Local-first operations contract

`/admin`, PostgreSQL `HumanWorkItem`, and the transactional Outbox are the native operations control
plane. Chatwoot is an optional compatibility bridge; it does not own local claims, draft review,
delivery exceptions, conversation automation state, or manual-reply provenance.

Every human mutation is tenant-scoped, audited and durable:

- **claim and take over** requires a `WAITING` item and its expected optimistic `version`; one
  conversation-locked transaction assigns the user/actor, moves the item to `CLAIMED`, moves the
  normal `HANDOFF_PENDING` state to `HUMAN_ACTIVE`, advances both versions, writes both audits, and
  cancels only pending or failed `DECISION/BOT` Outboxes for that conversation;
- **resolve and restore account policy** requires a current `CLAIMED` version and ownership, unless a
  superadmin explicitly overrides it; one transaction resolves the item and restores the current
  `PlatformAccount.automation_default`, with a safe `BOT_DRAFT_ONLY` fallback when the deployment's
  Meta release gate disallows a stored `BOT_ACTIVE` target;
- **resume** remains a compatibility/exception action after no open work remains. It can explicitly
  recover stranded legacy `HANDOFF_PENDING`, `HUMAN_ACTIVE`, or `BOT_COOLDOWN` states to
  `BOT_DRAFT_ONLY` or permitted `BOT_ACTIVE`; normal resolved work does not need this second click;
- **manual reply** binds to an explicit inbound `Message` target, creates or claims the work item,
  moves the conversation to `HUMAN_ACTIVE`, and commits a `MANUAL_REPLY/ADMIN_HUMAN` Outbox carrying
  `actor_id` and `reply_to_message_id`.

Successful local manual delivery records an outbound `Message` linked through `source_outbox_id`
and does not require a synthetic `ReplyDecision`. Direct-platform accounts do not require a
Chatwoot conversation; accounts intentionally configured for Chatwoot delivery still require their
persisted conversation mapping.

## Trust boundaries

1. Browser administrators authenticate through PostgreSQL-backed server-side sessions. The browser receives only an opaque, HTTP-only session token; PostgreSQL stores its HMAC digest, never the raw token. The browser never receives `CONTROL_API_KEY`.
2. `CONTROL_API_KEY` remains a server-to-server credential for automation and future external admin services.
3. Platform credentials are accepted only over TLS and stored as application-encrypted Fernet envelopes in PostgreSQL. Encryption keys remain outside PostgreSQL in `PLATFORM_SECRET_KEYS`; job request/result JSON never contains credentials.
4. `ADMIN_USERNAME` / `ADMIN_PASSWORD` remain the bootstrap superadmin and may access every tenant in `ADMIN_ALLOWED_TENANTS`. Database users are bound to exactly one tenant; every read and mutation is scoped to the current Principal and audited with the human or service actor.
5. Webhook route identifiers are globally unambiguous within their platform namespace.
6. Sender instances are isolated by `(platform, platform_account_id, config_version)`.

## Durable provisioning flow

```text
Admin Web / Provisioning API
  -> validate request and tenant authorization
  -> encrypt staging bundle with PLATFORM_SECRET_KEYS
  -> insert ProvisioningJob(PENDING) and audit submission
  -> enqueue Dramatiq actor
  -> atomic claim PROCESSING
  -> validate platform credentials
  -> provision PlatformApp and/or PlatformAccount
  -> configure webhook where the platform permits automation
  -> persist sanitized result and audit completion
  -> clear encrypted staging bundle after completion
```

Failed jobs retain only the encrypted staging envelope for controlled retry, use exponential backoff, and are recovered by the scheduler. Errors are normalized to codes and sanitized messages; raw HTTP requests and exception representations are not persisted.

## Account model

`PlatformApp` owns application-level webhook credentials and one webhook route. It may contain many `PlatformAccount` rows.

- Telegram: one direct `PlatformAccount`, account-level webhook.
- Feishu: one tenant-scoped direct `PlatformAccount` for one enterprise self-built application Bot;
  App ID is the external account identity, the account owns all four credentials and its webhook
  route, and no `PlatformApp` is created.
- Email: one tenant-scoped direct `PlatformAccount` per mailbox; the account owns encrypted login
  credentials plus IMAP/SMTP/threading configuration, has no webhook, and no `PlatformApp` is
  created. New accounts are always provisioned as `BOT_DRAFT_ONLY`.
- Facebook/Instagram: one Meta `PlatformApp`, multiple Page/Professional Account rows. App secrets sign webhook bodies and generate `appsecret_proof`; account tokens remain account-scoped. Facebook Login Instagram rows belong to family `meta` and require a Page ID; standalone Instagram Login rows belong to family `instagram` and forbid a Page ID. Shared webhook public IDs are unique across both families.
- WhatsApp: a Meta `PlatformApp` plus one account per `phone_number_id`.
- X: deployment-level OAuth Consumer App credentials, a tenant-shared `PlatformApp` webhook route, and one `PlatformAccount` per authorized user. Events route by `for_user_id`; legacy account-level webhook secrets remain readable during migration.

Each account owns tenant, brand, external ID, route ID, credential reference, capabilities, account-wide automation policy, config version, and lifecycle status. `PlatformAccount.automation_default` applies across the account's conversations as their initialization and post-human-work restore target; changing or handling one conversation does not rewrite sibling conversation state.

## Shared reply behavior

All platforms use the same decision pipeline. Platform adapters only normalize inbound events; senders only translate explicit Outbox destinations.

Supported destination commands:

- `telegram_dm`
- `meta_messenger_dm`
- `meta_instagram_dm`
- `meta_public_comment`
- `meta_private_reply`
- `feishu_p2p_reply`
- `feishu_group_reply`
- `email_reply`
- `whatsapp_session_message`
- `x_dm`
- `x_chat_message`
- `x_post_reply`

Direct-platform drafts are never sent to customers. A `DRAFT` decision is retained in `reply_decisions` for future approval/inbox workflows and has no direct Outbox until approved. Chatwoot private notes remain supported for legacy Chatwoot-backed accounts.

At delivery time the system revalidates account status, tenant/account/conversation consistency, capability, target type, text length, expiration/window, and takeover state.

## Admin Web first slice

The built-in administration surface provides:

- bootstrap-superadmin and tenant-user login/logout with opaque HTTP-only cookies, server-side revocation, and CSRF checks;
- grouped navigation under `/admin`: operations (`/admin/inbox`, `/admin/conversations`), content and policy (`/admin/content/*`), integrations (`/admin/integrations/*`), and system administration (`/admin/system/*`);
- superadmin-only direct user creation at `/admin/system/users`, with `/admin/users` retained as a compatibility route and no email/invitation flow;
- mandatory first-login password change for newly created tenant users;
- tenant-user self-service account authorization for the user's assigned tenant, including OAuth starts and tenant-scoped provisioning jobs;
- superadmin-only tenant-wide automation kill switch at `/admin/system/safety`; tenant users retain account-level controls for their own accounts;
- PostgreSQL-backed runtime health summary on `/admin`, covering ingestion recovery, decision jobs, Outbox, provisioning, active X sync gaps, and disabled accounts with oldest backlog age;
- an operations inbox at `/admin/inbox` for human handoff, draft review, and delivery exceptions, with direct-message versus public-interaction filtering and oldest-wait ordering;
- Feishu handoff routing at `/admin/integrations/feishu/handoff`, with `/admin/feishu-handoff` retained for compatibility, including one support-chat route per Tenant, an explicit app-scoped operator allowlist, a non-customer-data test card, and read-only notification failure visibility;
- a conversation archive at `/admin/conversations`, where direct messages and public comments/mentions are separated while every reply remains bound to an explicit inbound message target;
- read-only runtime diagnostics at `/admin/system/health`, with `/admin/health` retained for compatibility; draft approval and delivery retry remain inbox workflows instead of being duplicated across diagnostic pages;
- platform account and provisioning-job overview at `/admin/integrations/accounts`, with provider-specific deep links under `/admin/integrations/accounts/new/{provider}` and `/admin/accounts` retained for compatibility;
- Telegram, Facebook, Instagram, WhatsApp, Feishu, Email, and X connection forms;
- asynchronous job status and retry, with current job pages under `/admin/integrations/provisioning-jobs/{job_id}` and legacy `/admin/jobs/{job_id}` still accepted;
- account enable/disable and health checks, including separate Messaging/Comments status plus app-level and account-level subscription state; Meta accounts can only use `BOT_ACTIVE` when the deployment sets `META_AUTO_REPLY_ENABLED=true`, and every change is written to `audit_logs` as `SET_AUTOMATION_DEFAULT`; when `META_COMMENT_REPLY_ENABLED=true` too, newly authorized Facebook and Instagram accounts enable comments, still start as `BOT_DRAFT_ONLY`, validate account-targeted comment permissions, and install the required App/account webhook fields;
- finite brand-voice controls at `/admin/content/brand-voice`, with `/admin/prompt` retained for compatibility: administrators select only tone, length, empathy, and emoji enums. Saves dual-write canonical `voice_preferences` JSON and a code-compiled compatibility `persona`, bump the revision recorded in `reply_decisions.prompt_version`, and audit structured values as `SET_REPLY_PERSONA`; arbitrary system instructions are not accepted, and dry-run uses only compiled text without persisting a decision or Outbox;
- draft-first knowledge management at `/admin/content/knowledge`, with `/admin/knowledge` retained for compatibility: manual and CSV-created rows cannot be retrieved until an explicit tenant-scoped publish action. Publication and withdrawal are row-locked, idempotent, and audited. Official-contact classification is explicit, draft-only, tenant-scoped, row-locked, and audited as `SET_KNOWLEDGE_OFFICIAL_CONTACT`; only a published, verbatim-selected classified template can pass the deterministic contact PII exception;
- OAuth callbacks, signed webhooks, `/healthz`, and `/api/v1` remain stable protocol contracts and are not moved with the browser information architecture;
- no account-creation CLI requirement.

OAuth states contain the initiating server-side session ID and tenant. Callbacks revalidate that session and its current tenant permissions before exchanging credentials or creating a provisioning job, so logout, expiry, password change, or tenant revocation invalidates an in-flight authorization.

### Email provisioning contract

A tenant administrator can submit an Email account through `/admin/integrations/accounts/new/email` (legacy `/admin/accounts` remains available) or
`POST /api/v1/platform-accounts/email`. The request separates the mailbox address, IMAP/SMTP hosts,
ports, mailbox, TLS mode and policy fields from encrypted username/password secrets. Both the API
schema and Worker provisioning path require `BOT_DRAFT_ONLY` and reject hosts absent from
`EMAIL_ALLOWED_HOSTS` before any DNS or provider connection.

Provisioning performs an IMAP SSL login plus readonly mailbox selection and an authenticated SMTP
SSL or strict STARTTLS probe without sending a message. An omitted SMTP port defaults from the
selected security mode (SSL 465, STARTTLS 587), while an explicit valid port is preserved. Admin
shows this as the latest “接入探测” result and time: it is only a credential access validation, not
continuous monitoring or a periodic Email health reconciler. Repository tests use fake clients and
do not establish a real provider connection. Administrator-provided credentials must
still pass Phase 0 and the draft-only real smoke in `docs/email-integration.md` before any account is
promoted. Automatic sending additionally requires `EMAIL_ENABLED=true` and
`EMAIL_AUTO_REPLY_ENABLED=true` on API, Worker and Scheduler.

### Feishu provisioning contract

A tenant administrator selects Feishu at `/admin/integrations/accounts/new/feishu` and supplies exactly four Feishu values:
`app_id`, `app_secret`, `verification_token`, and `encrypt_key`, together with the already-selected
Tenant and Brand and an optional display name. The same tenant-scoped operation is available to
service callers at `POST /api/v1/platform-accounts/feishu`. The browser and job result never receive
the secrets back: intake is encrypted into the durable staging envelope, final credentials are
stored in the account's encrypted PostgreSQL bundle, and completed jobs clear staging material.

Provisioning validates the tenant token, Bot identity and active Bot status, then returns the
account-specific Callback URL and manual steps. It does not and cannot configure the Feishu event
callback through the provider API. An operator must enter the Callback URL in the Feishu developer
console, complete URL verification, subscribe to `im.message.receive_v1`, publish the application,
and add the Bot where group mentions are expected.

Feishu provisioning only accepts `BOT_DRAFT_ONLY`. After callback setup and draft smoke testing, an
administrator must explicitly use the account action on `/admin/integrations/accounts` to change that account to
`BOT_ACTIVE`; the mutation is tenant-scoped and audited as `SET_AUTOMATION_DEFAULT`. This explicit
post-provisioning promotion is separate from Feishu's deployment feature gate and does not imply
that production credentials or a live provider E2E are present.

### Feishu handoff notification contract

The handoff notification plane reuses a Tenant's existing Feishu enterprise self-built application
credentials but does not grant all Feishu users access to work items. `/admin/integrations/feishu/handoff`
selects one active Feishu account and one support group `chat_id` per Tenant, then maintains an
explicit `open_id` allowlist with independent claim and resolve permissions. Route, operator and
test-card mutations are tenant-scoped, CSRF-protected and audited.

The page displays the account-specific Card Action Callback URL. Operators must configure Feishu to
deliver `card.action.trigger` callbacks to that URL separately from `im.message.receive_v1`, publish
the app version, add the Bot to the support group and enable
`FEISHU_HANDOFF_NOTIFICATIONS_ENABLED` on API, Worker and Scheduler only after the test card arrives.
Unknown or ambiguous test-card creation is reported as indeterminate and must not be retried until
the support group has been checked.

A new HANDOFF remains valid if notification routing is absent: Reply Core records a durable
`BLOCKED_CONFIG` intent and Scheduler can recover it after configuration. Claim and resolve card
actions use the same work-item versions and account-policy restoration as Admin. By default only the
claimant may resolve. The “已回复，恢复 Bot” action records `FEISHU_OPERATOR_ATTESTED`; it is an
operator declaration because Reply Core cannot verify an external social-platform reply. Use the
local inbox manual reply when durable Outbox delivery evidence is required.

Production deployments should put `/admin` behind an identity-aware proxy or replace the local administrator login with OIDC/MFA. The service API and data-plane webhooks remain separate.

## OAuth evolution

Meta and X OAuth are adapters into the same `ProvisioningJob` boundary. Messenger/Instagram accounts route inbound evidence with `meta_health_status=PROVISIONING`, but send-time validation pauses customer delivery until the requested remote subscription reaches `READY`; a subscription failure disables the account. Facebook comment authorization requests `pages_read_engagement`, `pages_read_user_content`, and `pages_manage_engagement`; Facebook Login Instagram requests `pages_read_engagement` and `instagram_manage_comments`; standalone Instagram Login requests `instagram_business_manage_comments`. Provisioning rejects missing or wrong-target comment permissions with `META_COMMENT_PERMISSION_REQUIRED`. Periodic Scheduler checks repair subscription drift and expose permission loss as `REAUTH_REQUIRED`. OAuth transaction state is encrypted and short-lived in Redis. Exchanged platform credentials are encrypted with `PLATFORM_SECRET_KEYS` and staged in the durable job row, then stored as encrypted PostgreSQL bundles on the resulting `PlatformApp` / `PlatformAccount`; completed jobs clear the staging bundle. `ACCOUNT_SECRETS_ROOT` is retained only for legacy `file://` migration. Platform App Review, Business Verification, WhatsApp template approval, and X product-tier permissions remain external prerequisites and are surfaced as capabilities/manual steps rather than bypassed.
