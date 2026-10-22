import json
from pathlib import Path
from scripts import update_dependency_locks as locks

ROOT = Path(__file__).resolve().parents[1]


def test_checked_in_locks_have_pins_hashes_and_universal_markers():
    for name in ['backend.lock', 'dev.lock', 'build.lock']:
        locks.validate_lock(ROOT / 'requirements' / name)


def test_overlay_survives_linux_generated_lock(tmp_path):
    target = tmp_path / 'backend.lock'
    target.write_text('uvicorn==0.54.0 \\\n    --hash=sha256:' + 'a'*64 + '\n    # via project\n')
    overlays = json.loads(locks.OVERLAYS.read_text())
    locks.apply_platform_overlay(target, overlays)
    locks.validate_lock(target)
    text = target.read_text()
    assert text.count('pywin32==') == 1
    assert text.count('uvloop==') == 1
    locks.apply_platform_overlay(target, overlays)
    assert target.read_text() == text


def test_overlay_survives_windows_generated_lock(tmp_path):
    target = tmp_path / 'dev.lock'
    target.write_text('pywin32==312 \\\n    --hash=sha256:' + 'b'*64 + '\n    # via docker\nuvicorn==0.54.0 \\\n    --hash=sha256:' + 'a'*64 + '\n    # via project\n')
    locks.apply_platform_overlay(target)
    locks.validate_lock(target)
    assert target.read_text().count('pywin32==') == 1


def test_validation_rejects_missing_hash(tmp_path):
    import pytest
    target = tmp_path / 'build.lock'
    target.write_text('setuptools==84.2.1\n')
    with pytest.raises(ValueError, match='Unhashed'):
        locks.validate_lock(target)
