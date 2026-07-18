# Platform Account Control Plane

## Decision

Platform accounts are provisioned through a Web administration surface and a durable Provisioning API. Account creation CLIs are not part of the production workflow.

The system is split into two independently deployable concerns:

- **Control Plane**: administrator authentication, credential intake, account provisioning, webhook setup, capability checks, health, enable/disable, retry, and audit.
- **Data Plane**: signed platform webhooks, `CanonicalEvent`, durable decision jobs, the shared Rules/RAG/LLM/Final Guard pipeline, transactional Outbox, and account-scoped senders.

A Control Plane outage must not stop already-connected accounts from receiving or sending messages.

## Trust boundaries

1. Browser administrators authenticate with a dedicated signed, HTTP-only session cookie. The browser never receives `CONTROL_API_KEY`.
2. `CONTROL_API_KEY` remains a server-to-server credential for automation and future external admin services.
3. Platform credentials are accepted only over TLS and stored as application-encrypted Fernet envelopes in PostgreSQL. Encryption keys remain outside PostgreSQL in `PLATFORM_SECRET_KEYS`; job request/result JSON never contains credentials.
4. Every mutation is tenant-scoped and audited with the human or service actor.
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
- Facebook/Instagram: one Meta `PlatformApp`, multiple Page/Professional Account rows.
- WhatsApp: a Meta `PlatformApp` plus one account per `phone_number_id`.
- X: one direct `PlatformAccount`, account-level webhook/subscription.

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
- `x_post_reply`

Direct-platform drafts are never sent to customers. A `DRAFT` decision is retained in `reply_decisions` for future approval/inbox workflows and has no direct Outbox until approved. Chatwoot private notes remain supported for legacy Chatwoot-backed accounts.

At delivery time the system revalidates account status, tenant/account/conversation consistency, capability, target type, text length, expiration/window, and takeover state.

## Admin Web first slice

The built-in administration surface provides:

- login/logout with a signed HTTP-only cookie and CSRF checks;
- platform account and provisioning-job overview;
- Telegram, Facebook, Instagram, WhatsApp, and X connection forms;
- asynchronous job status and retry;
- account enable/disable and health checks;
- no account-creation CLI requirement.

Production deployments should put `/admin` behind an identity-aware proxy or replace the local administrator login with OIDC/MFA. The service API and data-plane webhooks remain separate.

## OAuth evolution

Meta Embedded Signup/OAuth and X OAuth are adapters into the same ProvisioningJob boundary. OAuth callbacks must stage exchanged tokens in `SecretStore`, store only grant metadata in PostgreSQL, and submit the same platform provisioning operations. Platform App Review, Business Verification, WhatsApp template approval, and X product-tier permissions remain external prerequisites and are surfaced as capabilities/manual steps rather than bypassed.
