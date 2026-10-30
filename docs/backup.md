# Backup and restore

The `data/` directory contains SQLite mappings, thread checkpoints,
activation cutoffs, and any local recovery journals. Keep it together with
the populated `.env` in a private backup outside the repository.

## Simple consistent backup

1. Stop the Compose service:

   ```sh
   docker compose stop masto-mirror
   ```

2. Copy the entire `data/` directory and your private `.env` to your backup
   destination, including any SQLite sidecar files. Protect that destination
   because `.env` contains credentials and the database contains post data.
3. Restart the service:

   ```sh
   docker compose start masto-mirror
   ```

For a live backup, use SQLite's online backup API on the Docker host. Avoid
editing a running SQLite database through an SMB or other network mount.
Startup creates an automatic online backup before its legacy thread-table
migration, under `data/backups/`.

## Restore

Stop the service, restore `data/` and `.env`, verify container access to the
files, then start it with the same account pair. Preserve saved URI/CID
mappings and planned record keys. Do not delete mappings to force a retry.
The worker reconciles stored keys with Bluesky when resuming a job.

Git ignores `.env`, runtime data, databases, and backups. The public
repository contains only a blank configuration template and synthetic tests.
