# Platform Account Control Plane

## Decision

Platform accounts are provisioned through a Web administration surface and a durable Provisioning API. Account creation CLIs are not part of the production workflow.

The system is split into two independently deployable concerns:

- **Control Plane**: administrator authentication, credential intake, account provisioning, webhook setup, capability checks, health, enable/disable, retry, and audit.
- **Data Plane**: signed platform webhooks, `CanonicalEvent`, durable decision jobs, the shared Rules/RAG/LLM/Final Guard pipeline, transactional Outbox, and account-scoped senders.

A Control Plane outage must not stop already-connected accounts from receiving or sending messages.

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
- Facebook/Instagram: one Meta `PlatformApp`, multiple Page/Professional Account rows. App secrets sign webhook bodies and generate `appsecret_proof`; account tokens remain account-scoped. Facebook Login Instagram rows belong to family `meta` and require a Page ID; standalone Instagram Login rows belong to family `instagram` and forbid a Page ID. Shared webhook public IDs are unique across both families.
- WhatsApp: a Meta `PlatformApp` plus one account per `phone_number_id`.
- X: deployment-level OAuth Consumer App credentials, a tenant-shared `PlatformApp` webhook route, and one `PlatformAccount` per authorized user. Events route by `for_user_id`; legacy account-level webhook secrets remain readable during migration.

Each account owns tenant, brand, external ID, route ID, credential reference, capabilities, automation mode, config version, and lifecycle status.

## Shared reply behavior

All platforms use the same decision pipeline. Platform adapters only normalize inbound events; senders only translate explicit Outbox destinations.

Supported destination commands:

- `telegram_dm`
- `meta_messenger_dm`
- `meta_instagram_dm`
- `meta_public_comment`
- `meta_private_reply`
- `whatsapp_session_message`
- `x_dm`
- `x_chat_message`
- `x_post_reply`

Direct-platform drafts are never sent to customers. A `DRAFT` decision is retained in `reply_decisions` for future approval/inbox workflows and has no direct Outbox until approved. Chatwoot private notes remain supported for legacy Chatwoot-backed accounts.

At delivery time the system revalidates account status, tenant/account/conversation consistency, capability, target type, text length, expiration/window, and takeover state.

## Admin Web first slice

The built-in administration surface provides:

- bootstrap-superadmin and tenant-user login/logout with opaque HTTP-only cookies, server-side revocation, and CSRF checks;
- superadmin-only direct user creation at `/admin/users`, with no email/invitation flow;
- mandatory first-login password change for newly created tenant users;
- tenant-user self-service account authorization for the user's assigned tenant, including OAuth starts and tenant-scoped provisioning jobs;
- superadmin-only tenant-wide automation kill switch; tenant users retain account-level controls for their own accounts;
- PostgreSQL-backed runtime health summary on `/admin`, covering ingestion recovery, decision jobs, Outbox, provisioning, active X sync gaps, and disabled accounts with oldest backlog age;
- platform account and provisioning-job overview;
- Telegram, Facebook, Instagram, WhatsApp, and X connection forms;
- asynchronous job status and retry;
- account enable/disable and health checks, including separate Messaging/Comments status plus app-level and account-level subscription state; Meta accounts can only use `BOT_ACTIVE` when the deployment sets `META_AUTO_REPLY_ENABLED=true`, and every change is written to `audit_logs` as `SET_AUTOMATION_DEFAULT`; when `META_COMMENT_REPLY_ENABLED=true` too, newly authorized Facebook and Instagram accounts enable comments, start as `BOT_ACTIVE`, validate account-targeted comment permissions, and install the required App/account webhook fields;
- tenant-editable LLM persona at `/admin/prompt`, stored in PostgreSQL so the Worker sees saves immediately; only the persona is editable, while the output contract and prompt-injection defences are appended by code and rendered read-only. Saves bump a revision that is recorded in `reply_decisions.prompt_version` and audited as `SET_REPLY_PERSONA`, and a dry-run box exercises the model without persisting a decision or creating an Outbox row;
- no account-creation CLI requirement.

OAuth states contain the initiating server-side session ID and tenant. Callbacks revalidate that session and its current tenant permissions before exchanging credentials or creating a provisioning job, so logout, expiry, password change, or tenant revocation invalidates an in-flight authorization.

Production deployments should put `/admin` behind an identity-aware proxy or replace the local administrator login with OIDC/MFA. The service API and data-plane webhooks remain separate.

## OAuth evolution

Meta and X OAuth are adapters into the same `ProvisioningJob` boundary. Messenger/Instagram accounts route inbound evidence with `meta_health_status=PROVISIONING`, but send-time validation pauses customer delivery until the requested remote subscription reaches `READY`; a subscription failure disables the account. Facebook comment authorization requests `pages_read_engagement`, `pages_read_user_content`, and `pages_manage_engagement`; Facebook Login Instagram requests `pages_read_engagement` and `instagram_manage_comments`; standalone Instagram Login requests `instagram_business_manage_comments`. Provisioning rejects missing or wrong-target comment permissions with `META_COMMENT_PERMISSION_REQUIRED`. Periodic Scheduler checks repair subscription drift and expose permission loss as `REAUTH_REQUIRED`. OAuth transaction state is encrypted and short-lived in Redis. Exchanged platform credentials are encrypted with `PLATFORM_SECRET_KEYS` and staged in the durable job row, then stored as encrypted PostgreSQL bundles on the resulting `PlatformApp` / `PlatformAccount`; completed jobs clear the staging bundle. `ACCOUNT_SECRETS_ROOT` is retained only for legacy `file://` migration. Platform App Review, Business Verification, WhatsApp template approval, and X product-tier permissions remain external prerequisites and are surfaced as capabilities/manual steps rather than bypassed.
