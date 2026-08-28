# Configuration reference

Runtime application settings are defined by `social_reply.shared.config.Settings`. Environment
variables use the uppercase field name. Values are resolved in this order: explicit constructor
arguments, process environment, `.env` in the process current working directory, then code defaults.

Use `.env.example` for local development only. Production configuration is stored in the deployment
platform and validated by `scripts/validate_railway_config.py` during every release. API, Worker, and
Scheduler must use the same application settings unless a variable is explicitly deployment-role-only.

## Production image registry

GitHub Actions builds and verifies the production image once after Ruff and Pytest succeed. A
separate `publish-ghcr` job receives that exact image as an artifact and publishes the immutable
`ghcr.io/junqingyongyuanbusi/reply-core:<full-git-sha>` tag without rebuilding it. The job uses the
repository-scoped `GITHUB_TOKEN`; no Docker Hub credential is required. It compares the migration
graph with the application revision advertised by the current GHCR `latest`. When that graph
changed, the same job builds, pushes and smoke-tests
`railway-compat-pre-<short-sha>` from the predecessor digest plus the target migration graph. A
code-only release does not create a compatibility image.

The mutable `latest` tag is release-controlled and is never moved by ordinary CI. Only
`scripts/publish_railway_release.sh` may promote `latest`, after it has verified the CI-published SHA
image and any required CI-published compatibility image. The local release path performs registry
manifest inspection/retagging only: it does not build, pull, load, save, or run image layers.
Railway native image auto-update remains disabled. Production rollout still follows API →
`/healthz` → Worker → Scheduler and verifies all three roles run the same digest. The GHCR package
must be public before Railway is switched so the services can pull it without registry credentials.
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

## Ordinary-user Channels prerequisites

`/app/t/{tenant_id}/channels` does not introduce per-user OAuth application credentials. API,
Worker and Scheduler must share the existing deployment-level X and Meta/Instagram App settings:

- X self-authorization requires `X_API_KEY` and `X_API_SECRET` plus at least one enabled X message
  stack.
- Facebook Login requires `FACEBOOK_APP_ID`, `FACEBOOK_APP_SECRET` and `META_VERIFY_TOKEN`, or a
  compatible active Tenant Meta `PlatformApp` retained from an earlier deployment.
- Standalone Instagram Login requires `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET` and
  `INSTAGRAM_VERIFY_TOKEN` (or the documented Meta verify-token fallback).
- Telegram requires no shared App setting; the user supplies a BotFather Bot Token.
- Email self-authorization is rendered only when `EMAIL_ENABLED=true` and remains subject to
  `EMAIL_ALLOWED_HOSTS`, TLS and DNS public-target validation.
- WhatsApp and Feishu are administrator-managed in the first Channels release and do not expose
  ordinary-user credential forms.

