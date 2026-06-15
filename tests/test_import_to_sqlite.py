"""Tests for tools/import_to_sqlite.py.

End-to-end against a real on-disk SQLite file (not :memory:) so we exercise
the same get_db_connection / commit path the operator's run will take.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from engine import sqlite_client
from tools import import_to_sqlite


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Fresh on-disk engine.db with the full schema applied."""
    p = tmp_path / "engine.db"
    conn = sqlite_client.get_db_connection(p)
    try:
        sqlite_client.ensure_schema(conn)
    finally:
        conn.close()
    return p


def _payload_one_row_per_table() -> dict:
    return {
        "vendor_aliases": [
            {"Contract Name": "Acme SaaS", "Aliases": "ACME\nAcme Inc", "Notes": ""},
        ],
        "campus_map": [
            {"Tableau Code": "ZZZ", "Asana Option Names": "Zed Zone",
             "Drop": False, "Notes": "test row"},
        ],
        "learned_mappings": [
            {"Campus": "CEN", "Dept": "000", "Account No": "63015",
             "Vendor": "Acme", "Contract Name": "Acme SaaS",
             "Learned At": "2025-01-15", "Notes": ""},
        ],
    }


def _row_count(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    finally:
        conn.close()


def test_run_import_inserts_one_row_per_table(db_path):
    stats = import_to_sqlite.run_import(
        _payload_one_row_per_table(), db_path=db_path, truncate=False,
    )

    assert stats["vendor_aliases"]   == {"inserted": 1, "skipped_dup": 0, "skipped_blank": 0}
    assert stats["campus_map"]       == {"inserted": 1, "skipped_dup": 0, "skipped_blank": 0}
    assert stats["learned_mappings"] == {"inserted": 1, "skipped_dup": 0, "skipped_blank": 0}

    assert _row_count(db_path, "Vendor Aliases") == 1
    assert _row_count(db_path, "Campus Map") == 1
    assert _row_count(db_path, "Learned Mappings") == 1


def test_run_import_synthesizes_learned_mapping_key_from_components(db_path):
    """The Learned Mappings 'Key' column is UNIQUE and required, but the
    Airtable export does NOT carry one (the engine generates it). The
    importer must synthesize it from Campus|Dept|Account No|Vendor in
    the canonical format the engine's attribution layer uses (verified
    against engine.attribution._group_key separator convention)."""
    import_to_sqlite.run_import(
        _payload_one_row_per_table(), db_path=db_path, truncate=False,
    )

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            'SELECT "Key", "Campus", "Dept", "Account No", "Vendor", "Contract Name" '
            'FROM "Learned Mappings"'
        ).fetchone()
    finally:
        conn.close()

    key, campus, dept, account, vendor, contract = row
    assert key == "CEN|000|63015|Acme"
    assert campus == "CEN"
    assert contract == "Acme SaaS"


def test_run_import_is_idempotent_skips_duplicates(db_path):
    """Re-running the same payload must skip duplicates without raising --
    the operator may run the import after a partial failure and we do not
    want to lose work."""
    payload = _payload_one_row_per_table()
    first = import_to_sqlite.run_import(payload, db_path=db_path, truncate=False)
    second = import_to_sqlite.run_import(payload, db_path=db_path, truncate=False)

    # First pass inserts everything.
    assert first["vendor_aliases"]["inserted"] == 1
    # Second pass inserts nothing, reports the skip. Vendor Aliases has
    # no UNIQUE constraint so the importer dedups by (Contract Name,
    # Aliases) tuple explicitly; the other two rely on IntegrityError.
    assert second["vendor_aliases"]   == {"inserted": 0, "skipped_dup": 1, "skipped_blank": 0}
    assert second["campus_map"]       == {"inserted": 0, "skipped_dup": 1, "skipped_blank": 0}
    assert second["learned_mappings"] == {"inserted": 0, "skipped_dup": 1, "skipped_blank": 0}

    # Row counts still equal one (no duplicates created).
    assert _row_count(db_path, "Vendor Aliases") == 1
    assert _row_count(db_path, "Campus Map") == 1
    assert _row_count(db_path, "Learned Mappings") == 1


def test_run_import_with_truncate_wipes_then_inserts(db_path):
    """--truncate is the operator's escape hatch for a clean reset:
    DELETE all rows in the three operator tables, THEN insert.
    Verifies the truncate counts the pre-existing rows so the operator
    sees what got dropped."""
    payload = _payload_one_row_per_table()

    # Seed once.
    import_to_sqlite.run_import(payload, db_path=db_path, truncate=False)

    # Build a slightly-different payload so the post-truncate insert is
    # observably distinct from the pre-truncate row.
    new_payload = {
        "vendor_aliases": [
            {"Contract Name": "Beta Co", "Aliases": "BETA", "Notes": ""},
        ],
        "campus_map": [],
        "learned_mappings": [],
    }

    stats = import_to_sqlite.run_import(new_payload, db_path=db_path, truncate=True)

    assert stats["truncated"] == {
        "Vendor Aliases": 1, "Campus Map": 1, "Learned Mappings": 1,
    }
    assert stats["vendor_aliases"]   == {"inserted": 1, "skipped_dup": 0, "skipped_blank": 0}
    assert stats["campus_map"]       == {"inserted": 0, "skipped_dup": 0, "skipped_blank": 0}
    assert stats["learned_mappings"] == {"inserted": 0, "skipped_dup": 0, "skipped_blank": 0}

    # The new VA row is in; the old one is gone.
    assert _row_count(db_path, "Vendor Aliases") == 1
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute('SELECT "Contract Name" FROM "Vendor Aliases"').fetchall()
    finally:
        conn.close()
    assert rows == [("Beta Co",)]


