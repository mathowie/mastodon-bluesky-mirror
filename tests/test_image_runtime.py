"""A deployed image must prove that it can actually process photographs."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx

from app import main
from app.core import image_runtime

ROOT = Path(__file__).resolve().parents[1]


def test_installed_pillow_supports_real_jpeg_and_png_encoding():
    status = image_runtime.image_status()
    assert status['ready'] is True
    assert status['pillow_version']
    assert status['error'] is None


def test_build_probe_fails_when_pillow_cannot_be_loaded(tmp_path):
    package = tmp_path / 'PIL'
    package.mkdir()
    (package / '__init__.py').write_text('raise ImportError("test native library failure")\n')
    result = subprocess.run(
        [sys.executable, str(ROOT / 'app/core/image_runtime.py')],
        env={**os.environ, 'PYTHONPATH': str(tmp_path)}, capture_output=True, text=True,
    )
    assert result.returncode == 1
    status = json.loads(result.stdout)
    assert status['ready'] is False
    assert 'recreate' in status['error'].lower()
    assert 'test native library failure' not in result.stdout


def test_health_reports_unavailable_image_processing(monkeypatch):
    state = {'ready': False, 'pillow_version': None, 'error': 'Rebuild and recreate the container'}
    monkeypatch.setattr(main, 'image_status', lambda: state)

    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            return await client.get('/health')

    response = asyncio.run(request())
    assert response.status_code == 503
    assert response.json()['status'] == 'degraded'
    assert response.json()['image_processing'] == state


def test_health_reports_ready_image_processing(monkeypatch):
    state = {'ready': True, 'pillow_version': 'test', 'error': None}
    monkeypatch.setattr(main, 'image_status', lambda: state)

    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            return await client.get('/health')

    response = asyncio.run(request())
    assert response.status_code == 200
    assert response.json()['status'] == 'ok'
    assert response.json()['image_processing'] == state


def test_build_probe_exits_successfully_with_a_working_image_library():
    result = subprocess.run([sys.executable, str(ROOT / 'app/core/image_runtime.py')], capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout)['ready'] is True
