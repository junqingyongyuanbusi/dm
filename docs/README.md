# Documentation map

## Current authority

- [Runtime architecture](architecture.md): process ownership, state boundaries, message paths and
  reliability invariants.
- [Configuration reference](configuration.md): application, module-level and deployment-only
  environment variables.
- [Platform account control plane](admin-control-plane.md): account, credential, tenant and
  provisioning trust boundaries.
- [Production migration notes](production-migration.md): database, encrypted-secret and staged
  rollout requirements.
- [VPS operations](../deploy/vps/README.md): deployment, backup, upgrade and rollback runbook.

When documents disagree, executable code and Alembic migrations define behavior. Update the current
architecture/configuration documents in the same change that alters their contracts.

## Historical material

- `../PLAN.md` is the original product and architecture exploration.
- `superpowers/plans/` contains implementation plans, old code sketches, historical test counts and
  superseded rollout assumptions.

Historical material explains why earlier decisions were considered, but it must not be used as a
runbook or current API contract.