def test_run_import_preserves_drop_flag_as_integer(db_path):
    """SQLite checkbox columns are INTEGER 0/1; importer must convert the
    Airtable boolean correctly so the Drop semantics survive the round-trip."""
    payload = {
        "vendor_aliases": [],
        "campus_map": [
            {"Tableau Code": "INT", "Asana Option Names": "",
             "Drop": True, "Notes": "international -- drop"},
            {"Tableau Code": "CEN", "Asana Option Names": "Central",
             "Drop": False, "Notes": ""},
        ],
        "learned_mappings": [],
    }
    import_to_sqlite.run_import(payload, db_path=db_path, truncate=False)

    conn = sqlite3.connect(db_path)
    try:
        rows = dict(conn.execute(
            'SELECT "Tableau Code", "Drop" FROM "Campus Map"'
        ).fetchall())
    finally:
        conn.close()
    assert rows["INT"] == 1
    assert rows["CEN"] == 0


def test_run_import_does_not_touch_non_operator_tables(db_path):
    """Defensive: a tomorrow-me edit that accidentally adds an Inbox /
    Dashboard / Run Log / State / Needs Tagging import path would clobber
    re-derivable state with stale data. Pin that the importer leaves
    those tables untouched."""
    untouched = ("Inbox", "Dashboard", "Needs Tagging", "State", "Run Log")
    pre = {t: _row_count(db_path, t) for t in untouched}

    import_to_sqlite.run_import(
        _payload_one_row_per_table(), db_path=db_path, truncate=True,
    )

    post = {t: _row_count(db_path, t) for t in untouched}
    assert pre == post, "import_to_sqlite must not touch re-derived tables"


def test_run_import_skips_rows_with_blank_required_fields(db_path):
    """Defensive against a malformed export -- a row missing the identity
    field for any of the three tables would corrupt downstream lookups.
    The exporter pre-filters these, but the importer must defend itself
    too in case a hand-edited JSON sneaks one in."""
    payload = {
        "vendor_aliases": [
            {"Contract Name": "", "Aliases": "X"},
            {"Contract Name": "   ", "Aliases": "Y"},  # whitespace-only also counts
            {"Contract Name": "Acme", "Aliases": "ACME"},
        ],
        "campus_map": [
            {"Tableau Code": "", "Asana Option Names": "X", "Drop": False},
            {"Tableau Code": "CEN", "Asana Option Names": "Central", "Drop": False},
        ],
        "learned_mappings": [
            # Missing Vendor -> incomplete identity -> skipped_blank.
            {"Campus": "CEN", "Dept": "000", "Account No": "63015",
             "Vendor": "", "Contract Name": "Acme"},
            # Missing Contract Name -> skipped_blank.
            {"Campus": "CEN", "Dept": "000", "Account No": "63015",
             "Vendor": "Acme", "Contract Name": ""},
            # Fully populated -> inserts.
            {"Campus": "CEN", "Dept": "107", "Account No": "63020",
             "Vendor": "Beta", "Contract Name": "Beta Co"},
        ],
    }
    stats = import_to_sqlite.run_import(payload, db_path=db_path, truncate=False)

    assert stats["vendor_aliases"]   == {"inserted": 1, "skipped_dup": 0, "skipped_blank": 2}
    assert stats["campus_map"]       == {"inserted": 1, "skipped_dup": 0, "skipped_blank": 1}
    assert stats["learned_mappings"] == {"inserted": 1, "skipped_dup": 0, "skipped_blank": 2}


def test_main_reads_json_file_and_returns_zero(tmp_path, db_path, capsys):
    """End-to-end CLI surface: writes payload to disk, calls main(), verifies
    summary print + exit 0."""
    payload_path = tmp_path / "export.json"
    payload_path.write_text(json.dumps(_payload_one_row_per_table()), encoding="utf-8")

    rc = import_to_sqlite.main([
        "--in", str(payload_path),
        "--db", str(db_path),
    ])
    assert rc == 0

    out = capsys.readouterr().out
    assert "Vendor Aliases" in out
    assert "inserted     1" in out


def test_main_returns_2_when_input_missing(tmp_path, db_path, capsys):
    rc = import_to_sqlite.main([
        "--in", str(tmp_path / "does-not-exist.json"),
        "--db", str(db_path),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "input file not found" in err


def test_main_returns_2_when_db_does_not_exist(tmp_path, capsys):
    payload_path = tmp_path / "export.json"
    payload_path.write_text(json.dumps(_payload_one_row_per_table()), encoding="utf-8")

    rc = import_to_sqlite.main([
        "--in", str(payload_path),
        "--db", str(tmp_path / "does-not-exist.db"),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--provision" in err  # operator hint
