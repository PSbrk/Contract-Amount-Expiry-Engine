"""Tests for engine.sqlite_client — exercised against an in-memory SQLite
database. No real filesystem path is touched.

The structural shape of these tests mirrors the legacy
tests/test_airtable_client.py so the migration is reviewable as a
side-by-side port. Key differences from the Airtable era:

- Records come back as {"id": int, "fields": {col_name: value, ...}}
  — same dict shape, integer id instead of an Airtable 'recXYZ' string.
- "Blank" is NULL → None in Python, NOT an absent key. Assertions that
  used to read `"X" not in rec["fields"]` become `rec["fields"]["X"] is None`.
- SQLite's ON CONFLICT writes all columns uniformly; there's no
  PATCH-merge gotcha for None values. The "stale cell" tests are kept
  because the bug class (a cell that transitioned non-None → None
  staying stale) is still worth pinning, even though it's now
  structurally impossible.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from config import schema
from engine.sqlite_client import (
    Promotion,
    SchemaPlan,
    append_run_log,
    cleanup_stale_needs_tagging,
    cleanup_stale_state,
    ensure_schema,
    file_hash_already_processed,
    insert_inbox_processed,
    load_campus_map_overrides,
    load_learned_mappings,
    load_state_priors,
    load_vendor_aliases,
    promote_filled_needs_tagging,
    prune_run_log_older_than,
    upsert_dashboard_row,
    upsert_needs_tagging_group,
    upsert_state_for_contract,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    """A fresh in-memory SQLite database with the engine schema applied."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    yield c
    c.close()


def _seed(conn, table_name: str, fields: dict) -> int:
    """Insert one row into `table_name` with the given column/value mapping.
    Returns the new row id."""
    cols = ", ".join(f'"{k}"' for k in fields)
    placeholders = ", ".join("?" for _ in fields)
    cur = conn.execute(
        f'INSERT INTO "{table_name}" ({cols}) VALUES ({placeholders})',
        tuple(fields.values()),
    )
    conn.commit()
    return cur.lastrowid


def _all_rows(conn, table_name: str) -> list[dict]:
    """Return all rows of `table_name` as a list of plain {col: value} dicts."""
    return [
        dict(r)
        for r in conn.execute(f'SELECT * FROM "{table_name}"').fetchall()
    ]


# ---------------------------------------------------------------------------
# sqlite_column_type mapper
# ---------------------------------------------------------------------------

def test_sqlite_column_type_text_variants():
    for ft in ("singleLineText", "multilineText", "singleSelect", "date"):
        assert schema.sqlite_column_type({"type": ft}) == "TEXT"


def test_sqlite_column_type_checkbox_has_default():
    """Checkbox columns must default to 0 so a missing value reads as
    falsy without a NULL check at every callsite."""
    assert schema.sqlite_column_type({"type": "checkbox"}) == "INTEGER NOT NULL DEFAULT 0"


def test_sqlite_column_type_number_precision_zero_is_integer():
    decl = {"type": "number", "options": {"precision": 0}}
    assert schema.sqlite_column_type(decl) == "INTEGER"


def test_sqlite_column_type_number_with_precision_is_real():
    decl = {"type": "number", "options": {"precision": 2}}
    assert schema.sqlite_column_type(decl) == "REAL"


def test_sqlite_column_type_rejects_multipleAttachments():
    """multipleAttachments has no SQLite equivalent — it should be
    filtered out of TABLES_SCHEMA before reaching this mapper. The
    filter is in config.schema; if it ever gets bypassed, surface
    loudly rather than producing an unreadable CREATE TABLE."""
    with pytest.raises(ValueError, match="multipleAttachments"):
        schema.sqlite_column_type({"type": "multipleAttachments"})


def test_sqlite_column_type_rejects_unknown_type():
    with pytest.raises(ValueError, match="unsupported"):
        schema.sqlite_column_type({"type": "linkRecord"})


# ---------------------------------------------------------------------------
# ensure_schema — empty / full / partial / dry-run
# ---------------------------------------------------------------------------

