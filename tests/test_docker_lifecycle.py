from __future__ import annotations

import json
from pathlib import Path
import subprocess
import pytest
from ops import docker_setup as ds
from ops import docker_backup as db


def _container(root, port=5055):
    return {'Config': {'Labels': {'com.docker.compose.project.working_dir': str(root), 'com.docker.compose.service': 'backend'}},
            'State': {'Running': True}, 'NetworkSettings': {'Ports': {'5055/tcp': [{'HostIp': '127.0.0.1', 'HostPort': str(port)}]}}}


def test_owned_ports_are_adopted_from_docker_not_unknown_listeners():
    assert ds._running_ports([_container(ds.ROOT, 5522)], production=True) == (5522, 5522)
    assert ds._running_ports([_container(ds.ROOT)], production=False) is None


def test_other_checkout_is_never_taken_over(tmp_path, monkeypatch):
    values = iter(['container-id', json.dumps([_container(tmp_path)])])
    monkeypatch.setattr(ds, '_run', lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=next(values), stderr=''))
    with pytest.raises(RuntimeError, match='another checkout'):
        ds.owned_containers()


def test_proxy_failure_cannot_be_ready(monkeypatch):
    def fake(url, timeout=2):
        if ':5173/api/' in url:
            return 502, None
        if url.endswith('/api/auth/status'):
            return 200, {'configured': True}
        if url.endswith('/api/phone/devices'):
            return 401, None
        return 200, None
    monkeypatch.setattr(ds, '_http_status', fake)
    assert ds.readiness(5055, 5173)['ready'] is False


def test_project_names_and_state_boundary(monkeypatch):
    monkeypatch.setenv('D3M0N_COMPOSE_PROJECT', 'invalid project;')
    with pytest.raises(ValueError):
        ds.project_name()
    monkeypatch.setenv('D3M0N_DOCKER_STATE_DIR', str(ds.ROOT / '.private'))
    with pytest.raises(ValueError, match='outside'):
        ds._state_root()


def test_backup_requires_secret_acknowledgement(tmp_path):
    with pytest.raises(ValueError, match='include-secrets'):
        db.backup(tmp_path / 'backup.tar.gz', include_secrets=False)


def test_restore_cannot_overwrite_current_project(tmp_path):
    with pytest.raises(ValueError, match='different'):
        db.restore(tmp_path / 'backup.tar.gz', ds.project_name(), include_secrets=True)


def test_backend_image_has_no_unpinned_fallback():
    text = (ds.ROOT / 'Dockerfile.dev').read_text()
    assert 'WARNING' not in text
    assert '--no-build-isolation --no-deps -e .' in text
    assert 'requirements/build.lock' in text
    assert 'scripts/install_platform_tools.py' in text
    assert 'ops/install-compatibility.json' in text
