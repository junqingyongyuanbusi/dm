# Multilingual runtime generation

> Runtime path: English knowledge source + detected customer language + LLM generation.
> Reviewed localization artifacts remain in the database and CLI for historical compatibility, but
> they are not required for runtime replies and are not a language allowlist.

## Configuration

The runtime path uses the existing retrieval and multilingual switches only:

```dotenv
KNOWLEDGE_RETRIEVAL_ENABLED=true
MULTILINGUAL_KNOWLEDGE_REPLY_ENABLED=true
```

There is no `MULTILINGUAL_SUPPORTED_LANGUAGES`, `MULTILINGUAL_LIVE_LOCALES`, experimental account
allowlist, or per-language auto-reply configuration.

The runtime always retrieves only published, verified-English knowledge for this path. The input
language is detected from the current message and recent customer history. Any language the detector
can identify proceeds to generation; `und`, ambiguous text, or an unsupported writing system falls
back to `HANDOFF`.

## Runtime flow

```text
customer message
  -> fail-closed language detection
  -> scoped English exact/dense/lexical retrieval
  -> answer-level confidence and margin
  -> low-confidence query translation fallback (protected entities)
  -> LLM generation in detected language
  -> deterministic language/fact/contact guards
  -> grounding verification against the English approved answer
  -> customer Outbox or HANDOFF
```

Only the query may be translated for retrieval. The answer is never translated from a machine
translation; customer-facing text is generated from the canonical English approved answer. Official
contact knowledge remains HANDOFF for the runtime generation path.

## Fail-closed boundaries

The decision becomes `HANDOFF` when language detection is unknown, retrieval fails, no strong answer
match exists after the optional query-translation retry, the selected knowledge is an official
contact, the generated language does not match the request, grounding fails, or the LLM/verifier is
unavailable. `BOT_DRAFT_ONLY` and private-note drafts remain available for human review.

## Historical reviewed artifacts

`src/social_reply/application/knowledge/localizations.py` and the localization CLI continue to
validate/publish/revoke reviewed text for existing records. Existing localization Outbox rows still
run their provenance and source-hash checks. New multilingual runtime decisions do not require a
localization artifact.
