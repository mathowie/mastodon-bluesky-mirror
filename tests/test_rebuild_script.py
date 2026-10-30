"""NAS repair builds before replacing the container and stops on build failure."""
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def fake_docker(tmp_path):
    executable = tmp_path / 'docker'
    executable.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_CALLS"
case "$*" in
  *" build "*)
    if [ "${REQUIRE_LEGACY:-0}" = 1 ] && [ "${DOCKER_BUILDKIT:-1}" != 0 ]; then
      echo 'runc run failed: seccomp: config provided but seccomp not supported' >&2
      exit 1
    fi
    exit "${BUILD_EXIT:-0}" ;;
esac
exit 0
''')
    executable.chmod(0o755)
    log = tmp_path / 'calls'
    return {**os.environ, 'PATH': f'{tmp_path}:' + os.environ['PATH'], 'DOCKER_CALLS': str(log)}, log


def test_rebuild_installs_then_recreates_without_removing_data(tmp_path):
    env, log = fake_docker(tmp_path)
    result = subprocess.run(['sh', str(ROOT / 'rebuild.sh')], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    build = next(i for i, c in enumerate(calls) if ' build ' in c)
    replace = next(i for i, c in enumerate(calls) if ' up ' in c)
    assert build < replace
    assert '--force-recreate' in calls[replace]
    assert any(' exec masto-bsky-mirror python ' in ' ' + c for c in calls)
    assert not any(' down' in c or ' rm ' in c or '--volumes' in c for c in calls)


def test_failed_build_leaves_running_container_alone(tmp_path):
    env, log = fake_docker(tmp_path)
    result = subprocess.run(['sh', str(ROOT / 'rebuild.sh')], env={**env, 'BUILD_EXIT': '19'}, capture_output=True, text=True)
    assert result.returncode == 19
    assert not any(' up ' in c for c in log.read_text().splitlines())


def test_nas_rebuild_uses_legacy_builder_when_buildkit_cannot_start(tmp_path):
    env, log = fake_docker(tmp_path)
    result = subprocess.run(
        ['sh', str(ROOT / 'rebuild.sh')],
        env={**env, 'REQUIRE_LEGACY': '1', 'DOCKER_BUILDKIT': '1'},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert any(' up ' in c for c in calls)
    assert any(' exec masto-bsky-mirror python ' in ' ' + c for c in calls)