OAuth callback URLs remain the existing `/admin/oauth/*/callback` protocol endpoints even when the
flow starts in Channels. The encrypted state controls the browser return surface; changing callback
paths is not required. All newly self-authorized accounts remain `BOT_DRAFT_ONLY`.

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
tokens must be reauthorized from `/admin/integrations/accounts`; missing or wrong-Page permissions produce
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
`FEISHU_HANDOFF_NOTIFICATIONS_ENABLED=false` until `/admin/integrations/feishu/handoff` has a validated support
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
| `PROMPT_VERSION` | `v2-editable-business-prompt` | Persisted identifier for the code-owned immutable reply contract; an active editable business Prompt appends `#bpN` and also records its version ID/hash separately |
| `REPLY_BUSINESS_PROMPT_ENABLED` | `false` | Enables the versioned Tenant + Brand business Prompt for primary reply generation. Must be explicit and identical on API, Worker and Scheduler; auxiliary Prompt calls never receive it |
| `OPENAI_API_KEY` | empty | Required outside tests when provider is `openai` |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible API base |
| `OPENAI_MODEL` | `gpt-4o-mini` | Chat completion model |
| `OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small` | Requested knowledge embedding model/version; real OpenRouter acceptance is not proven by configuration alone |
| `OPENAI_EMBEDDING_DIMENSIONS` | `1536` | Must match a vector column that exists on `knowledge_chunks` (`1536` or `1024`); startup rejects any other value |
| `OPENAI_TIMEOUT_SECONDS` | `30` | HTTP timeout for generation calls |
| `OPENAI_GROUNDING_MODEL` | empty | Optional separate model for semantic fidelity verification; empty uses `OPENAI_MODEL` |
| `GROUNDING_VERIFIER_TIMEOUT_SECONDS` | `8` | Short fail-closed timeout for the second-pass grounding verifier |
| `KNOWLEDGE_RETRIEVAL_ENABLED` | `false` | Enables knowledge retrieval |
| `KNOWLEDGE_MIN_SIMILARITY` | `0.5` | Minimum retrieval score |
| `KNOWLEDGE_TOP_K` | `3` | Maximum retrieved chunks |
| `KNOWLEDGE_AUTO_REPLY_MIN_SIMILARITY` | `0.8` | Answer-level strong-match gate: top1 similarity floor for auto reply |
| `KNOWLEDGE_AUTO_REPLY_MIN_MARGIN` | `0.08` | Answer-level strong-match gate: minimum top1-top2 similarity gap. Not transferable across embedding models — see the model-switch section |
| `KNOWLEDGE_VERBATIM_REPLY` | `false` | Return matched template text without LLM rewriting |
| `REQUIRE_KNOWLEDGE` | `false` | Legacy path: handoff without calling LLM when retrieval has no match |
| `MULTILINGUAL_KNOWLEDGE_REPLY_ENABLED` | `false` | Enables English-corpus multilingual runtime generation; non-English requests use the detected language, with no language or account allowlist; requires knowledge retrieval |
| `KNOWLEDGE_MATCH_ONLY_REPLY_ENABLED` | `false` | Temporary coordinated test mode. A unique exact match, or answer-level similarity `>= 0.80` with margin `>= 0.08`, decides whether to reply. The model only generates text and the content, grounding, source-currentness, contact, and language Guards are bypassed. Requires knowledge retrieval and multilingual generation, and is incompatible with `RAG_SELECTOR_MODE=live`. API, Worker, and Scheduler must set the same explicit value. |
| `KNOWLEDGE_LOCALIZATION_ENABLED` | `false` | Prefer human-reviewed localized text over runtime generation; requires `MULTILINGUAL_KNOWLEDGE_REPLY_ENABLED=true` and a non-empty live-locale list |
| `KNOWLEDGE_LOCALIZATION_LIVE_LOCALES` | empty | Comma-separated send allowlist for reviewed localizations; published locales outside it still fall back to runtime generation |
| `MULTILINGUAL_LANGUAGE_POLICY` | `review` | `review` preserves uncertain/wrong-language output as a private DRAFT after every hard guard passes; `legacy_hard` remains available as a rollback mode that converts it to HANDOFF |
| `RAG_SELECTOR_MODE` | `off` | `off`, `shadow`, or `live`; controls sampled non-exact candidate selection independently from language policy |
| `RAG_SELECTOR_CANARY_BPS` | `0` | Stable selector sample in basis points, `0..10000`; the bucket is derived from tenant and conversation identity |
| `CONVERSATION_HISTORY_LIMIT` | `20` | Prior messages sent to decision context; range 0-50 |
| `CONVERSATION_HISTORY_MAX_CHARS` | `12000` | Total history character budget; range 0-50000 |

`KNOWLEDGE_MATCH_ONLY_REPLY_ENABLED` is intentionally a reversible test switch, not the default
reply policy. Existing risk rules, LLM action contracts, output Guards, Grounding verification,
localization checks, and send-time source checks remain in the code and continue to run when the
switch is `false`. When the switch is `true`, only system-delivery correctness remains enforced:
tenant/account/conversation scope, Kill Switch, automation takeover, generation fencing, Prompt
currentness, payload/target binding, idempotency, platform capability, non-empty text, and platform
length. Reply language is requested through the generation Prompt and is not verified after
generation. Roll back by setting the switch to `false` on all three roles before redeploying them.