def test_ensure_schema_empty_database_creates_all_eight_tables():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    plan = ensure_schema(c)

    assert isinstance(plan, SchemaPlan)
    expected = {t["name"] for t in schema.TABLES_SCHEMA}
    assert set(plan.tables_created) == expected
    # Fields land via CREATE TABLE when a whole table is being created,
    # not as separate ALTER TABLE statements.
    assert plan.fields_added == []
    assert plan.tables_already_present == []
    # Verify the tables are actually there.
    actual = {
        r["name"]
        for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert expected.issubset(actual)


def test_ensure_schema_already_provisioned_is_noop(conn):
    """Re-running ensure_schema against an already-provisioned database
    is a true no-op: no tables created, no columns added."""
    plan = ensure_schema(conn)
    assert plan.tables_created == []
    assert plan.fields_added == []
    assert plan.is_noop is True
    assert set(plan.tables_already_present) == {
        t["name"] for t in schema.TABLES_SCHEMA
    }


def test_ensure_schema_partial_adds_only_missing_columns():
    """A drifted database (Inbox table exists but is missing the Notes
    column) gets the missing column added via ALTER TABLE; other tables
    are created from scratch."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    # Pre-create Inbox with all columns except Notes.
    c.execute('''
        CREATE TABLE "Inbox" (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            "Name" TEXT,
            "File Hash" TEXT,
            "Processed" INTEGER NOT NULL DEFAULT 0,
            "Processed At" TEXT,
            "Rows In Scope" INTEGER,
            "Total In Scope" REAL
        )
    ''')
    plan = ensure_schema(c)

    assert ("Inbox", "Notes") in plan.fields_added
    assert "Inbox" not in plan.tables_created
    other_names = {
        t["name"] for t in schema.TABLES_SCHEMA if t["name"] != "Inbox"
    }
    assert set(plan.tables_created) == other_names
    # The added column actually exists on the table.
    cols = {
        r["name"]
        for r in c.execute('PRAGMA table_info("Inbox")').fetchall()
    }
    assert "Notes" in cols


def test_ensure_schema_dry_run_makes_no_writes():
    """dry_run=True computes the plan but creates no tables."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    plan = ensure_schema(c, dry_run=True)

    expected_table_names = [t["name"] for t in schema.TABLES_SCHEMA]
    assert plan.tables_created == expected_table_names
    actual = {
        r["name"]
        for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    # NONE of the engine tables exist.
    assert not (set(expected_table_names) & actual)


def test_ensure_schema_unique_indexes_block_duplicate_keys(conn):
    """The Dashboard / Needs Tagging / State / Run Log / Learned Mappings /
    Campus Map / Inbox tables get UNIQUE indexes on their natural keys so
    a duplicate insert raises IntegrityError instead of silently producing
    two rows."""
    conn.execute(
        '''INSERT INTO "Dashboard" ("Asana Task GID", "Contract", "Alarms",
                                     "Spent so far", "Start", "Last Updated")
           VALUES ('gid-1', 'A', 'Clear', 0, '2026-01-01', '2026-06-15')'''
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            '''INSERT INTO "Dashboard" ("Asana Task GID", "Contract", "Alarms",
                                         "Spent so far", "Start", "Last Updated")
               VALUES ('gid-1', 'B', 'Clear', 0, '2026-01-01', '2026-06-15')'''
        )


# ---------------------------------------------------------------------------
# Inbox dedup — file_hash_already_processed + insert_inbox_processed
# ---------------------------------------------------------------------------

def test_file_hash_already_processed_empty_database_returns_false(conn):
    assert file_hash_already_processed(conn, "abc123") is False


def test_file_hash_already_processed_empty_hash_returns_false(conn):
    """A blank hash MUST NOT match any row — would otherwise produce a
    false positive that skips a real file."""
    assert file_hash_already_processed(conn, "") is False


def test_insert_inbox_processed_records_audit_row(conn):
    rec = insert_inbox_processed(
        conn, name="Q2.csv", file_hash="abc123",
        rows_in_scope=42, total_in_scope=1234.56,
        processed_at_iso_date="2026-06-15", notes="first run",
    )
    assert rec["fields"]["Name"] == "Q2.csv"
    assert rec["fields"]["File Hash"] == "abc123"
    assert rec["fields"]["Processed"] == 1
    assert rec["fields"]["Rows In Scope"] == 42


def test_file_hash_already_processed_after_insert_returns_true(conn):
    insert_inbox_processed(
        conn, name="Q2.csv", file_hash="abc123",
        rows_in_scope=1, total_in_scope=1.0,
        processed_at_iso_date="2026-06-15",
    )
    assert file_hash_already_processed(conn, "abc123") is True
    assert file_hash_already_processed(conn, "different") is False


def test_insert_inbox_processed_blocks_duplicate_hash(conn):
    """File Hash has a UNIQUE index — a second insert with the same hash
    raises IntegrityError. Callers should check
    file_hash_already_processed FIRST and skip on True."""
    insert_inbox_processed(
        conn, name="Q2.csv", file_hash="abc123",
        rows_in_scope=1, total_in_scope=1.0,
        processed_at_iso_date="2026-06-15",
    )
    with pytest.raises(sqlite3.IntegrityError):
        insert_inbox_processed(
            conn, name="Q2-dup.csv", file_hash="abc123",
            rows_in_scope=1, total_in_scope=1.0,
            processed_at_iso_date="2026-06-15",
        )


# ---------------------------------------------------------------------------
# Run Log validation
# ---------------------------------------------------------------------------

def test_append_run_log_rejects_unknown_mode(conn):
    with pytest.raises(ValueError, match="not one of"):
        append_run_log(conn, run_id="x", mode="ingst", outcome="ok")


def test_append_run_log_rejects_unknown_outcome(conn):
    with pytest.raises(ValueError, match="not one of"):
        append_run_log(conn, run_id="x", mode="ingest", outcome="success")


def test_append_run_log_writes_all_known_fields(conn):
    rec = append_run_log(
        conn,
        run_id="2026-06-15T08:00:00+00:00", mode="ingest", outcome="ok",
        file_name="Q2.csv", file_hash="abc123",
        rows_in_scope=100, rows_out_of_scope=10,
        total_in_scope=5000.0, total_out_of_scope=500.0,
        anomalies="none", review_flags="none", notes="ok",
    )
    fields = rec["fields"]
    assert fields["Run ID"] == "2026-06-15T08:00:00+00:00"
    assert fields["Mode"] == "ingest"
    assert fields["Outcome"] == "ok"
    assert fields["Rows In Scope"] == 100
    assert fields["Total In Scope"] == 5000.0


# ---------------------------------------------------------------------------
# load_vendor_aliases
# ---------------------------------------------------------------------------

def test_load_vendor_aliases_parses_commas_and_newlines(conn):
    _seed(conn, "Vendor Aliases",
          {"Contract Name": "Acme SaaS", "Aliases": "ACME, ACME INC"})
    _seed(conn, "Vendor Aliases",
          {"Contract Name": "Beta Tools",
           "Aliases": "BETA\nBETA TOOLS\nbeta inc"})
    out = load_vendor_aliases(conn)
    assert out == {
        "Acme SaaS": ["ACME", "ACME INC"],
        "Beta Tools": ["BETA", "BETA TOOLS", "beta inc"],
    }


def test_load_vendor_aliases_handles_empty_aliases_cell(conn):
    """Empty string and unset (NULL) both produce an empty alias list."""
    _seed(conn, "Vendor Aliases",
          {"Contract Name": "Solo Contract", "Aliases": ""})
    _seed(conn, "Vendor Aliases", {"Contract Name": "Another"})  # Aliases NULL
    out = load_vendor_aliases(conn)
    assert out == {"Solo Contract": [], "Another": []}


# ---------------------------------------------------------------------------
# load_campus_map_overrides
# ---------------------------------------------------------------------------

def test_load_campus_map_overrides_picks_up_forward_and_drops(conn):
    _seed(conn, "Campus Map",
          {"Tableau Code": "CEN",
           "Asana Option Names": "CEN, CEN/EDM, EDM_NEW"})
    _seed(conn, "Campus Map", {"Tableau Code": "ZZZ", "Drop": 1})
    _seed(conn, "Campus Map",
          {"Tableau Code": "OMH", "Asana Option Names": "OMH"})
    overrides, drops = load_campus_map_overrides(conn)
    assert overrides == {
        "CEN": frozenset({"CEN", "CEN/EDM", "EDM_NEW"}),
        "OMH": frozenset({"OMH"}),
    }
    assert drops == frozenset({"ZZZ"})


def test_load_campus_map_returns_none_drops_when_no_drop_checkboxes_set(conn):
    """If no Campus Map row has Drop=1, return None for drops so the
    builder falls back to config defaults (INT)."""
    _seed(conn, "Campus Map",
          {"Tableau Code": "CEN",
           "Asana Option Names": "CEN, CEN/EDM"})
    overrides, drops = load_campus_map_overrides(conn)
    assert overrides == {"CEN": frozenset({"CEN", "CEN/EDM"})}
    assert drops is None


# ---------------------------------------------------------------------------
# load_learned_mappings
# ---------------------------------------------------------------------------

def test_load_learned_mappings_builds_key_tuple(conn):
    _seed(conn, "Learned Mappings", {
        "Key": "CEN|000|63015|Acme SaaS",
        "Campus": "CEN", "Dept": "000",
        "Account No": "63015", "Vendor": "Acme SaaS",
        "Contract Name": "Acme SaaS Contract",
    })
    _seed(conn, "Learned Mappings", {  # incomplete — must be skipped
        "Key": "PARTIAL", "Campus": "CEN", "Contract Name": "",
    })
    out = load_learned_mappings(conn)
    assert out == {
        ("CEN", "000", "63015", "Acme SaaS"): "Acme SaaS Contract",
    }


# ---------------------------------------------------------------------------
# upsert_needs_tagging_group
# ---------------------------------------------------------------------------

def test_upsert_needs_tagging_creates_new_row(conn):
    result = upsert_needs_tagging_group(
        conn,
        group_key="CEN|000|63015|Mystery",
        campus="CEN", dept="000", account_no="63015", vendor="Mystery",
        sample_description="some desc",
        amount=1234.56,
        candidate_names=["Acme SaaS"],
        created_at_iso_date="2026-06-12",
    )
    assert result["fields"]["Group Key"] == "CEN|000|63015|Mystery"
    assert result["fields"]["$ in group"] == 1234.56
    # Engine writes to Engine Candidates (its own field); Notes is for
    # the operator and starts NULL on a fresh row.
    assert result["fields"]["Engine Candidates"].startswith("Engine vendor candidates")
    assert result["fields"]["Notes"] is None
    assert len(_all_rows(conn, "Needs Tagging")) == 1


def test_upsert_needs_tagging_updates_existing_row_by_group_key(conn):
    """Re-upsert with the same Group Key updates the rolling fields and
    DOES NOT create a duplicate row. Operator-owned Notes must survive."""
    _seed(conn, "Needs Tagging", {
        "Group Key": "CEN|000|63015|Mystery",
        "Campus": "CEN", "Dept": "000", "Account No": "63015",
        "Vendor": "Mystery",
        "Sample Record Description": "old desc",
        "$ in group": 100.0,
        "Created At": "2026-05-01",
        "Notes": "operator annotation — must survive",
    })
    upsert_needs_tagging_group(
        conn,
        group_key="CEN|000|63015|Mystery",
        campus="CEN", dept="000", account_no="63015", vendor="Mystery",
        sample_description="updated desc",
        amount=999.99,
        candidate_names=[],
        created_at_iso_date="2026-06-12",
    )
    rows = _all_rows(conn, "Needs Tagging")
    assert len(rows) == 1
    assert rows[0]["$ in group"] == 999.99
    assert rows[0]["Sample Record Description"] == "updated desc"
    # Operator-owned Notes preserved.
    assert rows[0]["Notes"] == "operator annotation — must survive"


def test_upsert_needs_tagging_with_no_candidates_says_so(conn):
    upsert_needs_tagging_group(
        conn,
        group_key="CEN|000|63015|TotallyUnknown",
        campus="CEN", dept="000", account_no="63015", vendor="TotallyUnknown",
        sample_description="x",
        amount=10.0,
        candidate_names=[],
        created_at_iso_date="2026-06-12",
    )
    rec = _all_rows(conn, "Needs Tagging")[0]
    assert "No vendor candidates" in rec["Engine Candidates"]


def test_upsert_needs_tagging_handles_vendor_with_apostrophe(conn):
    """Regression: vendors like Domino's or O'Reilly round-trip through
    the keyed lookup without producing a malformed query. SQLite
    parameterized queries handle quoting natively — no Airtable-style
    formula escape needed — but pinning the behavior keeps the test
    suite useful when an operator inevitably reports a Domino's row."""
    upsert_needs_tagging_group(
        conn,
        group_key="CEN|000|63015|Domino's",
        campus="CEN", dept="000", account_no="63015", vendor="Domino's",
        sample_description="x",
        amount=10.0,
        candidate_names=[],
        created_at_iso_date="2026-06-12",
    )
    upsert_needs_tagging_group(
        conn,
        group_key="CEN|000|63015|Domino's",
        campus="CEN", dept="000", account_no="63015", vendor="Domino's",
        sample_description="y",
        amount=20.0,
        candidate_names=[],
        created_at_iso_date="2026-06-12",
    )
    rows = _all_rows(conn, "Needs Tagging")
    assert len(rows) == 1
    assert rows[0]["$ in group"] == 20.0


# ---------------------------------------------------------------------------
# promote_filled_needs_tagging
# ---------------------------------------------------------------------------

def test_promote_filled_creates_learned_and_deletes_nt(conn):
    _seed(conn, "Needs Tagging", {
        "Group Key": "CEN|000|63015|Acme",
        "Campus": "CEN", "Dept": "000", "Account No": "63015",
        "Vendor": "Acme", "Assign Contract": "Acme SaaS Contract",
    })
    _seed(conn, "Needs Tagging", {
        "Group Key": "CEN|107|63020|Beta",
        "Campus": "CEN", "Dept": "107", "Account No": "63020",
        "Vendor": "Beta", "Assign Contract": "",
    })
    promotions = promote_filled_needs_tagging(conn, learned_at_iso_date="2026-06-12")
    assert len(promotions) == 1
    p = promotions[0]
    assert p.group_key == "CEN|000|63015|Acme"
    assert p.contract_name == "Acme SaaS Contract"

    # The Learned Mappings table now has the promoted row.
    lm = _all_rows(conn, "Learned Mappings")
    assert len(lm) == 1
    assert lm[0]["Key"] == "CEN|000|63015|Acme"
    assert lm[0]["Contract Name"] == "Acme SaaS Contract"
    # The Needs Tagging row was deleted; the unfilled one survives.
    nt = _all_rows(conn, "Needs Tagging")
    assert len(nt) == 1
    assert nt[0]["Assign Contract"] == ""


def test_promote_filled_updates_existing_learned_mapping_in_place(conn):
    """If the same Key already exists in Learned Mappings (operator
    re-promotion), update rather than duplicate."""
    _seed(conn, "Learned Mappings", {
        "Key": "CEN|000|63015|Acme",
        "Campus": "CEN", "Dept": "000", "Account No": "63015",
        "Vendor": "Acme", "Contract Name": "OLD Contract Name",
    })
    _seed(conn, "Needs Tagging", {
        "Group Key": "CEN|000|63015|Acme",
        "Campus": "CEN", "Dept": "000", "Account No": "63015",
        "Vendor": "Acme", "Assign Contract": "NEW Contract Name",
    })
    promotions = promote_filled_needs_tagging(conn, learned_at_iso_date="2026-06-12")
    assert len(promotions) == 1
    lm = _all_rows(conn, "Learned Mappings")
    assert len(lm) == 1  # no duplicate
    assert lm[0]["Contract Name"] == "NEW Contract Name"


def test_promote_skips_rows_with_incomplete_fields(conn):
    """A row with filled Assign Contract but missing Campus/etc. must
    NOT be promoted into a broken Learned Mappings row."""
    _seed(conn, "Needs Tagging", {
        "Group Key": "MISSING",
        "Campus": "",  # missing
        "Dept": "000", "Account No": "63015", "Vendor": "Acme",
        "Assign Contract": "Some Contract",
    })
    promotions = promote_filled_needs_tagging(conn, learned_at_iso_date="2026-06-12")
    assert len(promotions) == 0
    # NT row left intact (no destructive op).
    assert len(_all_rows(conn, "Needs Tagging")) == 1


def test_promote_rejects_unknown_contract_name_when_validation_set_provided(conn):
    """An operator typo in Assign Contract must NOT bake a permanent
    broken Learned Mapping. Row stays for correction."""
    _seed(conn, "Needs Tagging", {
        "Group Key": "CEN|000|63015|Acme",
        "Campus": "CEN", "Dept": "000", "Account No": "63015",
        "Vendor": "Acme", "Assign Contract": "TypoedNameHere",
    })
    promotions = promote_filled_needs_tagging(
        conn, learned_at_iso_date="2026-06-12",
        valid_contract_names=frozenset({"Acme SaaS Contract"}),
    )
    assert promotions == []
    # NT row preserved for operator correction. No LM created.
    assert len(_all_rows(conn, "Needs Tagging")) == 1
    assert _all_rows(conn, "Learned Mappings") == []


def test_promote_with_no_validation_set_still_promotes(conn):
    """valid_contract_names=None disables validation (initial-run / dev path)."""
    _seed(conn, "Needs Tagging", {
        "Group Key": "CEN|000|63015|Acme",
        "Campus": "CEN", "Dept": "000", "Account No": "63015",
        "Vendor": "Acme", "Assign Contract": "Any Name",
    })
    promotions = promote_filled_needs_tagging(conn, learned_at_iso_date="2026-06-12")
    assert len(promotions) == 1


# ---------------------------------------------------------------------------
# cleanup_stale_needs_tagging
# ---------------------------------------------------------------------------

def test_cleanup_stale_needs_tagging_deletes_only_empty_rows_not_in_live_set(conn):
    _seed(conn, "Needs Tagging",
          {"Group Key": "STALE|000|63015|X", "Assign Contract": ""})
    _seed(conn, "Needs Tagging",
          {"Group Key": "LIVE|000|63015|Y", "Assign Contract": ""})
    # Filled row — NEVER deleted regardless of live-set membership.
    _seed(conn, "Needs Tagging",
          {"Group Key": "STALE_BUT_FILLED|000|63015|Z",
           "Assign Contract": "Some Contract"})

    deleted = cleanup_stale_needs_tagging(
        conn, live_group_keys={"LIVE|000|63015|Y"},
    )
    assert deleted == 1
    remaining_keys = {r["Group Key"] for r in _all_rows(conn, "Needs Tagging")}
    assert remaining_keys == {"LIVE|000|63015|Y", "STALE_BUT_FILLED|000|63015|Z"}


def test_cleanup_stale_needs_tagging_with_empty_live_set_deletes_all_unfilled(conn):
    _seed(conn, "Needs Tagging",
          {"Group Key": "A|0|1|X", "Assign Contract": ""})
    _seed(conn, "Needs Tagging",
          {"Group Key": "B|0|1|Y", "Assign Contract": ""})
    _seed(conn, "Needs Tagging",
          {"Group Key": "C|0|1|Z", "Assign Contract": "Has Answer"})

    deleted = cleanup_stale_needs_tagging(conn, live_group_keys=set())
    assert deleted == 2
    remaining = _all_rows(conn, "Needs Tagging")
    assert len(remaining) == 1
    assert remaining[0]["Group Key"] == "C|0|1|Z"


# ---------------------------------------------------------------------------
# upsert_dashboard_row
# ---------------------------------------------------------------------------

from engine.compute import DashboardRow  # noqa: E402


def _dashboard_row(**overrides) -> DashboardRow:
    base = dict(
        contract_name="Acme",
        asana_task_gid="gid-acme-001",
        campus_set="CEN, OMH",
        contract_amount=10000.0,
        spent_so_far=5000.0,
        pct_spent=50.0,
        spending_rate=1.0,
        spending_rate_alarm=None,
        alarms="Clear",
        start=date(2026, 1, 1),
        due=date(2026, 12, 31),
        status="Active",
        pm_email="pm@example.com",
        last_updated=date(2026, 6, 12),
    )
    base.update(overrides)
    return DashboardRow(**base)


def test_upsert_dashboard_creates_new_row_when_no_gid_match(conn):
    result = upsert_dashboard_row(conn, _dashboard_row())
    assert result["fields"]["Contract"] == "Acme"
    assert result["fields"]["Asana Task GID"] == "gid-acme-001"
    assert result["fields"]["% Spent"] == 50.0
    assert result["fields"]["Alarms"] == "Clear"
    assert result["fields"]["Start"] == "2026-01-01"
    assert len(_all_rows(conn, "Dashboard")) == 1


def test_upsert_dashboard_updates_existing_row_by_gid(conn):
    """Idempotency: same GID → update, not duplicate."""
    upsert_dashboard_row(conn, _dashboard_row(spent_so_far=1000.0, pct_spent=10.0))
    upsert_dashboard_row(conn, _dashboard_row(
        spent_so_far=8000.0, pct_spent=80.0,
        spending_rate_alarm="75%", alarms="ALARM",
    ))
    rows = _all_rows(conn, "Dashboard")
    assert len(rows) == 1
    assert rows[0]["Spent so far"] == 8000.0
    assert rows[0]["% Spent"] == 80.0
    assert rows[0]["Spending Rate Alarm"] == "75%"
    assert rows[0]["Alarms"] == "ALARM"


def test_upsert_dashboard_stores_none_fields_as_null(conn):
    """When the pace guard blanks Spending Rate or a contract has no
    amount, those fields land as NULL in SQLite — read back as None."""
    upsert_dashboard_row(conn, _dashboard_row(
        contract_amount=None,
        pct_spent=None,
        spending_rate=None,
        spending_rate_alarm=None,
    ))
    rec = _all_rows(conn, "Dashboard")[0]
    assert rec["Contract Amount"] is None
    assert rec["% Spent"] is None
    assert rec["Spending Rate"] is None
    assert rec["Spending Rate Alarm"] is None
    # The unconditional fields ARE present.
    assert rec["Spent so far"] is not None
    assert rec["Alarms"] == "Clear"


def test_upsert_dashboard_rejects_unknown_spending_rate_alarm(conn):
    """Client-side validation prevents a typo from silently landing in
    the DB. SQLite has no native enum type, so this guard at the
    application layer is what keeps the data clean."""
    with pytest.raises(ValueError, match="Spending Rate Alarm"):
        upsert_dashboard_row(conn, _dashboard_row(spending_rate_alarm="ALMOST"))


def test_upsert_dashboard_rejects_unknown_alarms_value(conn):
    with pytest.raises(ValueError, match="Alarms"):
        upsert_dashboard_row(conn, _dashboard_row(alarms="MAYBE"))


def test_upsert_dashboard_accepts_apostrophe_in_contract_name(conn):
    """Regression: a contract name like Domino's Pizza round-trips
    through the keyed lookup without misquoting (SQLite parameterized
    queries handle this natively)."""
    upsert_dashboard_row(conn, _dashboard_row(
        contract_name="Domino's Pizza",
        asana_task_gid="gid-with-quote",
    ))
    upsert_dashboard_row(conn, _dashboard_row(
        contract_name="Domino's Pizza",
        asana_task_gid="gid-with-quote",
        spent_so_far=999.0,
    ))
    rows = _all_rows(conn, "Dashboard")
    assert len(rows) == 1
    assert rows[0]["Spent so far"] == 999.0


def test_upsert_dashboard_clears_cells_when_value_transitions_to_none(conn):
    """A Dashboard cell that goes from a non-None value back to None
    must be CLEARED on update, not left stale. Classic failure mode:
    Spending Rate Alarm showing '75%' forever after the operator raised
    Contract Amount in Asana.

    SQLite's ON CONFLICT ... DO UPDATE SET col = excluded.col writes
    the NEW value (which is NULL) uniformly — there's no PATCH-merge
    gotcha to handle explicitly, unlike the Airtable era. Test stays as
    a regression pin so a future code change can't reintroduce the
    failure mode (e.g. someone adds COALESCE to "preserve" old values).
    """
    # Run 1: alarm tripping at 75% band.
    upsert_dashboard_row(conn, _dashboard_row(
        pct_spent=80.0, spending_rate=1.5,
        spending_rate_alarm="75%", alarms="ALARM",
    ))
    # Run 2: operator raised Contract Amount; band drops to None.
    upsert_dashboard_row(conn, _dashboard_row(
        pct_spent=30.0, spending_rate=0.5,
        spending_rate_alarm=None,
        alarms="Clear",
    ))
    rec = _all_rows(conn, "Dashboard")[0]
    assert rec["Spending Rate Alarm"] is None, (
        f"Spending Rate Alarm cell was not cleared on update; got "
        f"{rec['Spending Rate Alarm']!r}. UPSERT-merge bug."
    )
    assert rec["Alarms"] == "Clear"
    assert rec["% Spent"] == 30.0


def test_upsert_dashboard_clears_contract_amount_when_removed(conn):
    """Same family as the stale-cell bug above — Contract Amount removed
    in Asana must clear the Dashboard cell."""
    upsert_dashboard_row(conn, _dashboard_row(contract_amount=10000.0))
    upsert_dashboard_row(conn, _dashboard_row(
        contract_amount=None,
        pct_spent=None, spending_rate=None,
        spending_rate_alarm=None,
    ))
    rec = _all_rows(conn, "Dashboard")[0]
    assert rec["Contract Amount"] is None


def test_dashboard_singleSelect_validators_match_settings_options():
    """Three sources of truth must stay in lock-step:
    1. settings.ASANA_SPENDING_RATE_ALARM_OPTIONS / ASANA_ALARMS_OPTIONS
    2. config.schema field_spec choices for Dashboard / State select fields
    3. engine.sqlite_client._DASHBOARD_* / _STATE_* validators

    A divergence means the client validator could reject a valid Asana
    option (false negative) or accept a stale one (false positive). Pin
    them equal here so a future edit to one fails CI.
    """
    from config import schema, settings
    from engine.sqlite_client import (
        _DASHBOARD_ALARMS_VALUES,
        _DASHBOARD_SPENDING_RATE_ALARM_VALUES,
    )

    assert _DASHBOARD_SPENDING_RATE_ALARM_VALUES == frozenset(
        settings.ASANA_SPENDING_RATE_ALARM_OPTIONS
    )
    assert _DASHBOARD_ALARMS_VALUES == frozenset(settings.ASANA_ALARMS_OPTIONS)

    sra_schema_names = [
        c["name"]
        for c in schema.field_spec(
            "Dashboard", "Spending Rate Alarm"
        )["options"]["choices"]
    ]
    assert set(sra_schema_names) == set(settings.ASANA_SPENDING_RATE_ALARM_OPTIONS)

    alarms_schema_names = [
        c["name"]
        for c in schema.field_spec("Dashboard", "Alarms")["options"]["choices"]
    ]
    assert set(alarms_schema_names) == set(settings.ASANA_ALARMS_OPTIONS)


def test_upsert_dashboard_writes_all_fourteen_fields(conn):
    """Defensive: when a DashboardRow has every optional field set, all
    14 schema columns should land populated. A regression that drops one
    silently (e.g. someone removes 'Status' on a refactor) is caught
    here."""
    upsert_dashboard_row(conn, _dashboard_row(
        contract_name="Acme",
        asana_task_gid="gid-full",
        campus_set="CEN, OMH",
        contract_amount=10000.0,
        spent_so_far=5000.0,
        pct_spent=50.0,
        spending_rate=1.0,
        spending_rate_alarm=None,  # validly None — band not reached
        alarms="Clear",
        start=date(2026, 1, 1),
        due=date(2026, 12, 31),
        status="Active",
        pm_email="pm@example.com",
        last_updated=date(2026, 6, 12),
    ))
    fields = _all_rows(conn, "Dashboard")[0]
    for key in (
        "Contract", "Asana Task GID", "Campus Set", "Contract Amount",
        "Spent so far", "% Spent", "Spending Rate", "Alarms",
        "Start", "Due", "Status", "PM Email", "Last Updated",
    ):
        assert fields[key] is not None, (
            f"Dashboard payload missing or NULL for key {key!r}"
        )
    # And the None one stays NULL.
    assert fields["Spending Rate Alarm"] is None


# ---------------------------------------------------------------------------
# State table I/O
# ---------------------------------------------------------------------------

def test_load_state_priors_parses_state_rows(conn):
    """State table keyed by Asana Task GID. Rows missing the GID are
    skipped with a logged warning (legacy / hand-edited rows)."""
    _seed(conn, "State", {
        "Contract Name": "Acme SaaS",
        "Asana Task GID": "gid-acme",
        "Prior Spent": 1234.56,
        "Prior % Spent": 12.35,
        "Prior Spending Rate": 1.5,
        "Prior Spending Rate Alarm": "75%",
        "Prior Alarms": "ALARM",
        "Last Processed Hash": "abc123",
        "Last Updated At": "2026-06-11",
    })
    _seed(conn, "State", {  # legacy row without GID — skipped
        "Contract Name": "Legacy",
        "Prior Spent": 100.0,
    })
    priors = load_state_priors(conn)
    assert set(priors) == {"gid-acme"}
    p = priors["gid-acme"]
    assert p.contract_name == "Acme SaaS"
    assert p.asana_task_gid == "gid-acme"
    assert p.prior_spent == pytest.approx(1234.56)
    assert p.prior_pct_spent == pytest.approx(12.35)
    assert p.prior_spending_rate_alarm == "75%"
    assert p.prior_alarms == "ALARM"
    assert p.last_processed_hash == "abc123"
    assert p.last_updated_at == date(2026, 6, 11)


def test_cleanup_stale_state_deletes_rows_not_in_live_set(conn):
    """A contract archived in Asana between runs leaves its State row
    orphaned. Engine-owned table, operator doesn't hand-edit; sweep is
    safe."""
    _seed(conn, "State", {"Contract Name": "Live", "Asana Task GID": "gid-live"})
    _seed(conn, "State", {"Contract Name": "Stale", "Asana Task GID": "gid-stale"})
    _seed(conn, "State", {"Contract Name": "Other Stale", "Asana Task GID": "gid-other"})
    deleted = cleanup_stale_state(conn, live_asana_task_gids={"gid-live"})
    assert deleted == 2
    remaining = _all_rows(conn, "State")
    assert len(remaining) == 1
    assert remaining[0]["Asana Task GID"] == "gid-live"


def test_load_state_priors_returns_empty_dict_on_empty_table(conn):
    """First run: every contract surfaces as `first_run` in the diff."""
    assert load_state_priors(conn) == {}


def test_upsert_state_creates_new_row_for_first_seen_contract(conn):
    upsert_state_for_contract(
        conn,
        contract_name="Acme", asana_task_gid="gid-acme",
        spent=1234.56, pct_spent=12.35, spending_rate=1.5,
        spending_rate_alarm="75%", alarms="ALARM",
        last_processed_hash="hash-1",
        last_updated_iso_date="2026-06-12",
    )
    rows = _all_rows(conn, "State")
    assert len(rows) == 1
    r = rows[0]
    assert r["Contract Name"] == "Acme"
    assert r["Asana Task GID"] == "gid-acme"
    assert r["Prior Spent"] == 1234.56
    assert r["Prior Spending Rate Alarm"] == "75%"
    assert r["Prior Alarms"] == "ALARM"
    assert r["Last Processed Hash"] == "hash-1"


def test_upsert_state_updates_existing_by_asana_task_gid(conn):
    """Idempotency by Asana Task GID — the stable identity. A rename on
    the Contract Name side still updates the same State row."""
    upsert_state_for_contract(
        conn, contract_name="Acme", asana_task_gid="gid-acme",
        spent=1000.0, pct_spent=10.0, spending_rate=0.5,
        spending_rate_alarm=None, alarms="Clear",
        last_processed_hash="hash-1", last_updated_iso_date="2026-06-11",
    )
    upsert_state_for_contract(
        conn, contract_name="Acme Inc.", asana_task_gid="gid-acme",
        spent=2000.0, pct_spent=20.0, spending_rate=1.0,
        spending_rate_alarm=None, alarms="Clear",
        last_processed_hash="hash-2", last_updated_iso_date="2026-06-12",
    )
    rows = _all_rows(conn, "State")
    assert len(rows) == 1, "Rename created a duplicate — GID keying broken"
    assert rows[0]["Contract Name"] == "Acme Inc."
    assert rows[0]["Prior Spent"] == 2000.0
    assert rows[0]["Last Processed Hash"] == "hash-2"


def test_upsert_state_clears_nullable_cells_on_update(conn):
    """Same regression-pin pattern as Dashboard: a Prior Spending Rate
    Alarm transitioning from '75%' back to None must be cleared."""
    upsert_state_for_contract(
        conn, contract_name="Acme", asana_task_gid="gid-acme",
        spent=8000.0, pct_spent=80.0, spending_rate=1.5,
        spending_rate_alarm="75%", alarms="ALARM",
        last_processed_hash="h1", last_updated_iso_date="2026-06-11",
    )
    upsert_state_for_contract(
        conn, contract_name="Acme", asana_task_gid="gid-acme",
        spent=3000.0, pct_spent=30.0,
        spending_rate=None, spending_rate_alarm=None,
        alarms="Clear",
        last_processed_hash="h2", last_updated_iso_date="2026-06-12",
    )
    r = _all_rows(conn, "State")[0]
    assert r["Prior Spending Rate Alarm"] is None
    assert r["Prior Spending Rate"] is None
    assert r["Prior Alarms"] == "Clear"


def test_upsert_state_rejects_unknown_alarms_option(conn):
    with pytest.raises(ValueError, match="Prior Alarms"):
        upsert_state_for_contract(
            conn, contract_name="Acme", asana_task_gid="gid-acme",
            spent=0.0, pct_spent=None, spending_rate=None,
            spending_rate_alarm=None, alarms="WRONG",
            last_processed_hash="x", last_updated_iso_date="2026-06-12",
        )


def test_upsert_state_rejects_unknown_band_option(conn):
    with pytest.raises(ValueError, match="Prior Spending Rate Alarm"):
        upsert_state_for_contract(
            conn, contract_name="Acme", asana_task_gid="gid-acme",
            spent=0.0, pct_spent=None, spending_rate=None,
            spending_rate_alarm="ALMOST",
            alarms="Clear",
            last_processed_hash="x", last_updated_iso_date="2026-06-12",
        )


# ---------------------------------------------------------------------------
# prune_run_log_older_than
# ---------------------------------------------------------------------------

def _seed_run_log(conn, rows: list[dict]) -> None:
    """Seed Run Log records with explicit Run IDs and a `tag` carried in
    Notes for test-readable assertions."""
    for r in rows:
        _seed(conn, "Run Log",
              {"Run ID": r["run_id"], "Mode": "ingest", "Outcome": "ok",
               "Notes": r.get("tag", "")})


def test_prune_run_log_disabled_when_retention_zero(conn):
    """RUN_LOG_RETENTION_DAYS=0 → true no-op."""
    _seed_run_log(conn, [
        {"run_id": "2020-01-01T00:00:00+00:00", "tag": "ancient"},
        {"run_id": "2026-06-15T00:00:00+00:00", "tag": "today"},
    ])
    deleted = prune_run_log_older_than(conn, retention_days=0,
                                       today=date(2026, 6, 15))
    assert deleted == 0
    assert len(_all_rows(conn, "Run Log")) == 2


def test_prune_run_log_deletes_rows_older_than_cutoff(conn):
    """30-day retention against today=2026-06-15 → everything strictly
    before 2026-05-16 is gone; boundary row stays."""
    _seed_run_log(conn, [
        {"run_id": "2020-01-01T12:34:56+00:00", "tag": "ancient"},
        {"run_id": "2026-05-15T23:59:59+00:00", "tag": "just-too-old"},
        {"run_id": "2026-05-16T00:00:01+00:00", "tag": "boundary-keep"},
        {"run_id": "2026-06-14T23:00:00+00:00", "tag": "yesterday"},
        {"run_id": "2026-06-15T08:00:00+00:00", "tag": "today"},
    ])
    deleted = prune_run_log_older_than(conn, retention_days=30,
                                       today=date(2026, 6, 15))
    assert deleted == 2
    remaining_tags = {r["Notes"] for r in _all_rows(conn, "Run Log")}
    assert remaining_tags == {"boundary-keep", "yesterday", "today"}


def test_prune_run_log_leaves_malformed_run_ids_in_place(conn):
    """A row whose Run ID isn't a parseable ISO date is NOT deleted —
    we never nuke a row we can't read the timestamp from."""
    _seed_run_log(conn, [
        {"run_id": "not-an-iso-timestamp", "tag": "hand-edit"},
        # Empty string can't go via _seed_run_log + UNIQUE; use a plain
        # insert to bypass the Run ID null/blank uniqueness check (SQLite
        # treats NULL as distinct under UNIQUE).
    ])
    # Insert a NULL Run ID row directly — sqlite's UNIQUE treats NULLs
    # as distinct, so this doesn't conflict with the seed above.
    _seed(conn, "Run Log",
          {"Run ID": None, "Mode": "ingest", "Outcome": "ok", "Notes": "blank"})
    _seed_run_log(conn, [
        {"run_id": "2020-01-01T00:00:00+00:00", "tag": "ancient-but-parseable"},
    ])
    deleted = prune_run_log_older_than(conn, retention_days=30,
                                       today=date(2026, 6, 15))
    assert deleted == 1
    remaining_tags = {r["Notes"] for r in _all_rows(conn, "Run Log")}
    assert remaining_tags == {"hand-edit", "blank"}


def test_prune_run_log_keeps_everything_when_window_far_exceeds_history(conn):
    """365-day retention against a week-old base must delete nothing."""
    _seed_run_log(conn, [
        {"run_id": "2026-06-10T00:00:00+00:00", "tag": "a"},
        {"run_id": "2026-06-11T00:00:00+00:00", "tag": "b"},
        {"run_id": "2026-06-12T00:00:00+00:00", "tag": "c"},
    ])
    deleted = prune_run_log_older_than(conn, retention_days=365,
                                       today=date(2026, 6, 15))
    assert deleted == 0
    assert len(_all_rows(conn, "Run Log")) == 3
