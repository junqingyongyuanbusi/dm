# Documentation map

## Current authority

The direct account platform contract currently covers seven platforms: Telegram, Facebook,
Instagram, WhatsApp, Feishu, X and Email. The Alembic graph has one current head:
`a6f1c3d8e205`. Email protocol/unit coverage does not by itself imply that a real mailbox credential
or live provider E2E has been validated.

- [Runtime architecture](architecture.md): process ownership, state boundaries, message paths and
  reliability invariants.
- [Configuration reference](configuration.md): application, module-level and deployment-only
  environment variables.
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
- [Railway release script](../scripts/publish_railway_release.sh): the required Docker Hub publish and production rollout entrypoint.

When documents disagree, executable code and Alembic migrations define behavior. Update the current
architecture/configuration documents in the same change that alters their contracts.

## Proposed research / not runtime authority

- [Multilingual knowledge replies ADR](proposals/multilingual-knowledge-replies-adr.md):
  proposed architecture and bake-off contract for replying in the customer's language from a
  canonical English knowledge base. The Phase 1 trusted-local, synthetic-only internal schema and
  library foundation is implemented; the runtime/live architecture and real dm bake-off are not,
  no architecture winner has been selected, and runtime configuration remains unchanged.
- [Multilingual knowledge replies sources](proposals/multilingual-knowledge-replies-sources.md):
  stable papers and official documentation used by the proposed ADR.

## Historical material

- `superpowers/plans/` contains implementation plans, old code sketches, historical test counts and
  superseded rollout assumptions. Some archived plans reference the removed original `PLAN.md`; those
  references are preserved as historical context only.

Historical material explains why earlier decisions were considered, but it must not be used as a
runbook or current API contract.
