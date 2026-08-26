# Multilingual reply OSS research

> Status: PostgreSQL is the current backend; external retrieval/model candidates remain unselected.
> Accessed: 2026-08-25.
>
> This document informs an offline bake-off and staged architecture. It does not authorize multilingual live mode. Production enablement still requires the repository's calibration, reviewed end-to-end holdout, version/hash consistency, and human approval gates.

## Decision summary

For this repository, the safest near-term path is **not** to replace the application with a RAG
framework or immediately migrate to a separate vector service. Keep PostgreSQL + pgvector +
PostgreSQL full-text search, compare exact vector scan with filtered HNSW Recall@k, and roll out the
selector and language-review policy independently:

```text
inbox message
  -> scoped language resolution
  -> protected English query translation when reliable and non-English
  -> scoped PostgreSQL exact/dense/lexical retrieval
  -> legacy similarity/margin gate or sampled selector with a similarity floor
  -> approved localization or grounded generation
  -> deterministic fact/provenance/entity/contact guards
  -> semantic verifier, then language observation
  -> Outbox, private review DRAFT, or HANDOFF
```

OpenSearch, Qdrant, Qwen3, BGE-M3, alternative query-translation strategies, and reranking should
remain versioned candidates until representative DM data proves a quality, latency, cost, and
operational advantage.

## Candidate matrix

| Candidate | License evidence | Deployment shape | Strengths | Risks / fit for this repository |
| --- | --- | --- | --- | --- |
| PostgreSQL FTS + pgvector | PostgreSQL and pgvector use permissive open-source licenses | Existing durable database; exact scan or HNSW/IVFFlat index | Keeps tenant/publish/version filters and retrieval evidence in the current transaction boundary; exact scan is practical at the current corpus size | Approximate-index filtering occurs after ANN scanning and can reduce in-scope recall; benchmark exact scan and iterative HNSW under real filters |
| Qdrant | Apache-2.0 repository | Separate Rust vector service with payload indexes and dense/sparse/hybrid query support | Purpose-built filtered vector retrieval and scalable rebuildable collections | Adds projection, synchronization and recovery operations; cannot become the durable source for tenant, publication or policy state |
| OpenSearch + k-NN + Neural Search | Apache-2.0 repositories | Separate JVM cluster; optional ML Commons/model-serving nodes; versioned mappings and search pipelines | Mature lexical/vector/hybrid search, ANN pre-filtering, scalable projection | Adds another distributed system; filter behavior is version/query-shape sensitive; must remain a rebuildable projection of PostgreSQL |
| Qwen3-Embedding / Qwen3-Reranker | Qwen model-card YAML publishes `apache-2.0`; repository license discoverability has an open concern | Transformers, Sentence Transformers, vLLM, or TEI; GPU strongly preferred for 4B/8B | Multilingual, instruction-aware, MRL/custom dimensions; separate reranker family | 8B embedding outputs up to 4096 dimensions, incompatible with current `Vector(1536)`; serving needs explicit truncation/batching/memory limits |
| FlagEmbedding / BGE-M3 | MIT repository | Python/PyTorch or separate serving layer | 100+ languages; dense, sparse, and multi-vector outputs; 1024 dense dimensions | Toolkit, not a production service; sparse/multi-vector modes require new storage and retrieval contracts |
| Haystack | Apache-2.0 repository | Python orchestration plus separate integrations/services | Explicit pipeline components; OpenSearch integration available | The application already owns durable jobs, safety gates, Outbox, tenant scope, and recovery; adding Haystack to production would duplicate orchestration |
| RAGFlow | Apache-2.0 repository | Full Dockerized RAG platform with multiple services | Broad ingestion/retrieval UI and hybrid RAG features | Too large as an in-process dependency; would become a separate platform and still would not replace this repository's send/authorization contracts |
| LlamaIndex | MIT repository | Python framework with modular integrations | Useful for isolated retrieval experiments | Adds abstraction but not the repository's policy, scope, localization, Outbox, or fail-closed semantics |
| Lingua | Apache-2.0 repository | In-process offline language detector | Strong short-text orientation and no network dependency | Proper names and short/mixed text still produce false classifications; keep uncertainty and HANDOFF semantics |
| fastText language ID | Code repository license and downloadable model license differ; verify both before use | In-process native model | 176-language model and fast inference | Model artifact has separate licensing obligations; a second detector is not automatically safer |
| Argos Translate | MIT repository | Local Python/OpenNMT models; optional acceleration | Offline translation and no provider data transfer | Translation quality/model licenses vary; pivot translation can compound errors; unsuitable as an unreviewed official-policy source |
| NLLB-200 distilled 600M | Model card: CC-BY-NC | Local Transformers | Broad research language coverage | Model card says research-only and not released for production deployment; not a commercial-production default |

## Verified failure evidence

### pgvector filtering and exact-search baseline

