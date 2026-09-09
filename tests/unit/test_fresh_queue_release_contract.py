import json
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest


def test_fresh_release_keeps_preflights_before_stop_and_api_before_consumers():
    script = Path("scripts/publish_railway_release.sh").read_text()
    invocation = script.index("  prepare_fresh_queue_cutover\n")
    for gate in (
        "wait_for_ci\n", "validate_railway_config\n", "validate_railway_colocation\n",
        'verify_sha_image "$sha_ref"', 'write_manifest "$manifest_status"',
        'verify_predecessor_image "${IMAGE_REPO}@${previous_digest}"',
    ):
        assert script.index(gate) < invocation
    assert script.index("run_standard_rollout()") < invocation
    standard = script.split("run_standard_rollout()", 1)[1].split("\n}", 1)[0]
    assert standard.index("deploy_role api") < standard.index("wait_for_api_health")
    assert standard.index("wait_for_api_health") < standard.index("deploy_role worker")
    assert "RAILWAY_COLOCATED_SERVICES=(api worker scheduler Postgres Redis)" in script


def test_fresh_release_records_evidence_and_never_flushes_or_auto_deploys_variables():
    script = Path("scripts/publish_railway_release.sh").read_text()
    fresh = script.split("prepare_fresh_queue_cutover()", 1)[1].split("\n}\n", 1)[0]
    assert "scheduler worker api" in fresh
    assert fresh.index('write_manifest "deploying"') < fresh.index("railway down")
    assert fresh.index("assert_fresh_role_stopped") < fresh.index("datetime.now(timezone.utc)")
    assert fresh.index("datetime.now(timezone.utc)") < fresh.index("railway variable set")
    assert "--skip-deploys" in fresh
    assert "WORKSPACE_QUEUE_CUTOVER=" in fresh
    assert "DRAMATIQ_NAMESPACE=" in fresh
    assert "fresh_queue: $fresh_queue" in script
    assert "fresh queue recovery requires explicit operator review" in script
    assert "FLUSHDB" not in script.upper()
    assert "FLUSHALL" not in script.upper()


def test_empty_active_deployment_requires_fresh_stopped_predecessor():
    script = Path("scripts/publish_railway_release.sh").read_text()
    deploy = script.split("deploy_role()", 1)[1].split("\n}\n", 1)[0]
    assert 'if [[ -z "$active_id" || -z "$active_digest" ]]' in deploy
    assert '[[ "$fresh_queue" == "true" ]]' in deploy
    assert 'assert_fresh_role_stopped "$service"' in deploy
    stopped = script.split("assert_fresh_role_stopped()", 1)[1].split("\n}\n", 1)[0]
    assert "deploymentStopped == true" in stopped
    assert 'deployment_runtimes="$(service_deployment_runtimes_json "$service")"' in stopped
    assert '|| fail "$service deployment stop query failed"' in stopped
    assert 'all(.[];' in stopped


@pytest.mark.parametrize("arguments", [
    ["--fresh-queue"], ["--restore-admin-id"], ["--skip-ci"],
    ["--fresh-queue", "--restore-admin-id", "bad-uuid"],
    ["--restore-admin-id", "11111111-1111-4111-8111-111111111111"],
])
def test_invalid_release_arguments_fail_before_any_remote_work(arguments, tmp_path):
    command_log = tmp_path / "commands.log"
    for command in ("git", "docker", "railway", "gh", "jq", "curl", "date", "awk", "python3", "uv"):
        executable = tmp_path / command
        executable.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$COMMAND_LOG"\nexit 95\n'
        )
        executable.chmod(0o755)
    bash = shutil.which("bash")
    assert bash is not None
    result = subprocess.run(
        [bash, "scripts/publish_railway_release.sh", *arguments],
        check=False, capture_output=True, text=True,
        env={"PATH": str(tmp_path), "HOME": str(tmp_path), "COMMAND_LOG": str(command_log)},
        timeout=5,
    )
    assert not command_log.exists()
    assert result.returncode != 0
    assert "[release] ERROR:" in result.stderr
    assert "required command not found" not in result.stderr


def test_default_arguments_do_not_activate_fresh_queue():
    parser = Path("scripts/publish_railway_release.sh").read_text().split("require_command()", 1)[0]
    result = subprocess.run(
        ["bash", "-c", parser + '\nprintf "%s" "$fresh_queue"'],
        check=False, capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0
    assert result.stdout == "false"


@pytest.mark.parametrize("stopped,status,query_status,allowed", [
    (True, "REMOVED", 0, True), (True, "REMOVED", 1, False),
    (None, "REMOVED", 0, False), ("true", "REMOVED", 0, False),
    (False, "SUCCESS", 0, False), (True, "QUEUED", 0, False),
    (True, "UNKNOWN", 0, False),
])
def test_stopped_predecessor_guard_rejects_query_failure_and_unknown_state(
    tmp_path, stopped, status, query_status, allowed,
):
    script = Path("scripts/publish_railway_release.sh").read_text()
    guard = "assert_fresh_role_stopped()" + script.split(
        "assert_fresh_role_stopped()", 1,
    )[1].split("\n}\n", 1)[0] + "\n}\n"
    manifest = tmp_path / "release.json"
    manifest.write_text(json.dumps({
        "fresh_queue": {}, "previous_railway": {"api_deployment_id": "old"},
    }))
    runtimes = json.dumps([{"id": "old", "status": status, "deploymentStopped": stopped}])
    harness = (
        "set -Eeuo pipefail\n"
        'fail() { printf "%s\\n" "$*" >&2; exit 1; }\n'
        "fresh_queue=true\nprevious_api_deployment_id=old\n"
        f"manifest_path={shlex.quote(str(manifest))}\n"
        'deployment_runtime_json() { printf \'%s\' \'{"deploymentStopped":true}\'; }\n'
        "service_deployment_runtimes_json() { printf '%s' "
        + shlex.quote(runtimes) + f"; return {query_status}; }}\n"
        + guard + "assert_fresh_role_stopped api\n"
    )
    result = subprocess.run(
        ["bash", "-c", harness], check=False, capture_output=True, text=True, timeout=5,
    )
    assert (result.returncode == 0) is allowed
