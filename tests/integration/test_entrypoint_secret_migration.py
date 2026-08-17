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
    assert script.count("validate_railway_config") == 4
    assert "multilingual_live_enabled: false" in script
    assert "multilingual_shadow_enabled: false" in script
    assert "english_knowledge_only_enabled: false" in script


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
