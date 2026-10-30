import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.update({
    "MASTODON_API_BASE_URL": "https://mastodon.example",
    "MASTODON_ACCESS_TOKEN": "test-mastodon-secret",
    "BLUESKY_HANDLE": "test.bsky.social",
    "BLUESKY_APP_PASSWORD": "test-app-secret",
    "DASHBOARD_PASSWORD": "test-dashboard-secret",
    "DATABASE_URL": "sqlite:///:memory:",
    "MIRROR_REPLIES": "false",
    "MIRROR_BOOSTS": "false",
    "MIRROR_CONTENT_WARNINGS": "true",
})
