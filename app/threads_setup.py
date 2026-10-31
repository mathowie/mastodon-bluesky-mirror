"""Verify private Threads credentials; use --reconnect for explicit recovery."""
import argparse
import getpass
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

from .core.threads_auth import ThreadsAPIError, ThreadsAuth


def main(config=None, session=None, *, reconnect=False):
    if config is None:
        from .config import settings
        config = settings
    # Hidden prompts must not change the running worker's shared settings.
    private = SimpleNamespace(**{name: getattr(config, name) for name in (
        "THREADS_ENABLED", "THREADS_ACCESS_TOKEN", "THREADS_APP_SECRET",
        "THREADS_USER_ID", "THREADS_TOKEN_EXPIRES_AT", "THREADS_TOKEN_FILE",
    )})
    auth = ThreadsAuth(config=private, session=session)
    try:
        if reconnect:
            token = getpass.getpass("Fresh Threads access token: ")
            app_secret = getpass.getpass("Threads app secret: ")
            credentials = auth.reconnect(token, app_secret)
        else:
            status = auth.connection_status()
            if status["status"] == "ready":
                credentials = auth.verify_existing()
            else:
                if status["status"] in ("not_configured", "setup_required"):
                    if not private.THREADS_ACCESS_TOKEN:
                        private.THREADS_ACCESS_TOKEN = getpass.getpass("Threads access token: ")
                    if private.THREADS_TOKEN_EXPIRES_AT is None and not private.THREADS_APP_SECRET:
                        private.THREADS_APP_SECRET = getpass.getpass("Threads app secret: ")
                credentials = auth.authorize()
    except ThreadsAPIError as exc:
        print(str(exc), file=sys.stderr)
        print("Use python -m app.threads_setup --reconnect to replace credentials for the same account.", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Threads setup cancelled", file=sys.stderr)
        return 1
    expiry = datetime.fromtimestamp(credentials.expires_at, timezone.utc).isoformat()
    print(f"Threads username: @{credentials.username}")
    print(f"Threads user ID: {credentials.account_id}")
    print(f"Token expires: {expiry}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify private Threads credentials without publishing")
    parser.add_argument("--reconnect", action="store_true",
                        help="Prompt for fresh credentials and replace the private file after verifying the same account")
    args = parser.parse_args()
    raise SystemExit(main(reconnect=args.reconnect))
