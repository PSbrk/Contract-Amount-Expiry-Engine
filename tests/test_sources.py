"""Tests for the TransactionSource Protocol implementations.

LocalFileSource is exercised directly against the on-disk fixture. The
AirtableInboxSource branches are tested by monkeypatching the airtable_client
helpers it calls (get_newest_unprocessed_inbox, download_attachment_bytes,
file_hash_already_processed) — no live Airtable needed.

LocalInboxSource is exercised against a tmp_path inbox directory and an
in-memory SQLite connection — no real disk path other than tmp_path is
touched.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from engine import airtable_client, ingest
from engine.sqlite_client import ensure_schema, insert_inbox_processed


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "transactions_sample.tsv"


# ---------------------------------------------------------------------------
# LocalFileSource
# ---------------------------------------------------------------------------

def test_local_file_source_returns_df_and_metadata():
    source = ingest.LocalFileSource(FIXTURE)
    df, meta = source.get_latest_transactions()

    assert len(df) == 15  # 15 data rows in the fixture; Grand Total dropped
    assert list(df.columns) == list(ingest.EXPECTED_COLUMNS)
    assert meta.name == "transactions_sample.tsv"
    assert meta.inbox_record_id is None


def test_local_file_source_hash_matches_file_bytes():
    source = ingest.LocalFileSource(FIXTURE)
    _, meta = source.get_latest_transactions()
    expected = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert meta.hash == expected


def test_local_file_source_received_iso_is_real_timestamp():
    """Regression for the Step 2 review finding that LocalFileSource set
    received_iso='' — divergent from AirtableInboxSource's contract."""
    source = ingest.LocalFileSource(FIXTURE)
    _, meta = source.get_latest_transactions()
    assert meta.received_iso, "received_iso must not be empty"
    assert "T" in meta.received_iso  # ISO 8601 timestamp format check


# ---------------------------------------------------------------------------
# AirtableInboxSource — three branches via monkeypatched helpers
# ---------------------------------------------------------------------------

def test_airtable_source_raises_no_new_when_inbox_empty(monkeypatch):
    monkeypatch.setattr(ingest, "get_newest_unprocessed_inbox", lambda base: None)

    source = ingest.AirtableInboxSource(base=object())
    with pytest.raises(ingest.NoNewTransactionsError, match="no unprocessed"):
        source.get_latest_transactions()


def test_airtable_source_raises_unusable_when_record_has_no_attachment(monkeypatch):
    """Spec §3 says the operator drops the file as an attachment. A record
    with no attachment is malformed and must be marked Processed by the
    main handler to avoid an unbounded re-detect loop on every run."""
    record = airtable_client.InboxRecord(
        id="recX",
        name="malformed.csv",
        created_time="2026-06-12T10:00:00.000Z",
        attachments=[],
        file_hash="",
        processed=False,
    )
    monkeypatch.setattr(ingest, "get_newest_unprocessed_inbox", lambda base: record)

    source = ingest.AirtableInboxSource(base=object())
    with pytest.raises(ingest.UnusableInboxRecordError) as exc_info:
        source.get_latest_transactions()
    assert exc_info.value.inbox_record_id == "recX"
    assert "no attachment" in exc_info.value.reason


def test_airtable_source_raises_duplicate_when_hash_already_processed(monkeypatch):
    """A second Inbox record carrying identical file bytes must be flagged as
    duplicate by content hash (not Airtable record id)."""
    data = FIXTURE.read_bytes()
    record = airtable_client.InboxRecord(
        id="recDup",
        name="resend.tsv",
        created_time="2026-06-12T11:00:00.000Z",
        attachments=[{"url": "https://example.invalid/signed", "filename": "resend.tsv"}],
        file_hash="",
        processed=False,
    )
    monkeypatch.setattr(ingest, "get_newest_unprocessed_inbox", lambda base: record)
    monkeypatch.setattr(ingest, "download_attachment_bytes", lambda att: data)
    monkeypatch.setattr(ingest, "file_hash_already_processed", lambda base, h: True)

    source = ingest.AirtableInboxSource(base=object())
    with pytest.raises(ingest.DuplicateTransactionsError) as exc_info:
        source.get_latest_transactions()
    assert exc_info.value.inbox_record_id == "recDup"
    assert exc_info.value.filename == "resend.tsv"
    # Hash carried verbatim so main can mark the Inbox row with a Notes
    # referencing it.
    assert exc_info.value.hash == hashlib.sha256(data).hexdigest()


