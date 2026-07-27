# Repository Instructions

## Scope And Branch

- Work only on `dev` unless the user explicitly names another branch. Never modify or deploy `main` implicitly.
- Preserve unrelated user changes. Do not reset, revert, or include them in a release.
- PostgreSQL is durable truth. Redis remains transient infrastructure. API, Worker, and Scheduler use one shared image selected by `SERVICE_ROLE`.

## Completion Gate

A repository change is not complete when tests or the Git push finish. For every completed change pushed to `dev`, Codex must also publish and deploy the resulting image unless the user explicitly says not to deploy.

Before declaring completion:

1. Run focused validation, repository Ruff, the full pytest suite, migration/metadata checks when applicable, and `git diff --check`.
2. Commit the change independently on `dev` and push it to `origin/dev`.
3. Wait for every GitHub Actions CI run for that commit to complete successfully.
4. Run `scripts/publish_railway_release.sh` from a clean worktree.
5. Confirm Docker Hub and Railway verification from the script succeeded.

If Docker Hub, GitHub, or Railway credentials are unavailable, report `implementation complete, release blocked`; do not claim the task is complete or deployed.

## Docker Hub Release Contract

- Repository: `zhiyangxiaozi/reply-core`.
- Build exactly one `linux/amd64` image from the pushed commit.
- Push the immutable full-SHA tag `zhiyangxiaozi/reply-core:<40-character-git-sha>`.
- Never rebuild `latest` separately. Promote the already-pushed SHA image to `latest` with registry manifest tooling.
- Use `docker buildx imagetools create --prefer-index=false` so the SHA tag and `latest` resolve to the same digest.
- Keep immutable SHA tags. Before replacing `latest`, retain the currently deployed Railway digest under `railway-pre-<short-sha>` for rollback.
- Do not publish from a dirty worktree, an unpushed commit, a branch other than `dev`, or a commit with failing/pending CI.

## Railway Release Contract

- Project: `reply-core`; environment: `production`.
- Railway services `api`, `worker`, and `scheduler` must use `zhiyangxiaozi/reply-core:latest`.
- Use `railway redeploy --from-source` so Railway pulls the newly promoted `latest`; do not rely on a plain redeploy or image auto-update.
- Deploy API first because API owns database preparation and migrations. Require API `SUCCESS`, the expected image digest, and a successful `/healthz` before updating other roles.
- Then deploy Worker and Scheduler. Require both to reach `SUCCESS` at the same expected digest.
- Final verification must prove API, Worker, and Scheduler all run one identical digest matching Docker Hub `latest`.
- Retain the prior digest and deployment IDs. Image rollback does not imply database rollback; follow `docs/production-migration.md` for migration-specific restore requirements.
- A release requiring a staged or coordinated rollout must update `docs/production-migration.md` and follow that procedure instead of blindly using the generic script.

## Required Release Command

```bash
scripts/publish_railway_release.sh
```

The script is the default release path. Do not replace it with an ad hoc sequence unless repairing the script itself; any manual fallback must preserve every invariant above and be reported explicitly.
