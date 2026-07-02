"""Ingest sanity gate — hold a wrong/partial/differently-scoped export before
it can attribute and overwrite Asana (regression for the 2026-07-01 incident
where a 14,815-row export replaced the 17,231-row one and dropped ~$566k)."""

from __future__ import annotations

import sqlite3

from engine.main import _check_ingest_sanity
from engine.sqlite_client import append_run_log, ensure_schema


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    return c


def _ok_ingest(c, rows, total):
    append_run_log(
        c, run_id="2026-06-30T00:00:00", mode="ingest", outcome="ok",
        file_name="Transactions.csv", file_hash="a" * 64,
        rows_in_scope=rows, rows_out_of_scope=0, total_in_scope=total,
    )


def test_passes_when_volume_matches_last_ok():
    c = _conn(); _ok_ingest(c, 17231, 20_790_332.0)
    ok, reasons, _ = _check_ingest_sanity(c, rows_in=17231, total_in=20_790_332.0, rows_out=0)
    assert ok and reasons == []


def test_holds_on_the_real_incident_numbers():
    c = _conn(); _ok_ingest(c, 17231, 20_790_332.0)
    # the bad export: 14,815 in-scope (-14%), $17.79M (-14%), 2,027 out-of-scope
    ok, reasons, _ = _check_ingest_sanity(c, rows_in=14815, total_in=17_785_561.0, rows_out=2027)
    assert not ok
    assert any("in-scope rows" in r for r in reasons)
    assert any("in-scope dollars" in r for r in reasons)
    assert any("out-of-scope" in r for r in reasons)


def test_holds_on_oos_spike_even_without_a_baseline():
    c = _conn()  # first-ever ingest, no OK to compare volume against
    ok, reasons, _ = _check_ingest_sanity(c, rows_in=10000, total_in=1_000_000.0, rows_out=2000)
    assert not ok
    assert any("out-of-scope" in r for r in reasons)


def test_first_clean_ingest_passes():
    c = _conn()  # no baseline, 0 out-of-scope -> nothing to trip
    ok, reasons, _ = _check_ingest_sanity(c, rows_in=17231, total_in=20_790_332.0, rows_out=0)
    assert ok and reasons == []


def test_tolerates_small_wobble_under_threshold():
    c = _conn(); _ok_ingest(c, 17231, 20_790_332.0)
    # ~1.3% fewer rows / ~0.9% fewer dollars — normal daily variation
    ok, reasons, _ = _check_ingest_sanity(c, rows_in=17000, total_in=20_600_000.0, rows_out=0)
    assert ok and reasons == []


if __name__ == "__main__":
    test_passes_when_volume_matches_last_ok()
    test_holds_on_the_real_incident_numbers()
    test_holds_on_oos_spike_even_without_a_baseline()
    test_first_clean_ingest_passes()
    test_tolerates_small_wobble_under_threshold()
    print("ingest sanity gate: all checks pass")
