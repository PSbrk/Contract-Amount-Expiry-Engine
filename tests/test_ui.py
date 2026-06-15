"""Tests for engine.ui — Flask routes against an in-memory SQLite database.

Uses the app factory's `conn=` mode so every request shares a single
in-memory database. check_same_thread is set False so Flask's test
client (which runs in the same thread anyway) doesn't trip on sqlite3's
per-connection thread restriction.

DoD coverage: the Phase 3 plan says "operator fills Assign Contract on
a Needs Tagging row → refresh shows the change → next --ingest promotes
it correctly". The first two are unit-testable here; the third is
covered by tests/test_sqlite_client.py's
test_promote_filled_creates_learned_and_deletes_nt and friends.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from engine import sqlite_client
from engine.compute import DashboardRow
from engine.sqlite_client import (
    append_run_log,
    ensure_schema,
    upsert_dashboard_row,
    upsert_needs_tagging_group,
)
from engine.ui import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    """Single in-memory SQLite connection shared across requests in the
    test client. check_same_thread=False because Flask's test_client
    runs handlers in the same thread as the test, but the connection
    object guards against cross-thread use by default."""
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    yield c
    c.close()


@pytest.fixture
def client(conn):
    app = create_app(conn=conn)
    app.testing = True
    return app.test_client()


def _seed_dashboard_row(conn, **overrides) -> None:
    base = dict(
        contract_name="Acme",
        asana_task_gid="gid-acme",
        campus_set="CEN",
        contract_amount=10000.0,
        spent_so_far=2500.0,
        pct_spent=25.0,
        spending_rate=0.5,
        spending_rate_alarm=None,
        alarms="Clear",
        start=date(2026, 1, 1),
        due=date(2026, 12, 31),
        status="Active",
        pm_email="pm@example.com",
        last_updated=date(2026, 6, 15),
    )
    base.update(overrides)
    upsert_dashboard_row(conn, DashboardRow(**base))


def _seed_needs_tagging(conn, **overrides) -> int:
    """Insert one Needs Tagging row via the public upsert helper.
    Returns the row id so tests can POST against it."""
    fields = dict(
        group_key="CEN|000|63015|Mystery",
        campus="CEN", dept="000", account_no="63015", vendor="Mystery",
        sample_description="MYSTERY VENDOR INVOICE",
        amount=1234.56,
        candidate_names=["Acme"],
        created_at_iso_date="2026-06-12",
    )
    fields.update(overrides)
    rec = upsert_needs_tagging_group(conn, **fields)
    return rec["id"]


# ---------------------------------------------------------------------------
# / — Dashboard
# ---------------------------------------------------------------------------

def test_dashboard_renders_empty_state(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Dashboard" in body
    assert "No Dashboard rows yet" in body


def test_dashboard_lists_seeded_contracts(client, conn):
    _seed_dashboard_row(conn, contract_name="Acme", spent_so_far=1000.0)
    _seed_dashboard_row(conn,
                        contract_name="Beta", asana_task_gid="gid-beta",
                        spent_so_far=8000.0, pct_spent=80.0,
                        spending_rate_alarm="75%", alarms="ALARM")
    resp = client.get("/")
    body = resp.get_data(as_text=True)
    assert "Acme" in body
    assert "Beta" in body
    # ALARM row gets the alarm class applied; the count chip appears.
    assert "1 ALARM" in body


def test_dashboard_sorts_alarm_rows_first(client, conn):
    """A contract in ALARM must render BEFORE a Clear contract regardless
    of name/percent. Operator scanning the page needs the actionable
    rows at the top."""
    _seed_dashboard_row(conn,
                        contract_name="AAA Clear", asana_task_gid="gid-aaa",
                        pct_spent=10.0, alarms="Clear")
    _seed_dashboard_row(conn,
                        contract_name="ZZZ Alarm", asana_task_gid="gid-zzz",
                        pct_spent=80.0,
                        spending_rate_alarm="75%", alarms="ALARM")
    resp = client.get("/")
    body = resp.get_data(as_text=True)
    # ZZZ Alarm appears earlier in the rendered HTML than AAA Clear.
    assert body.index("ZZZ Alarm") < body.index("AAA Clear")


# ---------------------------------------------------------------------------
# /needs-tagging — list + inline edit
# ---------------------------------------------------------------------------

def test_needs_tagging_renders_empty_state(client):
    resp = client.get("/needs-tagging")
    assert resp.status_code == 200
    assert "No Needs Tagging rows" in resp.get_data(as_text=True)


def test_needs_tagging_lists_seeded_rows_with_form(client, conn):
    rid = _seed_needs_tagging(conn, vendor="ACME WIDGETS")
    resp = client.get("/needs-tagging")
    body = resp.get_data(as_text=True)
    assert "ACME WIDGETS" in body
    # The Assign Contract input and submit live on a real form pointing
    # at the per-row save endpoint.
    assert f'/needs-tagging/{rid}' in body
    assert 'name="assign_contract"' in body


def test_needs_tagging_save_persists_contract_name(client, conn):
    """The Phase 3 DoD: POST writes the Assign Contract column; a
    follow-up GET shows the new value."""
    rid = _seed_needs_tagging(conn)
    resp = client.post(
        f"/needs-tagging/{rid}",
        data={"assign_contract": "Acme SaaS Contract"},
        follow_redirects=False,
    )
    # 302 → /needs-tagging
    assert resp.status_code == 302
    assert "/needs-tagging" in resp.headers["Location"]
    # The value lands in SQLite.
    row = conn.execute(
        'SELECT "Assign Contract" FROM "Needs Tagging" WHERE id = ?', (rid,)
    ).fetchone()
    assert row["Assign Contract"] == "Acme SaaS Contract"


def test_needs_tagging_save_round_trip_shows_value_on_refresh(client, conn):
    """End-to-end: POST then GET; the saved value renders in the form."""
    rid = _seed_needs_tagging(conn)
    client.post(
        f"/needs-tagging/{rid}",
        data={"assign_contract": "Acme SaaS Contract"},
    )
    resp = client.get("/needs-tagging")
    body = resp.get_data(as_text=True)
    assert 'value="Acme SaaS Contract"' in body


def test_needs_tagging_save_can_clear_a_prior_answer(client, conn):
    """Posting an empty string clears the cell — operator changed their
    mind. Stored as '' (not NULL) but the unfilled row class still
    triggers since the route's filter uses TRIM(...) = ''."""
    rid = _seed_needs_tagging(conn)
    # First fill it.
    client.post(f"/needs-tagging/{rid}", data={"assign_contract": "X"})
    # Then clear it.
    client.post(f"/needs-tagging/{rid}", data={"assign_contract": ""})
    row = conn.execute(
        'SELECT "Assign Contract" FROM "Needs Tagging" WHERE id = ?', (rid,)
    ).fetchone()
    assert (row["Assign Contract"] or "") == ""


