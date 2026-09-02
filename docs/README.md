# Documentation map

## Current authority

The direct account platform contract currently covers seven platforms: Telegram, Facebook,
Instagram, WhatsApp, Feishu, X and Email. The Alembic graph has one current head:
`a8f4d2c6e901`. Email protocol/unit coverage does not by itself imply that a real mailbox credential
or live provider E2E has been validated.

- [Runtime architecture](architecture.md): process ownership, state boundaries, message paths,
  reliability invariants and the C1 legacy-schema compatibility boundary, including the
  English-corpus multilingual RAG and review path.
- [Configuration reference](configuration.md): application, module-level and deployment-only
  environment variables, language policy, selector canary semantics and retrieval backend choice.
- [Multilingual English-corpus replies](multilingual-reviewed-localization.md): operator-facing
  runtime flow, fail-closed boundaries and reviewed-localization preference.
- [Platform account control plane](admin-control-plane.md): account, credential, tenant and
  provisioning trust boundaries.
- [Feishu integration operator runbook](feishu-integration.md): self-built application Bot setup,
  callback verification, draft-first smoke checks, activation and rollback.
- [Email integration operator runbook](email-integration.md): implemented IMAP/SMTP contract,
  deployment gates, Phase 0 with administrator-provided credentials, real smoke and rollback.
- [Production migration notes](production-migration.md): database, encrypted-secret and staged
  rollout requirements.
- [Reliability fault drills](reliability-drills.md): repeatable queue-loss, crash, lease, takeover,
  retry-exhaustion and recovery validation.
- [Railway release script](../scripts/publish_railway_release.sh): verifies the CI-published immutable GHCR SHA image, prepares rollback evidence, promotes `latest`, and performs the production rollout.
- [Migration-compatible Railway rollback](../scripts/rollback_railway_migration_compatible.sh): restores the predecessor application on the additive current schema using the release manifest's compatible digest.

When documents disagree, executable code and Alembic migrations define behavior. Update the current
architecture/configuration documents in the same change that alters their contracts.

## Research and proposals / not runtime authority

- [Multilingual reply OSS research](multilingual-oss-research.md): primary-source candidate and
  failure-evidence review for PostgreSQL/pgvector, Qdrant, OpenSearch, embedding/reranking frameworks,
  language detection and translation. It informs the current architecture but does not authorize a
  production mode or override executable policy.

- [Multilingual knowledge replies ADR](proposals/multilingual-knowledge-replies-adr.md):
  earlier proposed architecture and bake-off contract for replying in the customer's language from
  a canonical English knowledge base. Current runtime behavior is defined by
  [architecture.md](architecture.md), [configuration.md](configuration.md), code and migrations;
  proposal-only assumptions do not override them.
- [Multilingual knowledge replies sources](proposals/multilingual-knowledge-replies-sources.md):
  stable papers and official documentation used by the proposed ADR.

## Historical material

- `superpowers/plans/` contains implementation plans, old code sketches, historical test counts and
  superseded rollout assumptions. Some archived plans reference the removed original `PLAN.md`; those
  references are preserved as historical context only.

Historical material explains why earlier decisions were considered, but it must not be used as a
runbook or current API contract.