def test_airtable_source_succeeds_when_record_is_fresh(monkeypatch):
    """Full happy path — record has attachment, hash is not a known dup."""
    data = FIXTURE.read_bytes()
    record = airtable_client.InboxRecord(
        id="recFresh",
        name="fresh.tsv",
        created_time="2026-06-12T12:00:00.000Z",
        attachments=[{"url": "https://example.invalid/signed", "filename": "fresh.tsv"}],
        file_hash="",
        processed=False,
    )
    monkeypatch.setattr(ingest, "get_newest_unprocessed_inbox", lambda base: record)
    monkeypatch.setattr(ingest, "download_attachment_bytes", lambda att: data)
    monkeypatch.setattr(ingest, "file_hash_already_processed", lambda base, h: False)

    source = ingest.AirtableInboxSource(base=object())
    df, meta = source.get_latest_transactions()

    assert len(df) == 15
    assert meta.inbox_record_id == "recFresh"
    assert meta.name == "fresh.tsv"
    assert meta.hash == hashlib.sha256(data).hexdigest()
    assert meta.received_iso == "2026-06-12T12:00:00.000Z"


# ---------------------------------------------------------------------------
# LocalInboxSource — folder scan + dedup against SQLite
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    """A fresh in-memory SQLite database with the engine schema applied."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    yield c
    c.close()


@pytest.fixture
def inbox_dirs(tmp_path):
    """Return (inbox_dir, processed_dir) as Paths under tmp_path."""
    inbox = tmp_path / "inbox"
    processed = tmp_path / "processed"
    inbox.mkdir()
    processed.mkdir()
    return inbox, processed


def _copy_fixture_into(inbox: Path, name: str, *, mtime: float | None = None) -> Path:
    """Copy the canonical TSV fixture into `inbox` with `name`; optionally
    pin mtime so the FIFO-by-mtime pick is deterministic."""
    dest = inbox / name
    shutil.copy(FIXTURE, dest)
    if mtime is not None:
        os.utime(dest, (mtime, mtime))
    return dest


def test_local_inbox_source_raises_no_new_when_empty(conn, inbox_dirs):
    inbox, processed = inbox_dirs
    source = ingest.LocalInboxSource(conn, inbox_dir=inbox, processed_dir=processed)
    with pytest.raises(ingest.NoNewTransactionsError, match="no unprocessed"):
        source.get_latest_transactions()


def test_local_inbox_source_returns_df_and_metadata_with_source_path(conn, inbox_dirs):
    inbox, processed = inbox_dirs
    file_path = _copy_fixture_into(inbox, "Q2.tsv")
    # Rename .tsv -> .csv so the source picks it up (.tsv is not in the
    # allowed-suffix list; only .csv and .xlsx are accepted).
    target = inbox / "Q2.csv"
    file_path.rename(target)

    source = ingest.LocalInboxSource(conn, inbox_dir=inbox, processed_dir=processed)
    df, meta = source.get_latest_transactions()

    assert len(df) == 15  # same 15-row fixture
    assert meta.name == "Q2.csv"
    assert meta.source_path == str(target)
    assert meta.inbox_record_id is None  # Airtable-only field; stays unset for local
    assert meta.hash == hashlib.sha256(target.read_bytes()).hexdigest()


def test_local_inbox_source_ignores_non_csv_xlsx_extensions(conn, inbox_dirs):
    inbox, processed = inbox_dirs
    # An operator-dropped README or a hidden .DS_Store must NOT be
    # picked up as if it were a Tableau export.
    (inbox / "README.md").write_text("hello")
    (inbox / ".DS_Store").write_bytes(b"\x00")
    (inbox / "notes.txt").write_text("hello")

    source = ingest.LocalInboxSource(conn, inbox_dir=inbox, processed_dir=processed)
    with pytest.raises(ingest.NoNewTransactionsError):
        source.get_latest_transactions()


def test_local_inbox_source_picks_oldest_by_mtime(conn, inbox_dirs):
    """Backlog draining is FIFO by mtime — if the operator drops three
    files, the engine processes the EARLIEST one first."""
    inbox, processed = inbox_dirs
    # Three files, mtimes ordered oldest → newest. Rename to .csv so the
    # source accepts them.
    base_mtime = time.time() - 3 * 86400  # three days ago
    p1 = inbox / "first.csv"
    p2 = inbox / "second.csv"
    p3 = inbox / "third.csv"
    shutil.copy(FIXTURE, p1)
    shutil.copy(FIXTURE, p2)
    shutil.copy(FIXTURE, p3)
    os.utime(p1, (base_mtime, base_mtime))
    os.utime(p2, (base_mtime + 86400, base_mtime + 86400))
    os.utime(p3, (base_mtime + 2 * 86400, base_mtime + 2 * 86400))

    source = ingest.LocalInboxSource(conn, inbox_dir=inbox, processed_dir=processed)
    _, meta = source.get_latest_transactions()
    assert meta.name == "first.csv"


def test_local_inbox_source_dedup_moves_duplicate_out_of_inbox(conn, inbox_dirs):
    """A file whose hash already lives in the Inbox table must be moved
    out of inbox/ (so subsequent runs don't busy-loop the warning) AND
    raise DuplicateTransactionsError so the caller can record it. The
    moved file lands in processed/_duplicate-<hash>-<name> rather than
    overwriting the real processed file."""
    inbox, processed = inbox_dirs
    target = inbox / "Q2.csv"
    shutil.copy(FIXTURE, target)
    file_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    # Seed the Inbox table with a prior row for this same hash.
    insert_inbox_processed(
        conn, name="earlier.csv", file_hash=file_hash,
        rows_in_scope=1, total_in_scope=1.0,
        processed_at_iso_date="2026-06-01",
        notes="seeded prior run",
    )

    source = ingest.LocalInboxSource(conn, inbox_dir=inbox, processed_dir=processed)
    with pytest.raises(ingest.DuplicateTransactionsError):
        source.get_latest_transactions()

    # The duplicate file is gone from inbox/.
    assert not target.exists()
    # And lives under processed/ with the _duplicate- prefix.
    moved_files = list(processed.iterdir())
    assert len(moved_files) == 1
    assert moved_files[0].name.startswith("_duplicate-")
    assert "Q2.csv" in moved_files[0].name


def test_local_inbox_source_move_to_processed(conn, inbox_dirs):
    """move_to_processed() is the success path — caller invokes it once
    the pipeline writes the Inbox row + Run Log row, atomically taking
    the file out of the queue."""
    inbox, processed = inbox_dirs
    target = inbox / "Q2.csv"
    shutil.copy(FIXTURE, target)

    source = ingest.LocalInboxSource(conn, inbox_dir=inbox, processed_dir=processed)
    dest = source.move_to_processed(str(target), file_hash="a" * 64)

    assert not target.exists()
    assert dest.exists()
    assert dest.parent == processed
    # Filename incorporates a 12-char hash prefix to avoid collisions
    # when the operator drops two files with the same name on different
    # weeks.
    assert dest.name == "aaaaaaaaaaaa-Q2.csv"


def test_local_inbox_source_creates_dirs_if_missing(tmp_path, conn):
    """A fresh install has no data/inbox or data/processed; the source
    must create them on first use rather than crash."""
    inbox = tmp_path / "fresh-inbox"
    processed = tmp_path / "fresh-processed"
    assert not inbox.exists()
    assert not processed.exists()

    source = ingest.LocalInboxSource(conn, inbox_dir=inbox, processed_dir=processed)
    with pytest.raises(ingest.NoNewTransactionsError):
        source.get_latest_transactions()
    assert inbox.is_dir()
    assert processed.is_dir()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

def test_both_sources_conform_to_transaction_source_protocol():
    """runtime_checkable Protocol — isinstance check pins that all
    source classes expose get_latest_transactions in the right shape."""
    local = ingest.LocalFileSource(FIXTURE)
    airtable = ingest.AirtableInboxSource(base=object())
    local_inbox = ingest.LocalInboxSource(conn=object())
    assert isinstance(local, ingest.TransactionSource)
    assert isinstance(airtable, ingest.TransactionSource)
    assert isinstance(local_inbox, ingest.TransactionSource)