def test_needs_tagging_save_404s_for_unknown_id(client):
    """A stale tab posting against a row that was cleaned up between
    requests must 404, not silently INSERT or corrupt the table."""
    resp = client.post(
        "/needs-tagging/9999",
        data={"assign_contract": "Anything"},
    )
    assert resp.status_code == 404


def test_needs_tagging_datalist_pulls_from_dashboard_contracts(client, conn):
    """The datalist of contract-name suggestions comes from the
    Dashboard table — those names are by definition valid open
    contracts that the last --ingest computed."""
    _seed_dashboard_row(conn, contract_name="Acme SaaS",
                        asana_task_gid="gid-acme")
    _seed_dashboard_row(conn, contract_name="Beta Tools",
                        asana_task_gid="gid-beta")
    _seed_needs_tagging(conn)
    resp = client.get("/needs-tagging")
    body = resp.get_data(as_text=True)
    assert '<datalist id="contract-names">' in body
    assert 'value="Acme SaaS"' in body
    assert 'value="Beta Tools"' in body


def test_needs_tagging_unfilled_row_gets_highlight_class(client, conn):
    """Operator scanning the page should see un-answered rows clearly
    — they get a CSS class the base template styles with a background."""
    _seed_needs_tagging(conn)  # default: Assign Contract is NULL
    resp = client.get("/needs-tagging")
    body = resp.get_data(as_text=True)
    assert 'row-unfilled' in body


# ---------------------------------------------------------------------------
# /run-log
# ---------------------------------------------------------------------------

