import pytest
from pydantic import ValidationError

from app.config import Settings


def test_missing_settings_do_not_dump_other_credentials(monkeypatch):
    monkeypatch.delenv("BLUESKY_HANDLE")
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)
    rendered = str(exc.value)
    assert "BLUESKY_HANDLE" in rendered
    assert "test-dashboard-secret" not in rendered
    assert "test-mastodon-secret" not in rendered
    assert "test-app-secret" not in rendered
