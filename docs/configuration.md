# Configuration

Start with `.env.example`, copy it to `.env`, and fill the blank required
values. The template contains no credentials or real account names.

| Variable | Purpose |
| --- | --- |
| `MASTODON_API_BASE_URL` | Instance URL, including `https://`; no account path. |
| `MASTODON_ACCESS_TOKEN` | Read-only user token for the source account. |
| `BLUESKY_HANDLE` | Destination handle, without a leading `@`. |
| `BLUESKY_APP_PASSWORD` | Dedicated Bluesky app password. |
| `THREADS_ENABLED` | Optional Threads publishing; default `false`. Verify identity with the setup helper before enabling. |
| `THREADS_ACCESS_TOKEN` | Optional bootstrap token from Meta, or an imported long-lived token with explicit expiry. Hidden setup prompts can supply it instead. |
| `THREADS_APP_SECRET` | Threads-specific app secret for short-lived token exchange; can be entered at a hidden prompt. This differs from a regular Meta app secret. |
| `THREADS_USER_ID` | Optional expected Threads account ID; setup also pins the verified account in its private file. |
| `THREADS_TOKEN_EXPIRES_AT` | Absolute Unix timestamp in seconds for long-lived token import. Omit for short-lived exchange; do not assign a blank value. |
| `THREADS_TOKEN_FILE` | Private renewable credentials; default `data/threads-token.json`, written atomically with mode `0600`. |
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

The original activation time, self-reply activation time, and separate Threads
activation time are stored in SQLite and survive restarts. Enabling Threads
does not backfill posts before its first activation. Set both reply flags to `false` to disable all
reply publication. A restart reloads code and configuration read from the
mounted `.env`; after editing Compose environment inputs, recreate the
service so its environment is refreshed.

Threads uses a 500-character text budget; the existing Bluesky budget stays at
300. Public text, links, up to four photos with alt text, and eligible self-replies
are supported. Threads replies require their own successful parent mapping.
Photo URLs must be reachable by Meta from the public Mastodon attachment host;
the application does not host media publicly on the NAS. See
[Threads setup and recovery](threads.md) for authorization, token import, and renewal.

The dashboard reports errors and completed parts for the selected destination.
Its authenticated `/dashboard/stats`, `/dashboard/history`, and
`/dashboard/retry/{mastodon_id}` routes default to Bluesky. Append
`?destination=threads` to select Threads; unknown destinations are rejected.
The Threads stats include local connection metadata, never credentials. The
dashboard provides no credential entry form and makes no provider calls for
connection status.

Bluesky retry reuses saved record
keys and references; it does not erase mappings. Successful and ambiguous
legacy jobs cannot be blindly queued again. Back up state before manual
recovery. `data/recovery-attempts/` is local operational data and is excluded
from source control.

Threads retry accepts only `failed` or `queued` jobs while enabled, clears their
attempt count and error, and wakes the worker. A future next-attempt time is
preserved so manual retry cannot shorten a provider's Retry-After; an absent
or past deadline clears. Retry retains every
saved payload, account binding, container ID, child ID, and published post ID.
Disabled, successful, in-progress, and `needs_review` jobs return HTTP `409`;
an ambiguous part also blocks retry. Definite failures back off automatically;
unknown publication outcomes require review rather than a blind second publish.

The dashboard APIs use HTTP Basic authentication. For access over an
untrusted network, put the service behind HTTPS and access controls. Restrict
access to the populated `.env`, renewable token file, and backup files.
Application logs and dashboard error responses redact configured credentials
and recognized token patterns. Normal polling refreshes Threads credentials
near expiry; retain the private file rather than replacing renewed credentials
with the original bootstrap token. Expired, invalid, or changed account bindings
fail closed. For expired or revoked authorization, preserve the pinned account
and run `docker compose exec masto-mirror python -m app.threads_setup --reconnect`;
the helper verifies that original identity before replacing credentials. First
setup uses no flag. Optional blank credential, account-ID, and integer-expiry
assignments stay commented in `.env.example` until you supply values.
This source-only feature uses existing dependencies and needs no image rebuild.
Deploy disabled until Meta registration and authorization are complete.
Video, edit/deletion sync, native quote embeds, and historical
backfill remain unsupported.
