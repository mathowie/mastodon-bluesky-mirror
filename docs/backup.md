# Backup and restore

The `data/` directory contains SQLite mappings, thread checkpoints,
activation cutoffs, and any local recovery journals for both destinations.
It also contains the private renewable Threads token file, by default
`data/threads-token.json`, with the pinned account identity and token expiry.
Keep these together with the populated `.env` in a private backup outside the
repository. If `THREADS_TOKEN_FILE` points elsewhere, include that file too.

## Simple consistent backup

1. Stop the Compose service:

   ```sh
   docker compose stop masto-mirror
   ```

2. Copy the entire `data/` directory and your private `.env` to your backup
   destination, including any SQLite sidecar files. Protect that destination
   because `.env` and the Threads token file contain credentials and the
   database contains post data. Preserve the token file's mode `0600` and its
   ownership so the container can read it and atomically replace it on renewal.
3. Restart the service:

   ```sh
   docker compose start masto-mirror
   ```

For a live database backup, use SQLite's online backup API inside the running
container, where the database is local. Capture the renewable token file and
private configuration as well; a database-only backup is incomplete. A stopped
backup is the simplest way to capture all three consistently. Avoid
editing a running SQLite database through an SMB or other network mount.
Startup creates an automatic SQLite backup before adding legacy thread tables
or the new Threads tables, under `data/backups/`. Those migration backups
contain only the database, not renewable credentials or `.env`.

## Restore

Stop the service, restore `data/` and `.env`, verify container access to the
files, then start it with the same source and destination accounts. Preserve saved URI/CID
mappings and planned record keys. Do not delete mappings to force a retry.
The worker reconciles stored keys with Bluesky when resuming a job.

For Threads, restore the token file, its pinned account ID, both Threads tables,
all part/container/post IDs, and the persisted Threads activation cutoff as a
unit. Retain the private file's `0600` permissions and ensure its directory is
writable for atomic renewal. Do not substitute credentials for another account
or reset activation to recover an old job. Check the saved identity and expiry
using [the setup helper](threads.md) before re-enabling publication. A backup
token may have expired or been revoked; retain the pinned identity and use
`docker compose exec masto-mirror python -m app.threads_setup --reconnect`
to verify fresh credentials for that original account before replacing the
private file. First setup uses no flag. Do not
overwrite current renewed credentials with a stale bootstrap token.

Definite Threads failures may resume with backoff and their saved checkpoints.
An uncertain publish or crash with an unresolved post ID is held as
`needs_review`; a restore must retain that hold. Review the saved container and
destination before recovery. Deleting checkpoints or manually forcing retry
can duplicate a post. Completed parts must survive every restore and retry.

Git ignores `.env`, runtime data, databases, and backups. The public
repository contains only a blank configuration template and synthetic tests.
