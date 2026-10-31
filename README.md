# Mastodon to Bluesky and Threads Mirror

A self-hosted, single-user crossposter that automatically mirrors public
Mastodon posts to Bluesky and, optionally, Threads. Built with Python, FastAPI,
SQLite, and the documented platform APIs. Run it with Docker Compose, including
on a Synology NAS.

## Current features

- Polls the authenticated Mastodon account every 60 seconds by default.
- Mirrors public text and links into an existing Bluesky account.
- Optionally mirrors public text, links, up to four photos with alt text, and
  self-replies to Threads, with independent activation, progress, and retries.
- Uploads up to four images to Bluesky with their alt text, preserving aspect
  ratios, and prepares JPEG and PNG images within Bluesky's size limit.
- Splits long text into ordered posts on each destination, preferring paragraph,
  sentence, and clause boundaries, and keeping closing quotes and brackets with their text.
  Bluesky retains its 300-character budget; Threads uses 500 characters.
- Continues public self-replies under their mirrored parent, retaining the
  original conversation root, including when either post spans several parts.
- Keeps separate SQLite mappings and checkpoints for each destination:
  stable record keys and post URIs/CIDs for Bluesky, and media-container and
  published-post IDs for Threads. Uncertain publication responses are reconciled
  before retrying.
- Provides a password-protected history API and a dashboard with errors,
  progress, manual retry controls, and a destination selector. The Threads view
  includes local account and token-expiry status without revealing credentials.
- Checks JPEG and PNG processing during image builds and runtime health checks.

Private, followers-only, direct, and unlisted statuses are excluded. Boosts
are skipped. Replies to other accounts are disabled by default.

## Quick start

1. Download or clone the repository and enter its directory.
2. Copy the blank configuration template:

   ```sh
   cp .env.example .env
   ```

3. Fill in the Mastodon instance URL and access token, your Bluesky handle and
   app password, and a unique dashboard password. Keep the populated `.env`
   local; Git ignores it.
4. Start the Compose project:

   ```sh
   docker compose up -d --build
   ```

5. Open `http://localhost:8000/static/index.html`, or use the Docker host's
   hostname on your network. Log in with username `admin` and your configured
   `DASHBOARD_PASSWORD`.

The worker starts automatically. Original posts become eligible from its
first activation; self-replies have their own persisted activation cutoff.
Existing earlier posts are not automatically backfilled. A self-reply needs
an already-successful mapping for its source parent. Keep the same account
configuration and preserve `data/` across upgrades and restarts.

For Mastodon, use a read-only user token that can verify your account and
read statuses. See the official [Mastodon token guide](https://docs.joinmastodon.org/client/token/).
For Bluesky, create a dedicated app password in
[App Passwords](https://bsky.app/settings/app-passwords).

See [Synology setup](docs/synology.md), [configuration](docs/configuration.md),
[optional Threads setup](docs/threads.md), and [backup and restore](docs/backup.md).

## Add Threads

Threads support includes public text, links, up to four photos with alt text,
long posts split at natural boundaries within a 500-character budget, and
self-replies attached to their mirrored parent. Bluesky and Threads have
separate account bindings, activation times, history, and retry state.

1. Register a Meta developer app with the **Access the Threads API** use case.
   Add your own Threads account as a tester, accept the invitation in Threads,
   and authorize the publishing permissions described in [Threads setup](docs/threads.md).
   Generate a user token from the **User Token Generator** in the app's Threads
   use-case settings.
2. Keep `THREADS_ENABLED=false` in your private `.env`. Uncomment and fill
   `THREADS_ACCESS_TOKEN`. For a short-lived token, also fill the Threads-specific
   `THREADS_APP_SECRET`. If Meta has already issued a long-lived token, fill
   `THREADS_TOKEN_EXPIRES_AT` with its actual absolute Unix expiry in seconds;
   use Meta's reported expiry rather than calculating a new lifetime.
3. Run the setup helper inside the existing container:

   ```sh
   docker compose exec masto-mirror python -m app.threads_setup
   ```

4. Verify the displayed Threads identity, set `THREADS_ENABLED=true`, and
   recreate the service to load the updated environment:

   ```sh
   docker compose up -d --no-build --force-recreate masto-mirror
   ```

5. Select **Threads** in the dashboard to view its connection status, token
   expiry, synchronization history, errors, and retry controls. Create a new
   public Mastodon post to test both destinations.

The setup helper accepts private configuration or hidden prompts, exchanges a
short-lived token or imports an existing long-lived one, verifies `/me`, and
pins the account ID. It saves renewable credentials atomically to private
`data/threads-token.json` with mode `0600`, prints only identity and expiry, and
publishes no posts. Normal polling renews the saved token near expiry, even
when no new posts arrive. Preserve this file across restarts and backups.
For expired or revoked credentials, keep the original account binding and run
`docker compose exec masto-mirror python -m app.threads_setup --reconnect`.
Reconnect verifies the original account before replacing the private token file.
First setup uses no flag. See [Threads setup](docs/threads.md) for the full flow.

Only posts after Threads' own persisted activation time are eligible; enabling
it does not replay earlier Bluesky successes. Threads photos reference public
Mastodon attachment URLs, so no public NAS media hosting is required. Definite
failures use bounded backoff; an unknown publication result or crash can hold
a job as `needs_review` to prevent duplicate posts. Preserve its checkpoints
and review the destination before recovery; never blindly retry ambiguous jobs.

## Architecture and updates

The Compose service runs one Uvicorn process and one polling worker. Source
is mounted at `/app`; durable SQLite state is mounted at `/app/data`.
Dependency installation and its image-processing probe live outside that
source mount at `/opt/masto-mirror`.

Restart the container after source-only changes. Rebuild and recreate it
when the Dockerfile or requirements change. On compatible Synology systems,
`rebuild.sh` performs the build, recreation, and health verification using
the existing `masto-mirror` Compose project and service names.

Optional Threads support uses existing dependencies, so this source-only update
needs no image rebuild. Deploy with `THREADS_ENABLED=false` while Meta registration
and authorization are pending; authorize first, then activate future posts.
Recreate the service when changing its Compose environment values.

Avoid running multiple workers or replicas against the same source and
destination accounts.
Use your own credentials only after reviewing the configuration; this version
publishes eligible posts automatically and has no preview-only mode.

## Tests

Use Python 3.11:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q
node tests/dashboard.test.cjs
```

Node is needed only for the dashboard checks. Tests use synthetic accounts,
post IDs, and substituted API boundaries; no live account credentials are
required. An [example GitHub Actions workflow](docs/github-actions.yml) is
included; see [CI setup](docs/ci.md) to enable it.

## Current limits

This is a working initial version. Historical backfill, a publishing preview,
streaming, video mirroring, edit synchronization, deletion synchronization,
and verified mention mapping are not implemented. Unsupported video
attachments are not uploaded; supported text and images can still mirror.
Native quote embeds are not recreated.

Timeline discovery currently examines the latest 20 statuses, plus saved
pending and queued jobs. It can miss older unseen posts after a long outage
or a large burst. Self-replies wait while their mapped parent is incomplete;
parents are not automatically backfilled. The text splitter counts Python
characters rather than Unicode grapheme clusters, so complex emoji can split
awkwardly. Configuration is managed through `.env`, rather than dashboard
settings forms. Legacy ambiguous attempts require manual review.

## License

[MIT](LICENSE). This is an independent implementation using supported APIs.
