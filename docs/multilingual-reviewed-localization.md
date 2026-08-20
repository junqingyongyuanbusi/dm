# Reviewed multilingual knowledge replies

> Runtime status: PostgreSQL/Fake wiring and safety path. Production live remains gated by reviewed calibration/E2E reports, a pinned localization release, rotated provider credentials, and a canary. This path does not claim that OpenRouter cross-language retrieval quality has been validated.

## What the first slice supports

The English knowledge base remains canonical. For non-English input, automatic reply is allowed only when all of the following are true:

1. language detection is known and supported;
2. scoped English retrieval has a strong, unambiguous match;
3. the pinned `KNOWLEDGE_LOCALIZATION_RELEASE` contains a published, reviewed artifact for the resolved locale;
4. the artifact remains bound to the current English content hash;
5. deterministic language, hash, protected-value, contact, and delivery preflight checks pass.

Otherwise the decision is `HANDOFF`. Detecting `ja` does not infer country, region, or honorific level. A neutral reviewed `ja` artifact is selected. Pure-Han short text such as `返金希望` remains ambiguous without reliable context and is handed off.

English requests continue to use the canonical approved English reply; enabling a Japanese release does not require duplicate English localization artifacts.

## Configuration

The three runtime roles must receive identical values:

```dotenv
KNOWLEDGE_RETRIEVAL_ENABLED=true
ENGLISH_KNOWLEDGE_ONLY_ENABLED=true
MULTILINGUAL_KNOWLEDGE_REPLY_ENABLED=false
MULTILINGUAL_KNOWLEDGE_SHADOW_ENABLED=true
MULTILINGUAL_LIVE_LOCALES=ja
KNOWLEDGE_LOCALIZATION_RELEASE=ja-release-v1
OPENAI_EMBEDDING_DIMENSIONS=1536
```

`MULTILINGUAL_SUPPORTED_LANGUAGES` is the detectable input set. It is not the automatic-send allowlist. `MULTILINGUAL_LIVE_LOCALES` is the reviewed locale allowlist for the pinned release.

Keep live disabled while collecting retrieval and E2E evidence. Production Settings reject blank or `unversioned` localization releases.

## Prepare the English source mapping

Export published, verified-English knowledge IDs and hashes:

```bash
uv run python -m apps.cli.knowledge_localizations export-sources \
  --tenant default \
  --output dist/knowledge-localization-sources.csv
```

Use the exported `document_id`, scope, question, approved English answer, and content hash during translation review. Do not guess UUIDs.

## Import reviewed localization drafts

CSV columns:

```csv
document_id,locale,text,protected_values_json
00000000-0000-0000-0000-000000000000,ja,返金には通常3から5営業日かかります.,[]
```

A sample is available at `tests/fixtures/knowledge-localizations-ja.csv`; replace its placeholder document ID with an exported real ID.

Import drafts into a release:

```bash
uv run python -m apps.cli.knowledge_localizations import \
  --tenant default \
  --release ja-release-v1 \
  --input dist/knowledge-localizations-ja.csv \
  --actor localization-import
```

Draft import never authorizes automatic sending. It validates:

- source is published, verified English;
- source and localization numeric/time facts agree;
- source contact values are neither added, removed, nor changed;
- automatically protected entities and operator-provided protected values remain present.

## Review and publish

List artifacts:

```bash
uv run python -m apps.cli.knowledge_localizations list --tenant default
```

Publish one reviewed artifact and explicitly authorize automatic reply:

```bash
uv run python -m apps.cli.knowledge_localizations publish \
  --tenant default \
  --id <artifact-uuid> \
  --reviewer reviewer@example.com \
  --approve-auto-reply
```

For a source explicitly classified as official contact, the reviewer must additionally pass:

```bash
--approve-official-contact
```

The official-contact flag cannot authorize a new URL, email, phone number, handle, or service number: source and localized contact multisets must already match.

## Revoke and rollback

