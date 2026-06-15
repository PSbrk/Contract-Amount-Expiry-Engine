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
