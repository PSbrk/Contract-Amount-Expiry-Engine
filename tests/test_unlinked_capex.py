"""Unlinked-CapEx enrichment + persistence (the parked-spend spotting surface)."""
import sqlite3

import pandas as pd

from engine.capex import summarize_unlinked
from engine.sqlite_client import (
    ensure_schema,
    load_unlinked_capex,
    replace_unlinked_capex,
)


def _df():
    return pd.DataFrame({
        "Account No": ["63015", "63015", "63040", "63015"],
        "Project ID": ["FFE001389", "FFE001389", "X", "FFE002000"],
        "Campus":     ["HNV", "HNV", "CEN", "OKC"],
        "Vendor":     ["", "", "Acme", ""],
        "Record Description": [
            "Deposit for HNV office carpet replacement, EMPIRE TODAY HS",
            "Final payment carpet, EMPIRE TODAY",
            "opex janitorial",
            "Generator panel, WARREN CAT",
        ],
        "Amount": [4278.26, 9000.0, 50.0, 1000.0],
    })


def test_summarize_unlinked_enriches_only_parked_capex():
    out = summarize_unlinked(_df(), {"FFE001389"}, "63015")
    assert len(out) == 1
    r = out[0]
    assert r["capex_id"] == "FFE001389"
    assert r["spend"] == 13278.26                 # 4278.26 + 9000, 63015 only
    assert r["campuses"] == "HNV"
    assert r["rows"] == 2
    assert "EMPIRE TODAY HS" in r["descriptions"]
    # opex (63040) and the non-parked CapEx ID are excluded.
    assert "janitorial" not in r["descriptions"]
    assert "WARREN CAT" not in r["descriptions"]


def test_summarize_unlinked_empty_when_nothing_parked():
    assert summarize_unlinked(_df(), set(), "63015") == []


def test_replace_and_load_unlinked_roundtrip_is_wholesale():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    rows = summarize_unlinked(_df(), {"FFE001389", "FFE002000"}, "63015")
    for r in rows:
        r["updated"] = "2026-06-26"
    assert replace_unlinked_capex(c, rows) == 2
    loaded = load_unlinked_capex(c)
    assert {x["CapEx ID"] for x in loaded} == {"FFE001389", "FFE002000"}
    assert loaded[0]["Spend"] >= loaded[1]["Spend"]      # biggest spend first
    # A later ingest replaces wholesale (snapshot, not an audit log).
    assert replace_unlinked_capex(c, []) == 0
    assert load_unlinked_capex(c) == []
    c.close()
