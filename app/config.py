# app/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # Mastodon Configuration
    MASTODON_API_BASE_URL: str
    MASTODON_ACCESS_TOKEN: str

    # Bluesky Configuration
    BLUESKY_HANDLE: str
    BLUESKY_PASSWORD: str | None = None  # Legacy fallback; prefer BLUESKY_APP_PASSWORD
    BLUESKY_APP_PASSWORD: str | None = None

    # Optional Threads destination. Secrets stay in the private environment or
    # renewable token file; the file is never served by the dashboard.
    THREADS_ENABLED: bool = False
    THREADS_ACCESS_TOKEN: str | None = None
    THREADS_APP_SECRET: str | None = None
    THREADS_USER_ID: str | None = None
    THREADS_TOKEN_EXPIRES_AT: int | None = None
    THREADS_TOKEN_FILE: str = "data/threads-token.json"

    # App Configuration
    DATABASE_URL: str = "sqlite:///./mirror.db"
    SYNC_INTERVAL_SECONDS: int = 60
    DASHBOARD_PASSWORD: str = "change-me"
    
    # Mirroring Preferences
    MIRROR_SELF_REPLIES: bool = True
    MIRROR_REPLIES: bool = False
    MIRROR_BOOSTS: bool = False
    MIRROR_QUOTE_POSTS: bool = True
    MIRROR_CONTENT_WARNINGS: bool = True
    
    model_config = SettingsConfigDict(env_file=".env", hide_input_in_errors=True)

settings = Settings()
