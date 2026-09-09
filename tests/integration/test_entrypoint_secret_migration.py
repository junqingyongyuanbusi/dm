import os
import subprocess
from pathlib import Path

import pytest


def test_entrypoint_prepares_api_and_gates_worker_roles():
    script = Path("entrypoint.sh").read_text()
    assert 'ROLE="${SERVICE_ROLE:-}"' in script
    assert "SERVICE_ROLE is required" in script
    assert "python -m scripts.prepare_database" in script
    assert script.count("python -m scripts.assert_database_ready") == 2


def test_entrypoint_rejects_missing_role():
    env = {**os.environ}
    env.pop("SERVICE_ROLE", None)
    result = subprocess.run(
        ["sh", "entrypoint.sh"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 1
    assert "SERVICE_ROLE is required" in result.stderr


def test_release_requires_app_and_state_service_colocation():
    script = Path("scripts/publish_railway_release.sh").read_text()
    assert "RAILWAY_COLOCATED_SERVICES=(api worker scheduler Postgres Redis)" in script
    assert 'RAILWAY_REGION="us-east4-eqdc4a"' in script
    assert script.count("validate_railway_colocation") == 3
    assert "scripts/railway_active_region.py" in script
    assert "scripts/validate_railway_config.py" in script
    assert script.count("validate_railway_config") == 5
    assert "capture_experimental_multilingual_gate" not in script
    assert "railway-compat-pre-${short_sha}" in script
    assert "require_ci_rollback_compatible_image" in script
    assert "prepare_rollback_compatible_image" not in script
    assert "docker buildx build" not in script
    assert "docker run" not in script
    assert "docker pull" not in script
    assert "docker save" not in script
    assert "docker load" not in script
    assert "scripts/release_rollout_state.py" in script
    assert "migration_compatible_rollback" in script
    assert Path("scripts/verify_migration_compatible_rollback.sh").stat().st_mode & 0o111
    assert script.index(
        'verify_predecessor_image "${IMAGE_REPO}@${previous_digest}"'
    ) < script.index('rollback_compatible_digest="$(require_ci_rollback_compatible_image')
    bridge_case = script.index("promote_compatibility_latest)")
    manifest = script.index(
        'write_manifest "$manifest_status" "$observed_phase"', bridge_case
    )
    promotion = script.index("promote_latest_digest \\", bridge_case)
    assert manifest < promotion
    assert '[[ "$(image_digest "$latest_ref")" == "$expected_current" ]]' in script
    assert "invalid inherited release lock descriptor" in script
    assert script.rindex(
        'require_latest_digest "$expected_digest" "final verification"'
    ) < script.index('write_manifest "completed"')


def test_ci_publishes_the_verified_image_as_immutable_ghcr_sha():
    workflow = Path(".github/workflows/ci.yml").read_text()
    compatibility_script = Path(
        "scripts/publish_ci_migration_rollback_image.sh"
    ).read_text()
    assert "packages: write" in workflow
    assert "persist-credentials: false" in workflow
    assert "ghcr.io/junqingyongyuanbusi/reply-core" in workflow
    assert 'local_image="reply-core-ci:${GITHUB_SHA}"' in workflow
    assert (
        'temporary_ref="${GHCR_IMAGE}:ci-${GITHUB_SHA}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"'
        in workflow
    )
    assert 'sha_ref="${GHCR_IMAGE}:${GITHUB_SHA}"' in workflow
    assert 'docker pull "$sha_ref"' in workflow
    assert 'scripts/verify_production_image.sh "$sha_ref" "$GITHUB_SHA"' in workflow
    assert "--prefer-index=false" in workflow
    assert "latest remains release-controlled" in workflow
    assert "GHCR_PROMOTE_SHA" not in workflow
    assert "BOOTSTRAP_PREDECESSOR" not in workflow
    assert "Publish verified image to GHCR" in workflow
    assert "scripts/publish_ci_migration_rollback_image.sh" in workflow
    assert 'fetch-depth: 0' in workflow
    assert Path("scripts/verify_production_image.sh").stat().st_mode & 0o111
    assert Path("scripts/publish_ci_migration_rollback_image.sh").stat().st_mode & 0o111
    assert (
        'git diff --quiet "$predecessor_revision" "$target_sha" -- migrations/versions'
        in compatibility_script
    )
    assert "docker buildx build" in compatibility_script
    assert "scripts/verify_migration_compatible_rollback.sh" in compatibility_script
    assert 'write_evidence false' in compatibility_script


def test_release_bridges_legacy_review_outbox_contract_in_validated_order():
    script = Path("scripts/publish_railway_release.sh").read_text()
    dockerfile = Path("Dockerfile").read_text()
    compatibility_dockerfile = Path(
        "deploy/Dockerfile.migration-compatible-rollback"
    ).read_text()
    assert 'IMAGE_REPO="ghcr.io/junqingyongyuanbusi/reply-core"' in script
    assert "CI must publish $sha_ref before Railway release" in script
    assert "Railway native image auto-update must be disabled" in script
    assert "resuming in-flight $stage $service deployment" in script
    assert "refusing duplicate redeploy" in script
    assert 'TARGET_REVIEW_OUTBOX_CAPABILITY="review-outbox-dual-read-v1"' in script
    assert "review-outbox-dual-read-v1" in dockerfile
    assert "BASE_REVIEW_OUTBOX_CAPABILITY" in compatibility_dockerfile
    assert "DATABASE_HEAD" in compatibility_dockerfile
    assert "migrations/versions/" in compatibility_dockerfile
    assert "scripts/release_rollout_state.py" in script
    assert "nine safe bridge checkpoints" in script

    compatibility_latest = script.index("promote_compatibility_latest)")
    compatibility_api = script.index("deploy_compatibility_api)")
    compatibility_worker = script.index("deploy_compatibility_worker)")
    compatibility_scheduler = script.index("deploy_compatibility_scheduler)")
    target_latest = script.index("promote_target_latest)")
    target_worker = script.index("deploy_target_worker)")
    target_api = script.index("deploy_target_api)")
    target_scheduler = script.index("deploy_target_scheduler)")
    assert (
        compatibility_latest
        < compatibility_api
        < compatibility_worker
        < compatibility_scheduler
        < target_latest
        < target_worker
        < target_api
        < target_scheduler
    )
    assert script.count("promote_latest_digest \\") >= 2
    assert 'write_manifest "$manifest_status" "$observed_phase"' in script
    assert 'recorded_rollout_phase="$next_phase"' in script
    assert Path("scripts/publish_railway_release.sh").stat().st_mode & 0o111


def test_compatibility_api_migrates_before_target_worker_readiness_smoke():
    script = Path("scripts/verify_migration_compatible_rollback.sh").read_text()
    compat_prepare = script.index(
        'run_python_module "$compat_image" scripts.prepare_database'
    )
    compat_ready = script.index(
        'run_python_module "$compat_image" scripts.assert_database_ready'
    )
    target_worker_ready = script.index(
        'run_python_module "$target_image" scripts.assert_database_ready'
    )
    target_prepare = script.index(
        'run_python_module "$target_image" scripts.prepare_database'
    )
    assert compat_prepare < compat_ready < target_worker_ready < target_prepare


def test_migration_compatible_rollback_retags_latest_and_redeploys_in_order():
    script_path = Path("scripts/rollback_railway_migration_compatible.sh")
    script = script_path.read_text()
    assert 'IMAGE_REPO="ghcr.io/junqingyongyuanbusi/reply-core"' in script
    assert "release manifest image repository is not the production GHCR repository" in script
    assert script.count("validate_railway_image_auto_updates") >= 3
    assert "resuming in-flight $service rollback deployment" in script
    assert "unresolved in-flight rollback deployment" in script
    promote = script.index("docker buildx imagetools create --prefer-index=false")
    verify = script.rindex("verify_compatibility_image")
    api = script.index('api_deployment_id="$(redeploy_role api)"')
    worker = script.index('worker_deployment_id="$(redeploy_role worker)"')
    scheduler = script.index('scheduler_deployment_id="$(redeploy_role scheduler)"')
    assert verify < promote < api < worker < scheduler
    assert script_path.stat().st_mode & 0o111
    assert "--execute=<target-full-sha>" in script
    assert ".run/publish-railway-release.lock" in script
    assert "expected_compat_ref" in script
    assert ".previous_digest" in script
    assert '|| "$digest" == "$previous_digest"' in script
    assert '|| "$active_digest" == "$previous_digest"' in script
    assert "release manifest status is not rollback-eligible" in script
    assert script.count("validate_current_state") >= 4
    assert script.rindex("require_release_mutation") < promote
    assert "scripts/rollback_state_guard.py" in script
    assert "verify_compatibility_image" in script
    assert "railway_source_image" in script
    assert "invalid inherited release lock descriptor" in script
    assert "migration_compatible_rollback.digest" in script
    assert "wait_for_api_health" in script
    assert "validate_railway_config.py" in script
    assert script.rindex("require_compat_latest") < script.index(
        'rollback_manifest="dist/rollback-${target_sha}.json"'
    )
    migration_runbook = Path("docs/production-migration.md").read_text()
    assert "rollback_railway_migration_compatible.sh" in migration_runbook
    assert "--execute=<target-full-sha>" in migration_runbook


def test_migration_compatible_rollback_rejects_prepared_manifest(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for command in ("docker", "railway"):
        executable = bin_dir / command
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
    manifest = tmp_path / "release.json"
    target_sha = "a" * 40
    manifest.write_text('{"status":"prepared","git_sha":"' + target_sha + '"}')
    result = subprocess.run(
        [
            "scripts/rollback_railway_migration_compatible.sh",
            f"--execute={target_sha}",
            str(manifest),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )
    assert result.returncode == 1
    assert "release manifest status is not rollback-eligible: prepared" in result.stderr


def _run_worker_entrypoint(
    tmp_path: Path,
    *,
    processes: str | None = None,
    threads: str | None = None,
    timeout_ms: str | None = None,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = bin_dir / "python"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    dramatiq = bin_dir / "dramatiq"
    dramatiq.write_text(
        "#!/bin/sh\n"
        "printf 'timeout=%s\\n' \"$dramatiq_worker_timeout\"\n"
        "printf 'args=%s\\n' \"$*\"\n"
    )
    dramatiq.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "SERVICE_ROLE": "worker",
    }
    for key, value in (
        ("DRAMATIQ_PROCESSES", processes),
        ("DRAMATIQ_THREADS", threads),
        ("DRAMATIQ_WORKER_TIMEOUT_MS", timeout_ms),
    ):
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(
        ["sh", "entrypoint.sh"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_worker_entrypoint_uses_bounded_defaults(tmp_path: Path):
    result = _run_worker_entrypoint(tmp_path)
    assert result.returncode == 0
    assert "timeout=250" in result.stdout
    assert "args=--processes 4 --threads 8 apps.worker.main" in result.stdout


def test_worker_entrypoint_accepts_explicit_concurrency(tmp_path: Path):
    result = _run_worker_entrypoint(
        tmp_path,
        processes="6",
        threads="3",
        timeout_ms="400",
    )
    assert result.returncode == 0
    assert "timeout=400" in result.stdout
    assert "args=--processes 6 --threads 3 apps.worker.main" in result.stdout


@pytest.mark.parametrize(
    ("processes", "threads", "timeout_ms", "expected"),
    [
        ("0", None, None, "DRAMATIQ_PROCESSES must be an integer between 1 and 32"),
        ("33", None, None, "DRAMATIQ_PROCESSES must be an integer between 1 and 32"),
        ("08", None, None, "DRAMATIQ_PROCESSES must be an integer between 1 and 32"),
        ("9999999999", None, None, "DRAMATIQ_PROCESSES must be an integer between 1 and 32"),
        (None, "invalid", None, "DRAMATIQ_THREADS must be an integer between 1 and 32"),
        (None, None, "49", "DRAMATIQ_WORKER_TIMEOUT_MS must be an integer between 50 and 5000"),
        (None, None, "5001", "DRAMATIQ_WORKER_TIMEOUT_MS must be an integer between 50 and 5000"),
        ("32", "5", None, "DRAMATIQ_PROCESSES * DRAMATIQ_THREADS must not exceed 128"),
    ],
)
def test_worker_entrypoint_rejects_invalid_concurrency(
    tmp_path: Path,
    processes: str | None,
    threads: str | None,
    timeout_ms: str | None,
    expected: str,
):
    result = _run_worker_entrypoint(
        tmp_path,
        processes=processes,
        threads=threads,
        timeout_ms=timeout_ms,
    )
    assert result.returncode == 1
    assert expected in result.stderr
    assert "args=" not in result.stdout
