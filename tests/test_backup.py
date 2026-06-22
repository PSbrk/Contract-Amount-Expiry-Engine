"""Tests for the Phase-4 OneDrive backup helper.

The helper is small but its failure semantics are load-bearing: a backup
failure must NEVER fail the ingest run, because the local data/engine.db
is the source of truth and the next successful run will retry the copy.
These tests pin that contract.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

from engine.main import _backup_database_safely


def _make_fake_db(tmp_path: Path, name: str = "engine.db") -> Path:
    p = tmp_path / name
    p.write_bytes(b"SQLite-format-3-fake-bytes")
    return p


def test_backup_skipped_when_path_is_none(tmp_path, caplog):
    src = _make_fake_db(tmp_path)
    # No raise, no warning — backup is just a no-op.
    with caplog.at_level(logging.WARNING):
        _backup_database_safely(src, None)
    assert caplog.records == []


def test_backup_skipped_when_path_is_empty_string(tmp_path, caplog):
    src = _make_fake_db(tmp_path)
    # Empty string is the "unset" sentinel coming from settings.py
    # (`os.environ.get(...).strip() or None` collapses "" to None at the
    # settings layer, but the helper handles "" defensively anyway).
    with caplog.at_level(logging.WARNING):
        _backup_database_safely(src, "")
    assert caplog.records == []


def test_backup_copies_db_to_destination(tmp_path):
    src = _make_fake_db(tmp_path)
    dest = tmp_path / "onedrive" / "engine.db"
    _backup_database_safely(src, str(dest))
    assert dest.exists()
    assert dest.read_bytes() == src.read_bytes()


def test_backup_uses_copy2_to_preserve_mtime(tmp_path):
    src = _make_fake_db(tmp_path)
    # Backdate the source so we can prove copy2 (not copy) is being used.
    import os
    old_ts = src.stat().st_mtime - 86400
    os.utime(src, (old_ts, old_ts))

    dest = tmp_path / "backup" / "engine.db"
    _backup_database_safely(src, str(dest))

    src_mtime = src.stat().st_mtime
    dest_mtime = dest.stat().st_mtime
    assert abs(dest_mtime - src_mtime) < 1.0  # copy2 preserves mtime


def test_backup_creates_parent_directory_if_missing(tmp_path):
    src = _make_fake_db(tmp_path)
    # Three-level nested dest dir that doesn't exist yet.
    dest = tmp_path / "a" / "b" / "c" / "engine.db"
    assert not dest.parent.exists()
    _backup_database_safely(src, str(dest))
    assert dest.parent.is_dir()
    assert dest.exists()


def test_backup_failure_does_not_raise(tmp_path, caplog, monkeypatch):
    src = _make_fake_db(tmp_path)
    dest = tmp_path / "backup" / "engine.db"

    def _boom(*_args, **_kwargs):
        raise OSError("simulated disk full")

    monkeypatch.setattr(shutil, "copy2", _boom)

    with caplog.at_level(logging.WARNING):
        # The whole point: this must NOT raise.
        _backup_database_safely(src, str(dest))

    # And the failure must be logged as a warning so an operator tailing
    # logs can see something went wrong.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "backup" in msg.lower()
    assert "simulated disk full" in msg


def test_backup_failure_when_source_missing_does_not_raise(tmp_path, caplog):
    missing = tmp_path / "does-not-exist.db"
    dest = tmp_path / "backup" / "engine.db"

    with caplog.at_level(logging.WARNING):
        _backup_database_safely(missing, str(dest))

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "backup" in warnings[0].getMessage().lower()


def test_backup_accepts_path_object_for_db_path(tmp_path):
    """DEFAULT_DB_PATH is a pathlib.Path; the helper must accept it directly
    without the caller having to str() it."""
    src = _make_fake_db(tmp_path)
    dest = tmp_path / "backup.db"
    # Pass src as a Path, not a string.
    _backup_database_safely(Path(src), str(dest))
    assert dest.exists()


# ---------------------------------------------------------------------------
# Phase 12: _restore_database_safely — pull from OneDrive on startup
# ---------------------------------------------------------------------------

from engine.main import _restore_database_safely


def _touch_mtime(p: Path, offset_seconds: float) -> None:
    """Shift a file's mtime by offset_seconds (negative = older)."""
    import os
    new = p.stat().st_mtime + offset_seconds
    os.utime(p, (new, new))