def test_run_log_renders_empty_state(client):
    resp = client.get("/run-log")
    assert resp.status_code == 200
    assert "No Run Log rows yet" in resp.get_data(as_text=True)


def test_run_log_lists_seeded_rows_newest_first(client, conn):
    append_run_log(conn, run_id="2026-06-10T08:00:00+00:00",
                   mode="ingest", outcome="ok", file_name="old.csv")
    append_run_log(conn, run_id="2026-06-15T08:00:00+00:00",
                   mode="ingest", outcome="error", file_name="new.csv")
    resp = client.get("/run-log")
    body = resp.get_data(as_text=True)
    # Newest first — 2026-06-15 row appears before 2026-06-10.
    assert body.index("new.csv") < body.index("old.csv")
    assert "error" in body


def test_run_log_pagination_clamps_negative_offset(client, conn):
    """Defensive: ?offset=-1 must not produce a negative LIMIT clause."""
    append_run_log(conn, run_id="2026-06-15T08:00:00+00:00",
                   mode="ingest", outcome="ok")
    resp = client.get("/run-log?offset=-5")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /dashboard-detail/<gid>
# ---------------------------------------------------------------------------

def test_dashboard_detail_renders_for_known_gid(client, conn):
    _seed_dashboard_row(conn, contract_name="Acme SaaS",
                        asana_task_gid="gid-acme",
                        spent_so_far=15000.0, pct_spent=75.0)
    resp = client.get("/dashboard-detail/gid-acme")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Acme SaaS" in body
    assert "gid-acme" in body
    assert "Live values" in body
    # The empty-state messages appear when there are no linked rows.
    assert "No Learned Mappings" in body
    assert "No Vendor Aliases" in body


def test_dashboard_detail_lists_linked_learned_mappings_and_aliases(client, conn):
    _seed_dashboard_row(conn, contract_name="Acme SaaS",
                        asana_task_gid="gid-acme")
    # Seed a learned mapping pointing at this contract.
    from engine.sqlite_client import (
        insert_learned_mapping,
        insert_vendor_alias,
    )
    insert_learned_mapping(
        conn, key="CEN|000|63015|ACME",
        campus="CEN", dept="000", account_no="63015", vendor="ACME",
        contract_name="Acme SaaS",
    )
    insert_vendor_alias(
        conn, contract_name="Acme SaaS", aliases="ACME, ACME INC",
    )
    resp = client.get("/dashboard-detail/gid-acme")
    body = resp.get_data(as_text=True)
    assert "CEN|000|63015|ACME" in body
    assert "ACME, ACME INC" in body


def test_dashboard_detail_404s_for_unknown_gid(client):
    resp = client.get("/dashboard-detail/gid-does-not-exist")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Admin tables — CRUD round-trip via /vendor-aliases as representative
# ---------------------------------------------------------------------------

def test_vendor_aliases_empty_state_renders(client):
    resp = client.get("/vendor-aliases")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Vendor Aliases" in body
    # Add-row form has the Contract Name input.
    assert 'name="contract_name"' in body


def test_vendor_aliases_add_then_list(client, conn):
    resp = client.post(
        "/vendor-aliases",
        data={"contract_name": "Acme", "aliases": "ACME, ACME INC", "notes": ""},
    )
    assert resp.status_code == 302
    # Listed on the next GET.
    body = client.get("/vendor-aliases").get_data(as_text=True)
    assert "Acme" in body
    assert "ACME, ACME INC" in body


def test_vendor_aliases_update_row(client, conn):
    rec = sqlite_client.insert_vendor_alias(conn, contract_name="Old", aliases="A")
    rid = rec["id"]
    resp = client.post(
        f"/vendor-aliases/{rid}",
        data={"contract_name": "New", "aliases": "X, Y", "notes": "edited"},
    )
    assert resp.status_code == 302
    row = conn.execute(
        'SELECT "Contract Name", "Aliases", "Notes" FROM "Vendor Aliases" WHERE id = ?',
        (rid,),
    ).fetchone()
    assert row["Contract Name"] == "New"
    assert row["Aliases"] == "X, Y"
    assert row["Notes"] == "edited"


