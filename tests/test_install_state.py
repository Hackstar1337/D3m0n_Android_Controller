from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops.install_state import (
    StageLogger,
    atomic_promote_directory,
    fingerprint_tree,
    load_install_state,
    mark_stage,
    new_install_state,
    recover_atomic_promotion,
    save_install_state,
    stage_complete,
)


def test_stage_state_requires_matching_fingerprint_and_output(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    output = repo / "output.txt"
    output.write_text("ok", encoding="utf-8")
    state = new_install_state(repo)
    mark_stage(state, "frontend_build", "fp-1", outputs=[output], root=repo)
    assert stage_complete(state, "frontend_build", "fp-1", root=repo)
    assert not stage_complete(state, "frontend_build", "fp-2", root=repo)
    output.write_text("changed", encoding="utf-8")
    assert not stage_complete(state, "frontend_build", "fp-1", root=repo)
    state_path = tmp_path / "state.json"
    save_install_state(state, state_path)
    assert load_install_state(state_path, expected_repo=repo)["schema"] == 2


def test_stage_logger_redacts_secret_values(tmp_path: Path):
    with StageLogger(tmp_path, "frontend_build") as logger:
        logger.write("password=supersecret token:abc123 bearer XYZ")
    text = (tmp_path / "install-frontend_build.log").read_text(encoding="utf-8")
    assert "supersecret" not in text
    assert "abc123" not in text
    assert "[REDACTED]" in text


def test_atomic_promotion_and_recovery(tmp_path: Path):
    destination = tmp_path / "dist"
    destination.mkdir()
    (destination / "index.html").write_text("old", encoding="utf-8")
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "index.html").write_text("new", encoding="utf-8")
    runtime = tmp_path / "runtime"
    atomic_promote_directory(staging, destination, runtime)
    assert (destination / "index.html").read_text(encoding="utf-8") == "new"
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "index.html").write_text("old", encoding="utf-8")
    destination.rename(tmp_path / "interrupted-new")
    (runtime / "frontend-promotion.json").write_text(json.dumps({
        "destination": str(destination), "backup": str(backup), "staging": str(tmp_path / "unused")
    }), encoding="utf-8")
    assert recover_atomic_promotion(runtime, destination=destination)
    assert (destination / "index.html").read_text(encoding="utf-8") == "old"


def test_unknown_stage_rejected(tmp_path: Path):
    with pytest.raises(ValueError):
        StageLogger(tmp_path, "unknown")


def test_virtualenv_interpreter_symlink_is_recorded_by_checkout_location(tmp_path):
    from ops.install_state import output_records, outputs_valid
    repo = tmp_path / 'checkout'
    binary = repo / '.venv' / 'bin' / 'python'
    binary.parent.mkdir(parents=True)
    interpreter = tmp_path / 'system-python'
    interpreter.write_bytes(b'fake-interpreter-v1')
    try:
        binary.symlink_to(interpreter)
    except (PermissionError, NotImplementedError, OSError):
        pytest.skip('Host does not allow creating a test symlink.')
    records = output_records([binary],root=repo)
    assert Path(records[0]['path']) == Path('.venv/bin/python')
    assert outputs_valid(records,root=repo)
    interpreter.write_bytes(b'fake-interpreter-v2')
    assert not outputs_valid(records,root=repo)


def test_output_record_does_not_allow_lexically_external_paths(tmp_path):
    from ops.install_state import output_records
    repo=tmp_path / 'checkout'
    repo.mkdir()
    external=tmp_path / 'outside'
    external.write_text('not-an-owned-output')
    with pytest.raises(ValueError):
        output_records([external],root=repo)
