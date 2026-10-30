# Synology Container Manager setup

1. Install Container Manager on a NAS that supports Docker.
2. Create a shared-folder directory such as
   `/volume1/docker/mastodon-bluesky-mirror`. Adjust the volume path for your
   NAS. Put the repository contents directly inside it, including `app/`,
   `tests/`, `Dockerfile`, and `docker-compose.yaml`.
3. Copy `.env.example` to `.env` and fill all required settings. A text editor
   must preserve the leading dot in the filename.
4. In Container Manager, create a Project named `masto-mirror`, select that
   directory, and use its existing `docker-compose.yaml`.
5. Build and start the project. Open the NAS hostname on port `8000` at
   `/static/index.html`. Use dashboard username `admin` and the password you
   configured.

The supplied Compose file retains data under `./data`. It also mounts the
project source directory, allowing source-only updates with a container
restart. Keep one running service for each configured account pair.

## Rebuilding through SSH

When dependencies change, run the supplied helper on the NAS:

```sh
sudo sh /volume1/docker/mastodon-bluesky-mirror/rebuild.sh
```

It uses project/service `masto-mirror` and container `masto-bsky-mirror`.
It builds first, recreates the service only after a successful build, then
checks Pillow and HTTP health. Data is retained in its bind mount.

Some Synology Docker runtimes cannot start BuildKit steps that request
unsupported seccomp features. The helper selects Docker's classic builder
for its build command with `DOCKER_BUILDKIT=0`, without changing daemon
configuration. This helper targets engines that still support the classic
builder. On other hosts, use the standard Compose build command supported
by your Docker installation. See [Docker's builder documentation](https://docs.docker.com/engine/deprecated/#legacy-builder-for-linux-images).

A restarted old container keeps its installed libraries. Dependency updates
need a newly built image and container recreation. `/health` returns HTTP
503 if image processing is unavailable, even when the web server responds.

## Updating

Back up `data/` and your private `.env` first. Apply source updates and
restart the container; rebuild and recreate it for Dockerfile or dependency
updates. Preserve the database and activation cutoffs. Use the dashboard and
container logs to inspect failures before retrying.

For Docker CLI commands, use an SSH session on the NAS. The Mac or PC that
mounts a network share is a different machine from the Docker host.
