"""Tests for the TransactionSource Protocol implementations.

LocalFileSource is exercised directly against the on-disk fixture. The
AirtableInboxSource branches are tested by monkeypatching the airtable_client
helpers it calls (get_newest_unprocessed_inbox, download_attachment_bytes,
file_hash_already_processed) — no live Airtable needed.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from engine import airtable_client, ingest


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
# Protocol conformance
# ---------------------------------------------------------------------------

def test_both_sources_conform_to_transaction_source_protocol():
    """runtime_checkable Protocol — isinstance check pins that both classes
    expose get_latest_transactions in the right shape."""
    local = ingest.LocalFileSource(FIXTURE)
    airtable = ingest.AirtableInboxSource(base=object())
    assert isinstance(local, ingest.TransactionSource)
    assert isinstance(airtable, ingest.TransactionSource)
