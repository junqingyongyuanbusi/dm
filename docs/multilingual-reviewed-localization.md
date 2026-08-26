# Multilingual English-corpus replies

> Runtime path: English knowledge source + detected customer language + LLM generation.
> Reviewed localization artifacts remain the higher-trust wording path when enabled, but runtime
> generation does not require an artifact and does not use one as a language allowlist.

## Configuration

The runtime path uses the retrieval and multilingual switches. Language and selector rollout remain
fail-closed by default:

```dotenv
KNOWLEDGE_RETRIEVAL_ENABLED=true
MULTILINGUAL_KNOWLEDGE_REPLY_ENABLED=true
MULTILINGUAL_LANGUAGE_POLICY=legacy_hard
RAG_SELECTOR_MODE=off
RAG_SELECTOR_CANARY_BPS=0
```

There is no `MULTILINGUAL_SUPPORTED_LANGUAGES`, `MULTILINGUAL_LIVE_LOCALES`, experimental account
allowlist, or per-language auto-reply configuration.

The runtime always retrieves only published, verified-English knowledge for this path. It resolves
the input language from the current message and recent customer history. A reliable language may
proceed to generation. `und` or ambiguous text becomes HANDOFF under `legacy_hard`; under `review`,
an otherwise safe candidate can only become a private DRAFT.

## Runtime flow

```text
customer message
  -> language resolution
  -> reliably non-English query translated to English with protected values
  -> scoped English exact/dense/lexical retrieval
  -> answer-level confidence and margin
  -> optional sampled selector for non-exact candidates
  -> reviewed localization when available, otherwise grounded generation
  -> deterministic fact/provenance/entity/contact/number/currency guards
  -> grounding verification against the English approved answer
  -> language observation policy
  -> customer Outbox, private DRAFT, or HANDOFF
```

Only the query may be translated for retrieval. The answer is never translated from a machine
translation; customer-facing text is generated from the canonical English approved answer. Official
contact knowledge uses exact English wording or an explicitly authorized reviewed localization;
non-English runtime generation for official contacts remains HANDOFF.

Exact question matches bypass the selector. `shadow` records a proposal without changing the legacy
answer. `live` controls non-exact choice only in stable sampled buckets; selector failure or
abstention in sampled live traffic fails closed. The selector returns only an allowlisted candidate
ID; reply generation and grounding stay on the shared canonical path in every mode.

## Fail-closed boundaries

The decision becomes `HANDOFF` when retrieval fails, no strong answer exists, sampled live selection
abstains, official-contact authorization is missing, a deterministic fact or protected entity drifts,
a writing-system conflict is observed, grounding fails, or a required provider is unavailable.
`legacy_hard` also hands off on unresolved or wrong language. `review` changes only that language-
identity signal: after every hard check passes, it stores the candidate as a private DRAFT and never
sends it automatically.

## Reviewed localization artifacts

When `KNOWLEDGE_LOCALIZATION_ENABLED=true`, the runner prefers reviewed text if the selected document
has a published artifact in a live locale and its source/content hashes still match. Protected values
and official-contact authorization remain part of its send-time provenance checks. Missing or stale
artifacts fall back to grounded runtime generation where policy allows; they are not a global language
allowlist.