Stage the first editable Prompt release with `REPLY_BUSINESS_PROMPT_ENABLED=false` on all roles.
After the target digest and schema are healthy, use
`scripts/set_reply_business_prompt_gate.sh --enable`; use the same script with `--disable` for a
coordinated runtime rollback. The script validates a single stored value and forces every role to
restart on the same current digest. Direct one-service gate edits are not a supported rollout path.

### Language resolution

The runtime replies in whatever language it resolves for the customer message, with no language
allowlist anywhere in the code. Resolution is a three-stage cascade:

1. **Deterministic detection** (`domain/reply/language.py`) — writing-system rules plus Lingua.
   Pure and synchronous. Its result selects the prompt language and informs translation/retrieval
   routing, knowledge metadata, localization checks, and post-generation observation. It is not an
   Outbox safety assertion.
2. **LLM fallback** (`application/reply_decision/language_resolution.py`) — consulted when the
   deterministic result is `und`, or when it lands on a known-confusable sibling pair. Lingua only
   ever chooses between Hindi and Marathi on Devanagari text and returns confident wrong answers on
   short input, and confidence cannot separate those errors: a misdetected `नमस्ते` scored 0.624
   while correctly detected Russian scored 0.383. The trigger is therefore the candidate set, not a
   confidence threshold. Messages with no meaningful letters (emoji, digits, bare links) never reach
   the model.
3. **Unresolved:** still `und`. `legacy_hard` hands off with `UNKNOWN_LANGUAGE`; `review` may
   retrieve and generate for human review, but the result is always a private DRAFT and is never
   eligible for automatic public delivery.

The resolved provenance is stored in `reply_decisions.request_language_source` as
`current_message`, `recent_user_history` or `llm_fallback`.

There is no configuration for the fallback. It reuses the existing `OPENAI_*` settings and the
grounding timeout, and switches itself off when the client cannot detect languages — the same
convention as `translate_to_english`.

`MULTILINGUAL_LANGUAGE_POLICY` changes only how language-identity uncertainty is routed:

- `legacy_hard` preserves the historical HANDOFF for an unresolved request language, a detected
  output-language mismatch, or a script mismatch;
- `review` treats language identity as advisory after hard safety and grounding checks. It preserves
  a language-identity mismatch only as `DRAFT` with private visibility so Admin can review it;
  a writing-system conflict remains a hard HANDOFF.

The policy never relaxes deterministic facts, knowledge provenance, protected entities, official
contact authorization, numbers, currencies, semantic grounding, tenant/account scope, the kill
switch, idempotency, or Outbox preflight. A failure in any of those layers still discards the
candidate and fails closed.

### Reply-language observation

After deterministic hard checks and grounding, the runtime resolves the generated reply language
with a small cascade: deterministic detection first, then one structured LLM language-classification
call only when local detection is uncertain. Generated replies never inherit a language from
conversation history. Approved product names and protected entities are removed from the text being
classified because they carry no useful language evidence.

The resolved primary language must match the prompt target; Chinese script variants remain distinct
when both sides provide one. Under the default `review` policy, a mismatch or unresolved reply is
preserved only as a private DRAFT. `legacy_hard` remains a rollback mode that converts the same signal
to HANDOFF. Language observation runs only after hard fact, number/currency, protected-entity,
contact/PII and semantic-grounding checks, so it cannot soften those failures.

Outbox does not call a language model again. Public bot-derived delivery remains fail-closed on
grounding and deterministic knowledge provenance in addition to its normal scope, payload binding,
account, conversation, kill-switch and idempotency preflight.

Allowed writing systems are the per-language table **union** the customer message's dominant script.
The table alone cannot keep up — it lists `ru`/`uk`/`bg` but omits Macedonian, Serbian, Belarusian,
Kazakh and Mongolian, all of which the detector identifies correctly and the guard used to reject.
The union only widens: Japanese mixes kana and Han, so replacing the table with the customer's
dominant script would wrongly reject Han characters in the reply.

Numbers, currencies and percentages are always compared strictly against the approved English
answer. Time units are compared only when the target language's unit words are recognised —
`de`, `it`, `vi`, `tr`, `nl`, `pl` and `sv` are not in the pattern table, so those replies are
tagged `FACT_UNIT_UNVERIFIED` and left to the grounding verifier rather than being mistaken for
tampering. A recognised but *different* unit is still a mismatch and is blocked.

