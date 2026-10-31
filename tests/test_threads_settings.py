"""Threads is optional and must not alter existing Bluesky configuration."""
from app.config import Settings
from app.core.errors import describe_error


def test_threads_is_disabled_without_extra_credentials():
    config = Settings(_env_file=None)
    assert config.THREADS_ENABLED is False
    assert config.THREADS_ACCESS_TOKEN is None


def test_threads_credentials_are_redacted(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "THREADS_ACCESS_TOKEN", "private-threads-token")
    monkeypatch.setattr(settings, "THREADS_APP_SECRET", "private-threads-app-secret")
    detail = describe_error(RuntimeError("private-threads-token private-threads-app-secret"))
    assert "private-threads-token" not in detail
    assert "private-threads-app-secret" not in detail
