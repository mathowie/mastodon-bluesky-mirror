# Configuration

Start with `.env.example`, copy it to `.env`, and fill the blank required
values. The template contains no credentials or real account names.

| Variable | Purpose |
| --- | --- |
| `MASTODON_API_BASE_URL` | Instance URL, including `https://`; no account path. |
| `MASTODON_ACCESS_TOKEN` | Read-only user token for the source account. |
| `BLUESKY_HANDLE` | Destination handle, without a leading `@`. |
| `BLUESKY_APP_PASSWORD` | Dedicated Bluesky app password. |
| `DASHBOARD_PASSWORD` | Long, unique password; dashboard username is `admin`. |
| `SYNC_INTERVAL_SECONDS` | Poll interval; default `60`. |
| `MIRROR_SELF_REPLIES` | Public self-replies with mapped parents; default `true`. |
| `MIRROR_REPLIES` | Other replies, when a reliable parent mapping exists; default `false`. |
| `MIRROR_CONTENT_WARNINGS` | Prefix mirrored text with its warning; default `true`. |

The Compose file supplies `DATABASE_URL=sqlite:////app/data/mirror.db`.
The legacy `BLUESKY_PASSWORD` setting is accepted as a fallback, but use
`BLUESKY_APP_PASSWORD` for normal setup. The boost and quote preference
fields are reserved; they do not implement boost or native quote mirroring.

Only public source posts are eligible. A public reply's source parent must
also remain public. Replies attach to the last mirrored parent part while
retaining the original thread root. Missing or edited Bluesky context causes
a visible failure instead of an unrelated top-level publication.

The original activation time and self-reply activation time are stored in
SQLite and survive restarts. Set both reply flags to `false` to disable all
reply publication. A restart reloads code and configuration read from the
mounted `.env`; after editing Compose environment inputs, recreate the
service so its environment is refreshed.

The dashboard reports errors and completed parts. Retry reuses saved record
keys and references; it does not erase mappings. Successful and ambiguous
legacy jobs cannot be blindly queued again. Back up state before manual
recovery. `data/recovery-attempts/` is local operational data and is excluded
from source control.

The dashboard APIs use HTTP Basic authentication. For access over an
untrusted network, put the service behind HTTPS and access controls. Restrict
access to the populated `.env` and backup files. Application logs redact
configured credentials and recognized token patterns.