### Conversation history

History fed to the model keeps only turns that formed a question-answer pair. Customer messages
that were never answered have already gone to a human and are not the bot's context; leaving them
in makes the model adopt the stale intent — a real conversation where one unanswered licence
question preceded a plain greeting produced handoff 4/4, and 4/4 auto-reply once filtered.

The filter applies only to model context. Language resolution still sees the full history, because
unanswered customer messages remain valid evidence of the customer's language.

Multilingual runtime generation requires `KNOWLEDGE_RETRIEVAL_ENABLED=true` and
`MULTILINGUAL_KNOWLEDGE_REPLY_ENABLED=true`. It retrieves verified-English knowledge in code and
relies on language observation, hard fact/entity/contact checks, grounding, kill switch, and Outbox
guards. Configuration validation does not prove retrieval calibration or authorize a live selector
rollout.

### Non-English retrieval: translation comes first

The English corpus is the only source of truth, so a non-English query is translated to English
*before* retrieval, not as a fallback after a weak score. Translation failure silently falls back to
native-query retrieval, so the path is an enhancement and never a dependency.

Translating first restores two arms that are dead for non-English text:

- **Exact question match.** A non-English query can never equal an English question, so non-English
  customers previously could not reach the cheapest and most trustworthy arm at all.
- **Lexical (`tsvector`) retrieval.** The question index uses the `simple` text-search
  configuration, which does not segment Chinese, Japanese, Korean, or Thai, so the lexical arm
  returned nothing and RRF degraded to pure vector search.

Ordering matters for correctness, not just recall. Treating translation as a
"retrieve, and retry only if not strong" fallback lets a *confident but wrong* cross-lingual match
skip translation entirely, because a wrong top1 can still clear the strong gate.

### Candidate selector and canary semantics

The selector deduplicates the hybrid/vector candidate union by approved-answer identity and receives
at most three candidates. Its rollout is deterministic per conversation:

- bucket = the first eight bytes of SHA-256 over `tenant_id:conversation_key`, modulo 10,000;
- `off`: the selector is not invoked and the legacy top-1 assessment controls the answer;
- `shadow`: only buckets below `RAG_SELECTOR_CANARY_BPS` invoke the selector and record its proposed
  candidate/evidence; the legacy answer still controls the decision;
- `live`: only sampled buckets let the selector control non-exact selection; outside the sample the
  legacy answer remains in control;
- exact question matches bypass selector control in every mode;
- failure, invalid output, or abstention in sampled `live` traffic fails closed.

The selector returns only a candidate ID from the supplied allowlist. It does not generate customer
text; both legacy and selected candidates use the same reviewed-localization or canonical generation
and grounding path.

`RAG_SELECTOR_MODE` and `MULTILINGUAL_LANGUAGE_POLICY` are separate axes. The required rollout is
`legacy_hard/off/0` baseline, bounded `shadow`, evidence and draft-queue review, then at most a small
`review` and/or `live` canary with gradual basis-point increases. Restore `legacy_hard/off/0` on API,
Worker, and Scheduler to roll behavior back.

Each evaluated decision stores `decision_release_sha`, `retrieval_policy_version`,
`selector_version`, and bounded `rag_evidence`: candidate ranks, similarities and hashes, canary
bucket, selection method, latency, and guard/verifier results. The evidence object must not contain a
query, message, prompt, question, answer, reply, contact, or other body text. Knowledge
`protected_values` are exact entity strings that translation and generation must preserve. Their
normalized set is part of the knowledge revision hash and approved-answer identity, so a policy-only
change is imported as a new revision and conflicting policies are never deduplicated together.

### Retrieval backend choice

PostgreSQL remains the business fact source and the current retrieval backend, using pgvector plus
PostgreSQL full-text search. The corpus is approximately 716 verified published English documents,
so an exact vector scan must be benchmarked against HNSW. pgvector documents that approximate-index
filters are applied after ANN scanning: SQL scope filters still prevent cross-tenant rows from being
returned, but a shared multi-tenant HNSW index can lose in-scope Recall@k. If HNSW is retained, verify
the deployed extension supports `SET LOCAL hnsw.iterative_scan = strict_order` and test recall under
tenant, brand, and platform filters.