- [pgvector filtering documentation](https://github.com/pgvector/pgvector#filtering) states that with
  approximate indexes filtering is applied after the index is scanned. It recommends increasing
  `hnsw.ef_search`, enabling iterative scans in 0.8.0+, partial indexes, or partitioning depending on
  filter selectivity.
- The same official documentation exposes
  `SET LOCAL hnsw.iterative_scan = strict_order`, which scans farther while preserving strict result
  ordering. Extension version/support must be verified in the deployed image rather than assumed.

Implication: SQL predicates still prevent an out-of-scope row from being returned, but a shared
multi-tenant ANN scan can fail to surface enough eligible rows. At approximately 716 verified
published English documents, exact vector scan is a required candidate. Compare it with HNSW using
tenant, brand, platform, publication, language, and embedding-version filters and report Recall@k,
latency, and plans from the actual production extension version.

### Qdrant as an optional projection

- [Qdrant filtering documentation](https://qdrant.tech/documentation/concepts/filtering/) documents
  payload filters and payload indexes for constrained vector queries.
- [Qdrant hybrid-query documentation](https://qdrant.tech/documentation/concepts/hybrid-queries/)
  documents multi-stage dense, sparse, and fusion queries.
- [The Qdrant repository](https://github.com/qdrant/qdrant) publishes its Apache-2.0 implementation.

Implication: Qdrant becomes relevant only if measured corpus growth, filtered ANN latency, or
dense+sparse retrieval quality justifies operating a second service. Publish/revoke/version changes
must project from PostgreSQL through durable work, and the collection must remain disposable and
rebuildable.

### OpenSearch filtering and hybrid search

- [OpenSearch hybrid pre-filtering documentation](https://docs.opensearch.org/latest/vector-search/ai-search/hybrid-search/pre-filtering/) documents a top-level `hybrid.filter` that applies a common filter to hybrid subqueries.
- [Neural Search issue #1759](https://github.com/opensearch-project/neural-search/issues/1759) reports that, in an OpenSearch 3.x hybrid query containing a nested neural/k-NN query, the common filter was not pushed into the ANN engine. Global top-k candidates were selected first, then filtered, so an in-scope document could disappear. The reported workaround duplicates the filter inside the neural clause.
- [Neural Search issue #1705](https://github.com/opensearch-project/neural-search/issues/1705) reports a hybrid query dropping highly relevant k-NN documents when lexical and vector results were combined.
- [k-NN issue #3012](https://github.com/opensearch-project/k-NN/issues/3012) reports vector corruption/dimension mismatch during a reindex path involving derived source and dynamic templates.
- [Efficient k-NN filtering documentation](https://docs.opensearch.org/latest/vector-search/filter-search-knn/efficient-knn-filtering/) shows that efficient filter support depends on engine and OpenSearch version.

Implication for this project: `tenant_id`, `brand_id`, `platform/account`, effective time, publish/release state, and index version must be represented in the retrieval contract and applied inside the ANN query, not as an application-side post-filter. An OpenSearch adapter needs isolation tests against the exact pinned version and query shape.

### Cross-language retrieval loss

- [RAGFlow issue #12277](https://github.com/infiniflow/ragflow/issues/12277) reports an English paper corpus returning no chunks for the Chinese query `工具调用`, despite a similarity threshold of `0.2` and vector weight of `0.6`.
- [RAGFlow discussion #7470](https://github.com/orgs/infiniflow/discussions/7470) discusses poor cross-language retrieval and mitigation through multilingual embeddings, language partitioning, and translation for lexical search.
- [The Cross-Lingual Cost](https://aclanthology.org/2025.arabicnlp-main.6/) reports domain-specific Arabic-English retrieval degradation when query and evidence languages differ.
- [CLIRudit](https://arxiv.org/abs/2504.16264) reports that direct dense cross-language retrieval and translation-based retrieval have different trade-offs; sparse retrieval especially benefits from language alignment, while translation can damage names and terminology.

Implication: multilingual embedding, query translation, and English BM25 must be compared as separate candidates. No public benchmark proves the winner for this repository's English-only policies and short social DMs.

### Language detection errors

- [Lingua issue #293](https://github.com/pemistahl/lingua-py/issues/293) reports English text being classified as French or Latin when it contains French organization/place names, plus false positives in restricted-language configurations.
- The current repository already treats pure-Han `返金希望` as ambiguous between Chinese and Japanese. This is the correct safety posture unless reliable context resolves it.

Implication: `detected_language` is evidence, not country or locale. `resolved_locale` must be a
separate decision based on exact approved artifact availability and explicit/platform preference.
Ambiguous or mixed text remains HANDOFF under `legacy_hard`; `review` may preserve an otherwise safe
candidate only as a private draft.

### Qwen serving, dimensions, and long inputs

- [Qwen3-Embedding repository](https://github.com/QwenLM/Qwen3-Embedding) lists 0.6B/4B/8B embedding models with maximum dimensions 1024/2560/4096 and 32K sequence length; rerankers are separate 0.6B/4B/8B models.
- [Qwen3-Embedding-8B model card](https://huggingface.co/Qwen/Qwen3-Embedding-8B) documents up to 4096 dimensions and deployment through Sentence Transformers, Transformers, vLLM, or TEI.
- [Qwen repository issue #184](https://github.com/QwenLM/Qwen3-Embedding/issues/184) asks for a clearly discoverable repository LICENSE file even though model documentation states Apache 2.0. The model-card YAML publishes `license: apache-2.0`; legal review should use the exact model artifact and revision.
- [Qwen issue #166](https://github.com/QwenLM/Qwen3-Embedding/issues/166) raises model-training-data license provenance questions involving MS MARCO.
- [vLLM issue #24737](https://github.com/vllm-project/vllm/issues/24737) reports Qwen3 reranker failures/HTTP errors on longer requests and demonstrates the need for explicit request-size, token, batching, and timeout limits.
- [vLLM issue #24327](https://github.com/vllm-project/vllm/issues/24327) reports a long-input crash scenario for the Qwen3 reranker serving path; the public issue content is incomplete, so it is retained as a warning signal rather than quantified evidence.

Implication: do not start with the 8B pair by default. Compare smaller models first, pin model revision/dimension/instruction/max tokens, and batch by total tokens rather than candidate count alone. The reranker cannot recover policies absent from first-stage Recall@20.

### Translation and entity drift

- [Koharu issue #467](https://github.com/mayocream/koharu/issues/467) reports Japanese kana names translating into different names and proposes entity extraction/static mappings.
- [Argos Translate](https://github.com/argosopentech/argos-translate) is an offline option, but model quality and licensing must be checked per language package.
- [NLLB-200 distilled 600M model card](https://huggingface.co/facebook/nllb-200-distilled-600M) states `CC-BY-NC`, describes it as a research model, and says it is not released for production deployment.

Implication: runtime translation must not become the source of truth for official contacts, product IDs, policy conditions, numbers, currencies, or dates. Prefer reviewed localizations; if a translation fallback is later evaluated, protect entities with typed placeholders and fail closed when restoration/validation differs.

## Architecture recommendation for this repository

### Stage 1: safe PostgreSQL vertical slice

Keep PostgreSQL as the durable fact source and current retrieval backend. Maintain and validate:

1. strict embedding response and dimension validation;
2. retrieval errors as unconditional HANDOFF when knowledge retrieval is enabled;
3. `detected_language` and `resolved_locale` as distinct provenance;
4. exact-scan versus filtered-HNSW recall and latency measurement;
5. reviewed localization artifacts bound to the exact English knowledge content hash/revision;
6. deterministic facts and entity preservation before language observation;
7. selector `shadow` evidence before a bounded `live` canary;
8. language-only uncertainty routed to private review, never automatic public sending;
9. real multilingual hot-path integration tests through ReplyDecision and Outbox.

Reviewed localization is the highest-trust wording path. Runtime-generated replies may be eligible
without a localization artifact only after a strong English knowledge match, hard fact/entity/contact
checks, semantic grounding, language policy, account automation state, and send-time Outbox checks all
pass. An unresolved request language under `review` is always a private draft.

### Stage 2: offline candidate bake-off

Compare at least:

- current OpenRouter/OpenAI-compatible embedding baseline;
- BGE-M3 dense;
- Qwen3-Embedding-0.6B with a dimension explicitly selected for a new retrieval index;
- original-language dense only;
- original-language dense + protected English query translation + English lexical;
- candidate union/RRF followed by a small multilingual reranker.

Report by language, tenant, brand/platform/account scope:

- Candidate Recall@1/3/5/20;
- MRR and nDCG;
- wrong policy/scope retrieval;
- false AUTO_REPLY;
- wrong-language output;
- HANDOFF rate and safe automation coverage;
- p50/p95 latency, timeout/OOM rate, and variable cost.

### Stage 3: optional external search projection

Adopt Qdrant or OpenSearch only if the bake-off demonstrates a material advantage over PostgreSQL.
PostgreSQL remains authoritative. Publish/revoke changes project through durable Outbox work into a
versioned index. A release references one immutable index/model/normalization contract; no
application-side post-filtering is allowed.

### Stage 4: optional translation fallback

Keep reviewed localization first. A protected runtime translation fallback, if implemented, is a separate lower-trust mode and remains disabled by default until its own holdout passes. It must never translate official contact details or mutable case-specific facts without deterministic typed placeholders and validation.

## What not to adopt directly

- Do not embed Haystack/LlamaIndex only to recreate orchestration already owned by this application.
- Do not replace the monolith with RAGFlow; it would add a separate platform without satisfying the project's policy/authorization/Outbox contracts.
- Do not switch `OPENAI_EMBEDDING_MODEL` and run the current in-place re-embedding script. Build a new immutable projection, validate it, then promote atomically.
- Do not assume OpenSearch common filters are always ANN pre-filters; verify the exact pinned query shape.
- Do not infer country or regional locale from detected language.

## Required evidence before live mode

A code-complete or synthetic-test-complete implementation remains shadow-only. Live still requires:

1. rotated, non-exposed provider credentials;
2. representative de-identified DM calibration and locked E2E holdout;
3. approved corpus/retrieval/localization/contract hashes;
4. zero confirmed wrong-policy, wrong-scope, wrong-language, contact/slot mutation, or unexpected Outbox cases in the signed gate;
5. staged shadow and canary rollout with kill-switch and rollback drills.