Revoke an artifact without deleting audit history:

```bash
uv run python -m apps.cli.knowledge_localizations revoke \
  --tenant default \
  --id <artifact-uuid> \
  --actor reviewer@example.com \
  --reason "source policy changed"
```

Pending bot Outbox rows are rechecked before sending. A revoked/missing/stale artifact cancels the bot send and moves the conversation to `HANDOFF_PENDING` with a human work item and notification intent.

Unpublishing the English source transactionally revokes its active localizations. Knowledge documents with localization history are immutable and cannot be physically deleted through Admin.

Rollback order:

1. set the applicable kill switch or move accounts to `BOT_DRAFT_ONLY`;
2. disable multilingual live on API/Worker/Scheduler together;
3. revoke the affected artifact/release entries;
4. verify pending bot Outbox rows are cancelled or handed off;
5. select a previously reviewed release only after its reports and hashes are restored.

Changing only `OPENAI_EMBEDDING_MODEL` or running the in-place re-embedding script is not a safe rollback or model migration.

## Retrieval calibration

Retrieval shadow does not send reviewed localizations. It only calibrates English-corpus retrieval for the target language buckets.

```bash
uv run python -m apps.cli.multilingual_shadow_eval export \
  --output dist/multilingual-shadow-review.csv

uv run python -m apps.cli.multilingual_shadow_eval evaluate \
  --input dist/multilingual-shadow-reviewed.csv \
  --output dist/multilingual-calibration.json
```

The report binds corpus, embedding, gate, runtime contract, renderer version, and pinned localization release. Its language buckets are derived from `MULTILINGUAL_LIVE_LOCALES`, not every detectable language.

## Reviewed-localization E2E holdout

Export actual v2 decisions from an isolated evaluation database/test tenant. Do not use an unapproved real tenant or production customer sends to bootstrap this gate.

```bash
uv run python -m apps.cli.multilingual_e2e_eval export \
  --output dist/multilingual-e2e-review.csv

uv run python -m apps.cli.multilingual_e2e_eval evaluate \
  --input dist/multilingual-e2e-reviewed.csv \
  --calibration dist/multilingual-calibration.json \
  --output dist/multilingual-e2e-calibration.json
```

The E2E report is cryptographically tied to the calibration file and requires matching runtime versions, thresholds, release, renderer, locales, knowledge evidence, language outcome, localization provenance, and Outbox result. Human reviewers must preserve the exported decision set and review positive, negative, ambiguous, and risk samples.

Production variables additionally require the SHA-256 of both reports. Do not create a pass report manually and do not use `TESTING=true` to bypass production gates.

## Shadow, canary, and metrics

Roll out per tenant/brand/platform/account and locale:

1. retrieval shadow only;
2. isolated E2E evaluation and human review;
3. `BOT_DRAFT_ONLY` canary;
4. internal/test accounts with bot send;
5. small customer canary;
6. gradual promotion after signed review.

Measure by language, tenant, and scope:

- Candidate Recall@1/3/5/20, MRR, nDCG;
- wrong policy and wrong scope;
- false `AUTO_REPLY`;
- wrong-language Outbox;
- HANDOFF rate and safe automation coverage;
- p50/p95 latency, timeout rate, and variable cost.

## Current limitations

- The checked-in Fake/DB tests prove wiring and fail-closed behavior, not OpenRouter retrieval quality.
- The previously exposed OpenRouter key must not be reused. Real endpoint validation requires a rotated key.
- `text-embedding-3-small` is only the model ID the local code attempts to send to OpenRouter; provider acceptance and returned 1536 dimensions are not yet verified.
- Runtime translation fallback is not implemented and remains a future, default-off candidate.
- OpenSearch, Qwen3, BGE-M3, and rerankers remain bake-off candidates; see `docs/multilingual-oss-research.md`.
- Automatic reply capability is limited to locales with published artifacts in the pinned reviewed release. Missing locales hand off.