Qdrant is not a second durable store. It is only a future rebuildable projection if measured scale or
dense+sparse retrieval needs justify the extra service. Haystack, LlamaIndex, and RAGFlow do not
replace this application's tenant scope, durable jobs, safety policy, human review, or Outbox, so they
are experiment tooling rather than the runtime architecture. See
[multilingual-oss-research.md](multilingual-oss-research.md) for primary-source evidence and the
candidate matrix.

### Switching the embedding model

`knowledge_chunks` holds one vector column per supported dimension (`embedding` for 1536,
`embedding_1024` for 1024), and **each vector column has its own version column**
(`embedding_version`, `embedding_1024_version`). Both retrieval and writes pick the pair from the
vector's actual length. A shared version column would break the whole point of two columns: the
moment a backfill rewrote it, the live model's version filter would match nothing.

1. `alembic upgrade head` — create the target dimension's vector and version columns.
2. `uv run python -m apps.cli.reembed_knowledge --tenant <id>` — backfill. Only the target
   dimension's two columns are written, so live retrieval keeps serving throughout. Use
   `--dry-run` first to see the pending row count.
3. Set `OPENAI_EMBEDDING_MODEL` and `OPENAI_EMBEDDING_DIMENSIONS`, then restart.

Rollback is step 3 in reverse; the previous vectors and their version are still present, so no
backfill is needed.

**Gate thresholds do not transfer between models.** Measured on 240 translated queries across six
languages against the production corpus (716 verified-English documents), at the zero-wrong-answer
optimum:

| Embedding model | `MIN_SIMILARITY` | `MIN_MARGIN` | Auto-reply coverage | Wrong answers sent |
| --- | --- | --- | --- | --- |
| `text-embedding-3-small` (1536) | 0.55 | 0.08 | 159/240 | 0 |
| `baai/bge-m3` (1024) | 0.55 | 0.05 | 207/240 | 0 |

BGE-M3 aligns cross-lingual text far better (mean top1 similarity 0.66 -> 0.84), but it also
compresses the whole similarity space, so its margins are smaller in absolute terms. Reusing the
1536-model margin would over-handoff; reusing a loose margin sends wrong answers. Re-sweep the two
gates whenever the embedding model changes.

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
| `SERVICE_ROLE` | entrypoint | Required; must be explicitly set to `api`, `worker`, or `scheduler`. Missing/unknown values fail before startup |
| `PORT` | entrypoint/API | API listen port, default 8000 |
| `DRAMATIQ_PROCESSES` | entrypoint/Worker | Worker process count, default 4, range 1-32; never inferred from host CPU count |
| `DRAMATIQ_THREADS` | entrypoint/Worker | Threads per process, default 8, range 1-32; processes × threads must not exceed 128 |
| `DRAMATIQ_WORKER_TIMEOUT_MS` | entrypoint/Worker | Redis empty-queue polling backoff cap, default 250ms, range 50-5000ms; lower values reduce low-volume pickup latency but increase idle Redis fetches |

Railway injects `DATABASE_URL`, `REDIS_URL`, `SERVICE_ROLE`, and `PORT` into containers. Do not add
role-specific copies of feature flags; divergent values can accept work that another role will not
process or recover. Deploy API, Worker, Scheduler, PostgreSQL, and Redis in one infrastructure region.
Cross-region Worker database and broker round trips multiply across each durable reply stage and can
turn a two-second direct reply into tens of seconds without producing retries or errors.

## Template policy

- `.env.example`: executable single-process smoke profile with `TESTING=true`, inline actor
  fallbacks, StubBroker, stub LLM, knowledge retrieval disabled, localhost callbacks and public
  development-only secrets. It does not validate the production Redis/Dramatiq boundary.
- Production: Railway service variables are the source of truth. Every release validates required
  secrets, feature gates, role assignment, Pydantic production settings, and cross-role consistency
  before building or deploying an image.
- Repository test configuration: always points at a database whose name ends in `_test`; pytest
  refuses to collect against any other database.
