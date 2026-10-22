from pathlib import Path
import pytest
from gitd.runtime_paths import runtime_directory


def test_configured_state_does_not_write_source(tmp_path, monkeypatch):
    state = tmp_path / 'private'
    legacy = tmp_path / 'unwritable-source' / 'results'
    monkeypatch.setenv('D3M0N_STATE_DIR',str(state))
    assert runtime_directory('results',legacy) == state / 'results'
    assert not legacy.exists()


def test_native_legacy_path_is_preserved_without_state_override(tmp_path, monkeypatch):
    monkeypatch.delenv('D3M0N_STATE_DIR',raising=False)
    legacy = tmp_path / 'existing-results'
    legacy.mkdir()
    (legacy / 'sentinel.txt').write_text('preserved')
    assert runtime_directory('results',legacy) == legacy
    assert (legacy / 'sentinel.txt').read_text() == 'preserved'


@pytest.mark.parametrize('name',['..','../outside','/outside','nested/path','nested\\path'])
def test_rejects_runtime_directory_escape(tmp_path,name):
    with pytest.raises(ValueError):
        runtime_directory(name,tmp_path / 'legacy')


def test_import_storage_modules_use_configured_state(tmp_path, monkeypatch):
    # Spawn a clean process so module import paths are not cached from earlier tests.
    import os, subprocess, sys
    env = os.environ.copy()
    env['D3M0N_STATE_DIR'] = str(tmp_path)
    env['DB_PATH'] = str(tmp_path / 'gitd.db')
    code = '''from pathlib import Path
from gitd.routers.explorer import _EXPLORER_DIR
from gitd.routers.tests import _TR_RECORDINGS_DIR
from gitd.services.phone_recording import RECORDINGS_DIR
from gitd.benchmarks.runner import RESULTS_DIR
import os
root=Path(os.environ['D3M0N_STATE_DIR'])
assert all(root in path.parents for path in [_EXPLORER_DIR,_TR_RECORDINGS_DIR,RECORDINGS_DIR,RESULTS_DIR])
'''
    result=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