def test_restore_no_backup_path_is_no_op(tmp_path):
    local = _make_fake_db(tmp_path)
    result = _restore_database_safely(local, None)
    assert result["action"] == "no_backup_path"
    # Local untouched.
    assert local.read_bytes() == b"SQLite-format-3-fake-bytes"


def test_restore_cloud_missing_logs_and_keeps_local(tmp_path):
    local = _make_fake_db(tmp_path)
    cloud = tmp_path / "onedrive" / "engine.db"  # doesn't exist
    result = _restore_database_safely(local, str(cloud))
    assert result["action"] == "cloud_missing"
    assert local.read_bytes() == b"SQLite-format-3-fake-bytes"


def test_restore_pulls_when_local_missing(tmp_path):
    onedrive = tmp_path / "onedrive"
    onedrive.mkdir()
    cloud = onedrive / "engine.db"
    cloud.write_bytes(b"cloud-content")
    local = tmp_path / "data" / "engine.db"
    assert not local.exists()
    result = _restore_database_safely(local, str(cloud))
    assert result["action"] == "local_missing_pulled"
    assert local.exists()
    assert local.read_bytes() == b"cloud-content"


def test_restore_pulls_when_cloud_is_newer(tmp_path):
    local = tmp_path / "data" / "engine.db"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"local-stale")
    cloud = tmp_path / "onedrive" / "engine.db"
    cloud.parent.mkdir(parents=True)
    cloud.write_bytes(b"cloud-fresh")
    # Make cloud 30 seconds newer than local (well outside grace).
    _touch_mtime(local, -30)
    result = _restore_database_safely(local, str(cloud))
    assert result["action"] == "restored"
    assert local.read_bytes() == b"cloud-fresh"


def test_restore_does_not_overwrite_when_local_newer(tmp_path):
    local = tmp_path / "data" / "engine.db"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"local-fresh")
    cloud = tmp_path / "onedrive" / "engine.db"
    cloud.parent.mkdir(parents=True)
    cloud.write_bytes(b"cloud-stale")
    # Backdate cloud by 30 seconds.
    _touch_mtime(cloud, -30)
    result = _restore_database_safely(local, str(cloud))
    assert result["action"] == "local_newer"
    # Local untouched.
    assert local.read_bytes() == b"local-fresh"


def test_restore_no_op_when_in_sync(tmp_path):
    local = tmp_path / "data" / "engine.db"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"identical-content")
    cloud = tmp_path / "onedrive" / "engine.db"
    cloud.parent.mkdir(parents=True)
    cloud.write_bytes(b"identical-content")
    # Align mtimes exactly.
    import os
    t = local.stat().st_mtime
    os.utime(cloud, (t, t))
    result = _restore_database_safely(local, str(cloud))
    assert result["action"] == "in_sync"
    assert local.read_bytes() == b"identical-content"


def test_restore_failure_does_not_raise(tmp_path, monkeypatch, caplog):
    """A broken copy must NEVER crash the engine — local DB is source of truth."""
    onedrive = tmp_path / "onedrive"
    onedrive.mkdir()
    cloud = onedrive / "engine.db"
    cloud.write_bytes(b"cloud-content")
    local = tmp_path / "data" / "engine.db"  # missing → would trigger pull

    def _boom(*_args, **_kwargs):
        raise OSError("simulated disk full on restore")

    monkeypatch.setattr(shutil, "copy2", _boom)

    with caplog.at_level(logging.WARNING):
        result = _restore_database_safely(local, str(cloud))
    assert result["action"] == "failed"
    # And a clear warning logged.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) >= 1
    assert "restore" in warnings[0].getMessage().lower()
