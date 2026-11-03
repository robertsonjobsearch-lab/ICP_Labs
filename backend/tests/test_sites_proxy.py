"""The Sites gate is tested without opening a database connection."""
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app

DB = "postgresql+psycopg://unused:unused@127.0.0.1:1/unused_test"
SECRET = "a" * 48


@pytest.mark.parametrize("prefix", ["postgres://", "postgresql://", "postgresql+psycopg://"])
def test_render_database_url_uses_installed_driver(prefix):
    settings = Settings(database_url=prefix + "unused:unused@127.0.0.1:1/unused_test")
    assert settings.database_url == DB


def test_sites_gate_rejects_missing_or_wrong_secret():
    settings = Settings(database_url=DB, sites_proxy_secret=SECRET)
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 403
        assert client.get("/api/health", headers={"x-icp-sites-key": "wrong"}).status_code == 403
        assert client.get("/api/health", headers={"x-icp-sites-key": SECRET}).status_code == 200
        assert client.get("/health").status_code == 200
    app.state.engine.dispose()


def test_original_stack_remains_compatible_without_gate():
    app = create_app(Settings(database_url=DB))
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
    app.state.engine.dispose()


def test_secret_configuration_is_validated_and_redacted():
    for value in ("short", "a" * 40 + "\n"):
        with pytest.raises(ValidationError):
            Settings(database_url=DB, sites_proxy_secret=value)
    assert SECRET not in repr(Settings(database_url=DB, sites_proxy_secret=SECRET))