def test_vendor_aliases_delete_row(client, conn):
    rec = sqlite_client.insert_vendor_alias(conn, contract_name="Doomed")
    rid = rec["id"]
    resp = client.post(f"/vendor-aliases/{rid}/delete")
    assert resp.status_code == 302
    assert conn.execute(
        'SELECT COUNT(*) AS c FROM "Vendor Aliases" WHERE id = ?', (rid,)
    ).fetchone()["c"] == 0


def test_vendor_aliases_save_404s_unknown_id(client):
    resp = client.post("/vendor-aliases/9999",
                       data={"contract_name": "x", "aliases": "", "notes": ""})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Campus Map — Drop checkbox + dup-key behavior
# ---------------------------------------------------------------------------

def test_campus_map_checkbox_round_trips(client, conn):
    """Browsers send 'on' for a checked checkbox and OMIT the field when
    unchecked. The Add form must translate both to 0/1 correctly."""
    client.post("/campus-map", data={
        "tableau_code": "CEN",
        "asana_option_names": "CEN",
        "notes": "",
        # no `drop` key — unchecked
    })
    client.post("/campus-map", data={
        "tableau_code": "ZZZ",
        "asana_option_names": "",
        "drop": "on",  # checked
        "notes": "drop these",
    })
    rows = {r["Tableau Code"]: r for r in conn.execute(
        'SELECT * FROM "Campus Map"'
    )}
    assert rows["CEN"]["Drop"] == 0
    assert rows["ZZZ"]["Drop"] == 1


def test_campus_map_add_duplicate_tableau_code_flashes_error(client, conn):
    """UNIQUE constraint on Tableau Code → the add route catches the
    IntegrityError and flashes a friendly message instead of 500-ing."""
    client.post("/campus-map", data={
        "tableau_code": "CEN", "asana_option_names": "CEN", "notes": "",
    })
    # Duplicate Tableau Code.
    resp = client.post("/campus-map", data={
        "tableau_code": "CEN", "asana_option_names": "other", "notes": "",
    }, follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert "Could not add" in body
    # Only one row landed.
    cnt = conn.execute('SELECT COUNT(*) AS c FROM "Campus Map"').fetchone()["c"]
    assert cnt == 1


# ---------------------------------------------------------------------------
# Learned Mappings — quick smoke (admin route helper is shared)
# ---------------------------------------------------------------------------

def test_learned_mappings_add_then_list(client, conn):
    client.post("/learned-mappings", data={
        "key": "CEN|000|63015|ACME",
        "campus": "CEN", "dept": "000",
        "account_no": "63015", "vendor": "ACME",
        "contract_name": "Acme SaaS",
        "learned_at": "2026-06-15",
        "notes": "added manually",
    })
    body = client.get("/learned-mappings").get_data(as_text=True)
    assert "CEN|000|63015|ACME" in body
    assert "Acme SaaS" in body


# ---------------------------------------------------------------------------
# /state and /settings (read-only)
# ---------------------------------------------------------------------------

def test_state_view_empty(client):
    resp = client.get("/state")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "State" in body
    assert "No State rows yet" in body


def test_state_view_lists_seeded_rows(client, conn):
    from engine.sqlite_client import upsert_state_for_contract
    upsert_state_for_contract(
        conn, contract_name="Acme", asana_task_gid="gid-acme",
        spent=1234.0, pct_spent=12.0, spending_rate=0.5,
        spending_rate_alarm=None, alarms="Clear",
        last_processed_hash="abc123def456",
        last_updated_iso_date="2026-06-15",
    )
    body = client.get("/state").get_data(as_text=True)
    assert "Acme" in body
    # Hash truncated to 12 chars per the template.
    assert "abc123def456" in body


def test_settings_view_shows_grouped_constants_and_env_presence(client, monkeypatch):
    """Pin the settings page renders all groups + the env-state list.
    ASANA_PAT is forced set / unset to check both render paths."""
    monkeypatch.setenv("ASANA_PAT", "fake-pat-value")
    monkeypatch.delenv("AIRTABLE_PAT", raising=False)
    body = client.get("/settings").get_data(as_text=True)
    assert "ASANA_WORKSPACE_GID" in body
    assert "ACCOUNTS_IN_SCOPE" in body
    assert "TRANSACTION_SOURCE" in body
    assert "ASANA_PAT" in body
    # Set / not-set markers present.
    assert "set" in body
    assert "not set" in body
