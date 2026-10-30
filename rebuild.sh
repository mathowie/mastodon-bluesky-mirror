#!/bin/sh
# Run on the Synology NAS: sudo sh /volume1/docker/mastodon-bluesky-mirror/rebuild.sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$project_dir"

if docker compose version >/dev/null 2>&1; then
    compose() { docker compose -f "$project_dir/docker-compose.yaml" -p masto-mirror "$@"; }
elif command -v docker-compose >/dev/null 2>&1; then
    compose() { docker-compose -f "$project_dir/docker-compose.yaml" -p masto-mirror "$@"; }
else
    echo "Docker Compose is unavailable. Run this script on the Synology NAS with Container Manager installed." >&2
    exit 1
fi

# A failed build stops here and leaves the current container running.
# Synology runc may lack the seccomp support required by BuildKit.
# Use Docker's classic builder for this build only.
DOCKER_BUILDKIT=0 compose build --no-cache masto-mirror
compose up -d --no-deps --force-recreate masto-mirror
docker exec masto-bsky-mirror python /opt/masto-mirror/image_runtime.py
# Wait for startup (including recovery-journal import), then verify HTTP health.
docker exec -i masto-bsky-mirror python - <<'PY'
import json
import time
import urllib.request
for attempt in range(30):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3) as response:
            status = json.load(response)
        if status.get('status') == 'ok' and status.get('image_processing', {}).get('ready'):
            print('Container ready: image processing works and the dashboard is healthy.')
            break
    except (OSError, ValueError):
        pass
    time.sleep(2)
else:
    raise SystemExit('Container did not become ready. Check Container Manager logs; do not retry published posts.')
PY
