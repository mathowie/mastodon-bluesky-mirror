# Optional Threads destination

Threads is disabled by default. It shares Mastodon polling with Bluesky while
keeping its account binding, activation time, history, checkpoints, and retry
budget separate. Bluesky continues to use the existing settings and API defaults.

## Authorize your own account with Meta

1. Register as a developer at [Meta for Developers](https://developers.facebook.com/).
2. Create an app with the Threads use case. Follow Meta's current app setup
   instructions in the official [Threads API collection](https://www.postman.com/meta/threads/collection/dht3nzz/threads-api).
3. Add your own Threads account as an authorized app user/tester, accept any
   tester invitation with that account, and authorize the app's access to it.
   Grant `threads_basic` and `threads_content_publish`. Include
   `threads_manage_replies` when authorizing reply management for self-replies;
   follow Meta's current permission requirements for the reply endpoint.
4. Generate a user access token using Meta's developer UI and obtain the
   Threads-specific app secret. Meta's page labels and navigation can change;
   follow the current official instructions for your app and account.
   Look for **User Token Generator** within the app's Threads use-case settings.
   A token generated there may already be long-lived; check its actual expiry
   before choosing the import or exchange path below.

Use the app ID and secret for the Threads API of the app. The official
[Meta sample](https://github.com/fbsamples/threads_api) explicitly distinguishes
these from a regular Meta app ID and secret. This application uses the Threads
app secret to exchange a short-lived user token; it does not ask for your account
password or implement a browser OAuth callback. Keep tokens and app secrets in
private configuration or the helper's hidden prompts.

## Bootstrap inside the existing container

Keep `THREADS_ENABLED=false` while completing setup. With the usual Compose
service already running, execute this first-time setup without a flag:

```sh
docker compose exec masto-mirror python -m app.threads_setup
```

If credentials are absent from private configuration, the helper prompts for
the user token and Threads app secret without echoing them. Alternatively set
`THREADS_ACCESS_TOKEN` and `THREADS_APP_SECRET` in the private `.env` before
running it. The helper reads the container environment and mounted `.env`;
existing container environment values take precedence. Recreate the service
after changing Compose environment inputs so those values are refreshed.
Leave optional credential and account-ID assignments commented out when unused;
uncomment them only when supplying actual values.
Do not put secrets in command-line arguments, repository files, or dashboard fields.

The helper exchanges the short-lived token for a long-lived token, verifies
`/me`, pins the verified account ID, and atomically writes the renewable token
and identity/expiry metadata to `data/threads-token.json`. The private file has
mode `0600`; its directory must allow the container to replace it atomically.
The CLI prints only the verified username, account ID, and expiry. It publishes
no posts and does not enable mirroring.

To import an existing long-lived token instead, set `THREADS_ACCESS_TOKEN` and
`THREADS_TOKEN_EXPIRES_AT` in private configuration before running the same
helper. The expiry must be the token's actual absolute Unix timestamp in
seconds, not a lifetime duration or a date string. Omit that setting for
short-lived exchange; a blank expiry assignment is invalid. The imported
token is still verified through `/me` and saved with its account binding.
`THREADS_USER_ID` can assert an expected account ID; it is not a username.
Use Meta's reported expiry, available through its official
[Debug Access Token request](https://www.postman.com/meta/threads/request/34203612-e9a7f46e-e48c-4987-a203-22fb25a4b604).
An already long-lived token should use this import path; attempting to exchange
it as a short-lived token can return HTTP `400` even when it is valid.

Verify that the CLI identity is your intended destination. Then set
`THREADS_ENABLED=true` in private `.env` and recreate the existing service:

```sh
docker compose up -d --force-recreate masto-mirror
```

Select **Threads** in the dashboard to inspect the saved identity, token expiry,
history, errors, and progress. Connection status reads local metadata only;
it does not verify credentials over the network or refresh them. The worker
verifies credentials before use and refreshes them near expiry during normal
polling, saving each renewal atomically before use. Preserve this renewable
file across restarts. Renewal checks run even on quiet polls with no new posts.
Credential failures use a persisted cooldown and backoff, respecting provider
Retry-After; the dashboard shows the error and authorization retry time.
Account changes, invalid credentials, or expired tokens
fail closed. Do not replace a saved renewed token with an older environment token.

### Reconnect after expired or revoked authorization

Obtain fresh authorization for the original Threads account through Meta, then
use the supported recovery flag inside the existing container:

```sh
docker compose exec masto-mirror python -m app.threads_setup --reconnect
```

Reconnect accepts replacement credentials from private configuration or hidden
prompts. It verifies `/me` against the originally pinned account before replacing
the private token file. A different account is rejected. Keep the existing token
file and database checkpoints in place so the original binding remains available;
do not delete them to work around an expired or revoked token. The command
prints identity and expiry and publishes no posts. First setup uses no flag.

This feature uses existing dependencies. A source-only update requires a
container restart, with no image rebuild. Deploy initially with Threads disabled
while Meta registration is unfinished; only authorize and activate after the
intended account is ready. Recreate the service after changing Compose
environment values.

## Eligible content and images

Only future public Mastodon posts from Threads' separately persisted first
activation are eligible. Earlier Bluesky successes are not backfilled into
Threads. Preserve the activation cutoff in SQLite across restarts, disabling,
and upgrades. Discovery still examines a bounded recent timeline; enabling
Threads does not introduce a historical import.

Threads supports public text and links, up to four photos with alt text, and
eligible self-replies. It splits long text into readable ordered parts with
a 500-character budget, while Bluesky keeps its original 300-character budget.
Reply preferences still apply; a Threads self-reply needs its own successfully
mirrored parent, and missing parents are not backfilled. Private, unlisted,
followers-only, and direct statuses and boosts are excluded.

Threads image containers reference the original public Mastodon attachment
URLs. Meta must be able to fetch those URLs without a login; keep the source
attachments available while media processing completes. No public NAS endpoint
or separate NAS media hosting is needed. Saved child/container IDs and readiness
checks protect image and carousel publication; an image failure is not reported
as successful text-only publication.

Video mirroring, edit/deletion synchronization, native quote embeds, historical
backfill, and grapheme-aware text counting remain unsupported. Supported text
and photos can still mirror when an unsupported attachment is omitted.

## Retries and uncertain publication

Definite failures use persisted, bounded exponential backoff, respecting server
Retry-After. The dashboard can queue a retry for a `failed` or `queued` Threads
job while Threads is enabled. Retry clears the attempt count and error and
wakes the worker, while preserving a future scheduled not-before time so a
click cannot shorten the provider's Retry-After. Absent or past deadlines clear.
It preserves all frozen payloads, account
bindings, child/container IDs, completed parts, and published post IDs.

Threads publication has no Bluesky-style client record key. A publish may have
succeeded even if its response was lost or the process crashed. Recovery checks
the saved container; if it cannot establish the published post ID, the job is
held as `needs_review`. Never blindly retry, create another container, delete
checkpoints, or reset the database to resolve such a job. Preserve the state,
review the saved container and destination account, and establish what happened
before any manual recovery. Successful, in-progress, ambiguous, and disabled
jobs reject manual retry with HTTP `409`.

The authenticated APIs default to Bluesky. Use `?destination=threads` with
`GET /dashboard/stats`, `GET /dashboard/history`, or
`POST /dashboard/retry/{mastodon_id}` for Threads. The dashboard uses the same
six history columns and keeps retry controls tied to the selected destination.
It displays server values as text and exposes no credential form.

Back up the database, private `.env`, and renewable token file together,
including the pinned account identity. See [configuration](configuration.md)
and [backup and restore](backup.md).
