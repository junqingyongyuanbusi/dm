from pathlib import Path


def test_production_release_does_not_require_database_admin() -> None:
    script = Path("scripts/publish_railway_release.sh").read_text()

    assert "verify_active_default_admin" not in script
    assert "default_tenant_active_admin_required" not in script
    assert "role = 'ADMIN'" not in script
