from pathlib import Path


def test_entrypoint_prepares_api_and_gates_worker_roles():
    script = Path("entrypoint.sh").read_text()
    assert "python scripts/prepare_database.py" in script
    assert script.count("python scripts/assert_database_ready.py") == 2
