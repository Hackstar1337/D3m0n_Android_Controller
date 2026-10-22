from __future__ import annotations

import json
from pathlib import Path
import tarfile

import pytest

from ops.state_backup import create_backup, restore_backup, validate_backup


def test_backup_restore_round_trip_to_new_target(tmp_path: Path):
    source = tmp_path / "state"
    source.mkdir()
    (source / "dashboard-auth.json").write_text('{"hash":"private"}', encoding="utf-8")
    (source / "nested").mkdir()
    (source / "nested" / "settings.json").write_text("{}", encoding="utf-8")
    archive = tmp_path / "state.tar.gz"
    target = tmp_path / "restored"
    assert create_backup(source, archive) == archive
    manifest = validate_backup(archive)
    assert manifest["schema"] == 1
    assert restore_backup(archive, target) == target
    assert (target / "dashboard-auth.json").read_text(encoding="utf-8") == '{"hash":"private"}'
    assert (target / "nested" / "settings.json").is_file()


def test_restore_never_overwrites_existing_target(tmp_path: Path):
    source = tmp_path / "state"
    source.mkdir()
    (source / "one").write_text("1", encoding="utf-8")
    archive = create_backup(source, tmp_path / "state.tar.gz")
    target = tmp_path / "existing"
    target.mkdir()
    with pytest.raises(FileExistsError):
        restore_backup(archive, target)
    assert not any(target.iterdir())


def test_tampered_manifest_or_member_is_rejected(tmp_path: Path):
    source = tmp_path / "state"
    source.mkdir()
    (source / "one").write_text("1", encoding="utf-8")
    archive = create_backup(source, tmp_path / "state.tar.gz")
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(archive, "r:gz") as source_tar, tarfile.open(tampered, "w:gz") as target_tar:
        for member in source_tar.getmembers():
            stream = source_tar.extractfile(member) if member.isfile() else None
            if member.name == "one":
                member.size = 1
                target_tar.addfile(member, __import__("io").BytesIO(b"2"))
            else:
                target_tar.addfile(member, stream)
    with pytest.raises(ValueError):
        validate_backup(tampered)
