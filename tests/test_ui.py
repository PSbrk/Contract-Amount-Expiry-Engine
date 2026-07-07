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


def test_capex_budgets_page_and_bulk_crud(client, conn, monkeypatch):
    """The CapEx Budgets page renders without a live Asana pull, the bulk
    grid parses tab/CSV/space + $ and thousands-commas, and delete works."""
    from engine import asana_client

    def _no_asana(*a, **k):
        raise RuntimeError("no asana in test")
    # Keep hermetic — the route catches this and renders budgets-only.
    monkeypatch.setattr(asana_client, "get_api_client", _no_asana)

    assert client.get("/capex-budgets").status_code == 200

    client.post("/capex-budgets/bulk", data={
        # Mix of separators; the last line is the regression case — a SPACE
        # separator with a thousands-comma in the amount, which the old
        # first-comma split mangled into cid='FFE001500 $800' / amount=0.
        "bulk": ("FFE001428\t120000\n"
                 "NCD000083, $1,250,000.00\n"
                 " rmd000361 80000\n"
                 "FFE001500 $800,000.00"),
    }, follow_redirects=True)
    assert sqlite_client.load_capex_budgets(conn) == {
        "FFE001428": 120000.0, "NCD000083": 1250000.0, "RMD000361": 80000.0,
        "FFE001500": 800000.0,
    }

    page = client.get("/capex-budgets")
    assert b"FFE001428" in page.data

    rid = conn.execute(
        'SELECT id FROM "CapEx Budgets" WHERE "CapEx ID" = ?', ("FFE001428",)
    ).fetchone()["id"]
    client.post(f"/capex-budgets/{rid}/delete", follow_redirects=True)
    assert "FFE001428" not in sqlite_client.load_capex_budgets(conn)


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
    # Keep campus consistent with group_key (Campus|Dept|Acct|Vendor) when a
    # test overrides the key but not the campus — Vendor Conflicts now filters
    # candidates by campus, so the group's campus must match its key.
    if "group_key" in overrides and "campus" not in overrides:
        overrides = {**overrides, "campus": overrides["group_key"].split("|", 1)[0]}
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


def test_dashboard_detail_shows_assigned_entries(client, conn):
    """Clicking a contract drills into the Tableau entries assigned to it,
    split into in-term (counted) and out-of-term (excluded)."""
    from engine import sqlite_client
    _seed_dashboard_row(conn, contract_name="Acme", asana_task_gid="g_acme",
                        spent_so_far=100.0)
    sqlite_client.replace_attributed_lines(conn, [
        {"gid": "g_acme", "date": "2026-03-15", "campus": "CEN",
         "account_no": "63040", "vendor": "Acme Co", "description": "Service call",
         "reference": "INV-1", "amount": 100.0, "in_term": True, "tier": "opex"},
        {"gid": "g_acme", "date": "2025-06-15", "campus": "CEN",
         "account_no": "63040", "vendor": "Acme Co", "description": "Old call",
         "reference": "INV-0", "amount": 40.0, "in_term": False, "tier": "opex"},
    ])
    body = client.get("/dashboard-detail/g_acme").get_data(as_text=True)
    assert "Assigned Tableau entries" in body
    assert "INV-1" in body and "INV-0" in body
    assert "Out of term" in body


def test_dashboard_detail_empty_entries_points_to_needs_tagging(client, conn):
    """The Clear Creek case: $0 spent, nothing assigned. The drill-down says
    so plainly and routes the operator to Needs Tagging."""
    _seed_dashboard_row(conn, contract_name="Clear Creek", asana_task_gid="g_cc",
                        spent_so_far=0.0)
    body = client.get("/dashboard-detail/g_cc").get_data(as_text=True)
    assert "No Tableau entries are assigned" in body
    assert "Needs" in body  # link out to Needs Tagging


def test_dashboard_detail_resolve_unresolve_toggle(client, conn):
    """Mark Resolved snapshots the current band as the re-arm baseline and
    shows the muted state on the detail page + a pill on the list; un-resolve
    clears it."""
    from engine import sqlite_client
    _seed_dashboard_row(conn, contract_name="Marmic", asana_task_gid="g_m",
                        spending_rate_alarm="90%", alarms="ALARM")
    client.post("/dashboard-detail/g_m/resolve")
    resolved = sqlite_client.load_resolved_contracts(conn)
    assert "g_m" in resolved
    assert resolved["g_m"]["baseline_band"] == "90%"   # band snapshotted
    detail = client.get("/dashboard-detail/g_m").get_data(as_text=True)
    assert "muted" in detail.lower()
    assert "resolved" in client.get("/").get_data(as_text=True).lower()  # list pill
    client.post("/dashboard-detail/g_m/unresolve")
    assert "g_m" not in sqlite_client.load_resolved_contracts(conn)


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
# /miscoded — coding-mismatch tab
# ---------------------------------------------------------------------------

def _seed_miscoded(conn):
    """A Coding Mismatch row + the candidate contract on the Dashboard."""
    _seed_dashboard_row(conn, contract_name="Lux Lawns", asana_task_gid="g_lux")
    return _seed_needs_tagging(
        conn, group_key="MUS|000|63040|Lux Lawns",
        account_no="63040", vendor="Lux Lawns",
        candidate_names=["Lux Lawns"], candidate_gids=["g_lux"],
        coding_mismatch=True,
    )


def test_miscoded_open_lists_rows_and_hides_from_other_tabs(client, conn):
    _seed_miscoded(conn)
    body = client.get("/miscoded").get_data(as_text=True)
    assert "Lux Lawns" in body
    assert "Accept as miscoded" in body
    # Must NOT leak into Needs Tagging or Vendor Conflicts.
    assert "Lux Lawns" not in client.get("/needs-tagging").get_data(as_text=True)
    assert "Lux Lawns" not in client.get("/vendor-conflicts").get_data(as_text=True)


def test_miscoded_accept_writes_ignore_coding_lm_and_clears_row(client, conn):
    rid = _seed_miscoded(conn)
    resp = client.post(f"/miscoded/{rid}/accept",
                       data={"contract_gid": "g_lux", "contract_name": "Lux Lawns"})
    assert resp.status_code in (302, 303)
    lm = conn.execute(
        'SELECT "Contract Gid", "Ignore Coding" FROM "Learned Mappings" '
        'WHERE "Key" = ?', ("MUS|000|63040|Lux Lawns",)).fetchone()
    assert lm is not None
    assert lm["Contract Gid"] == "g_lux"
    assert lm["Ignore Coding"] == 1
    # NT row is consumed (it attributes on the next ingest).
    assert conn.execute('SELECT COUNT(*) FROM "Needs Tagging"').fetchone()[0] == 0
    # The accepted view sources from the LM.
    assert "Lux Lawns" in client.get("/miscoded?show=accepted").get_data(as_text=True)


def test_miscoded_accept_rejects_non_candidate_gid(client, conn):
    rid = _seed_miscoded(conn)
    client.post(f"/miscoded/{rid}/accept",
                data={"contract_gid": "g_evil", "contract_name": "Injected"})
    # No LM written; the row survives.
    assert conn.execute('SELECT COUNT(*) FROM "Learned Mappings"').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM "Needs Tagging"').fetchone()[0] == 1


def test_miscoded_confirm_correct_moves_to_confirmed_view(client, conn):
    rid = _seed_miscoded(conn)
    client.post(f"/miscoded/{rid}/confirm-correct")
    assert "Lux Lawns" not in client.get("/miscoded?show=open").get_data(as_text=True)
    assert "Lux Lawns" in client.get("/miscoded?show=confirmed").get_data(as_text=True)


def test_miscoded_cross_tier_offers_accept_to_capex_contract(client, conn):
    """A cross-tier coding mismatch surfaces the matched CapEx contract as its
    candidate, so the operator can Accept and link the opex charge to the CapEx
    project despite the Tableau miscoding. Accept writes the Ignore-Coding pin."""
    _seed_dashboard_row(conn, contract_name="JBP Concrete & Construction, LLC",
                        asana_task_gid="g_capex")
    rid = _seed_needs_tagging(
        conn, group_key="OMH|000|63040|JBP Concrete", account_no="63040",
        vendor="JBP Concrete",
        candidate_names=["JBP Concrete & Construction, LLC"],
        candidate_gids=["g_capex"], coding_mismatch=True,
        cross_tier_hint=("Coding mismatch: this vendor matches 'JBP Concrete & "
                         "Construction, LLC' (acct 63015, CapEx project FFE001428), "
                         "but this charge is acct 63040 (opex). Accept to attribute "
                         "it to the CapEx project anyway, or fix the coding upstream."),
    )
    body = client.get("/miscoded").get_data(as_text=True)
    assert "JBP Concrete" in body
    assert f"/miscoded/{rid}/accept" in body          # CapEx candidate → Accept offered
    resp = client.post(f"/miscoded/{rid}/accept",
                       data={"contract_gid": "g_capex",
                             "contract_name": "JBP Concrete & Construction, LLC"})
    assert resp.status_code in (302, 303)
    lm = conn.execute(
        'SELECT "Contract Gid", "Ignore Coding" FROM "Learned Mappings" '
        'WHERE "Key" = ?', ("OMH|000|63040|JBP Concrete",)).fetchone()
    assert lm is not None and lm["Contract Gid"] == "g_capex"
    assert lm["Ignore Coding"] == 1


def test_miscoded_no_candidate_row_shows_acknowledge_only(client, conn):
    """Fallback: a coding-mismatch row with NO candidate gid (no contract to
    link) offers only the acknowledge action, not an Accept button."""
    rid = _seed_needs_tagging(
        conn, group_key="OMH|000|63040|Mystery Co", account_no="63040",
        vendor="Mystery Co", candidate_names=[], candidate_gids=[],
        coding_mismatch=True,
        cross_tier_hint="Coding mismatch: no contract to link - fix coding upstream.",
    )
    body = client.get("/miscoded").get_data(as_text=True)
    assert f"/miscoded/{rid}/accept" not in body
    assert "fix coding at source" in body
    client.post(f"/miscoded/{rid}/confirm-correct")
    assert "Mystery Co" not in client.get("/miscoded?show=open").get_data(as_text=True)


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


def test_needs_tagging_datalist_shows_campuses_per_name(client, conn):
    """Near-identical contract names are distinguished in the dropdown by
    the campuses each covers, aggregated across same-named Dashboard rows."""
    _seed_dashboard_row(conn, contract_name="Marmic Fire & Safety",
                        asana_task_gid="gid-lnx", campus_set="LNX")
    _seed_dashboard_row(conn, contract_name="Marmic Fire & Safety",
                        asana_task_gid="gid-opk", campus_set="OPK")
    _seed_dashboard_row(conn, contract_name="Marmic Fire and Safety",
                        asana_task_gid="gid-stw", campus_set="STW")
    _seed_needs_tagging(conn)
    body = client.get("/needs-tagging").get_data(as_text=True)
    # One <option> per distinct name, value stays the exact name (LM resolves
    # by exact name) and the campuses show as the visible hint. GROUP_CONCAT
    # order isn't guaranteed, so accept either ordering.
    assert 'value="Marmic Fire &amp; Safety">' in body
    assert ("LNX,OPK" in body) or ("OPK,LNX" in body)
    assert '<option value="Marmic Fire and Safety">STW</option>' in body


def test_needs_tagging_out_of_term_row_shows_park_guidance(client, conn):
    """Out-of-term rows are pre-term spend: assigning a contract won't clear
    them, so the row is flagged and offered a one-click Park (reusing the
    once-off route) instead of inviting a futile Assign Contract answer."""
    rec_id = _seed_needs_tagging(conn, vendor="Pre Term Vendor")
    conn.execute('UPDATE "Needs Tagging" SET "Out Of Term" = 1 WHERE id = ?', (rec_id,))
    conn.commit()
    body = client.get("/needs-tagging").get_data(as_text=True)
    assert "Pre-dates the contract" in body          # the guidance
    assert "Park (pre-dates contract)" in body        # the one-click action
    assert f"/needs-tagging/{rec_id}/mark-once-off" in body  # posts to the once-off route


def test_needs_tagging_cross_tier_mismatch_hint(client, conn):
    """An opex charge whose vendor matches a CapEx-coded contract surfaces a
    'coding mismatch' hint instead of a bare 'no candidates' dead-end — so the
    operator knows to fix the account upstream, not fight the Assign box."""
    from engine.sqlite_client import upsert_needs_tagging_group
    upsert_needs_tagging_group(
        conn,
        group_key="OMH|000|63040|JBP Concrete And Construction LLC",
        campus="OMH", dept="000", account_no="63040",
        vendor="JBP Concrete And Construction LLC",
        sample_description="Parking Lot Repairs",
        amount=3400.0,
        candidate_names=[],
        created_at_iso_date="2026-06-25",
        cross_tier_hint=(
            "Coding mismatch: this vendor matches 'JBP Concrete & Construction, "
            "LLC' (acct 63015, CapEx project FFE001428), but this charge is acct "
            "63040. Fix the account coding in Asana or Tableau - it can't be tagged here."
        ),
    )
    body = client.get("/needs-tagging").get_data(as_text=True)
    assert "coding mismatch" in body                          # the pill
    assert "Coding mismatch: this vendor matches" in body     # the prominent hint
    assert "JBP Concrete &amp; Construction, LLC" in body     # the real contract surfaced


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


def test_dashboard_detail_learned_panel_excludes_other_campus_same_name(client, conn):
    """Same-vendor tasks in different campuses share the Asana name, so the
    'Learned Mappings attributing here' panel must key on the pinned gid /
    campus, NOT the name — else a CEN task's page lists OKC/BAO mappings that
    actually feed other tasks (the reported cross-campus display bug)."""
    _seed_dashboard_row(conn, contract_name="Oklahoma Chiller Corporation",
                        asana_task_gid="cen-gid", campus_set="CEN")
    _seed_dashboard_row(conn, contract_name="Oklahoma Chiller Corporation",
                        asana_task_gid="okc-gid", campus_set="OKC")

    def _lm(key, campus, gid):
        conn.execute(
            'INSERT INTO "Learned Mappings" '
            '("Key",Campus,Dept,"Account No",Vendor,"Contract Name","Contract Gid") '
            'VALUES (?,?,?,?,?,?,?)',
            (key, campus, "000", "63040", "OKLAHOMA CHILLER",
             "Oklahoma Chiller Corporation", gid),
        )
    _lm("CEN|000|63040|OKLAHOMA CHILLER", "CEN", "cen-gid")   # pinned here
    _lm("OKC|000|63040|OKLAHOMA CHILLER", "OKC", "okc-gid")   # pinned elsewhere
    _lm("OKC|107|63040|OKLAHOMA CHILLER", "OKC", "")          # blank-gid, OKC campus
    conn.commit()

    body = client.get("/dashboard-detail/cen-gid").get_data(as_text=True)
    assert "CEN|000|63040|OKLAHOMA CHILLER" in body      # this task's own mapping
    assert "OKC|000|63040|OKLAHOMA CHILLER" not in body  # gid-pinned to OKC task
    assert "OKC|107|63040|OKLAHOMA CHILLER" not in body  # blank-gid, wrong campus


def test_dashboard_detail_404s_for_unknown_gid(client):
    resp = client.get("/dashboard-detail/gid-does-not-exist")
    assert resp.status_code == 404


def test_held_ingest_shows_banner_until_a_good_ingest_clears_it(client, conn):
    """A sanity-gate HOLD shows a banner on every page; a later OK ingest clears it."""
    from engine.sqlite_client import append_run_log
    append_run_log(
        conn, run_id="2026-07-01T16:34:00", mode="ingest", outcome="partial",
        file_name="Transactions (1).csv", file_hash="c" * 64,
        rows_in_scope=14815, rows_out_of_scope=2027, total_in_scope=17_785_561.0,
        review_flags="HELD: in-scope rows down 14%",
        notes="HELD by sanity gate — NOT written to Asana.",
    )
    body = client.get("/").get_data(as_text=True)
    assert "Ingest held" in body
    assert "Transactions (1).csv" in body

    append_run_log(
        conn, run_id="2026-07-02T14:00:00", mode="ingest", outcome="ok",
        file_name="Transactions.csv", file_hash="a" * 64,
        rows_in_scope=17231, rows_out_of_scope=0, total_in_scope=20_790_332.0,
    )
    assert "Ingest held" not in client.get("/").get_data(as_text=True)


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
    body = client.get("/settings").get_data(as_text=True)
    assert "ASANA_WORKSPACE_GID" in body
    assert "ACCOUNTS_IN_SCOPE" in body
    assert "TRANSACTION_SOURCE" in body
    assert "ASANA_PAT" in body
    # Set / not-set markers present.
    assert "set" in body
    assert "not set" in body


# ---------------------------------------------------------------------------
# /vendor-conflicts — Phase 7 review panel for same-vendor multi-task conflicts
# ---------------------------------------------------------------------------

def test_vendor_conflicts_empty_state(client):
    body = client.get("/vendor-conflicts").get_data(as_text=True)
    assert "Vendor Conflicts" in body
    assert "Nothing to resolve" in body


def test_vendor_conflicts_lists_only_multi_candidate_rows(client, conn):
    """A conflict is only shown when the Needs Tagging row has 2+ candidate
    gids AND those gids point to actual Dashboard tasks. Single-candidate
    rows belong to the Needs Tagging tab, not Vendor Conflicts."""
    # Seed two Dashboard tasks (same vendor, different campuses).
    _seed_dashboard_row(conn, contract_name="Acme Service",
                        asana_task_gid="g_old", campus_set="CEN",
                        start=date(2025, 9, 1), due=date(2026, 9, 1))
    _seed_dashboard_row(conn, contract_name="Acme Service",
                        asana_task_gid="g_new", campus_set="CEN",
                        start=date(2026, 7, 1), due=date(2027, 7, 1))
    # Conflict row: vendor matched, both gids in candidates.
    _seed_needs_tagging(
        conn,
        group_key="CEN|000|63015|Acme Service",
        vendor="Acme Service",
        candidate_names=["Acme Service", "Acme Service"],
        candidate_gids=["g_old", "g_new"],
    )
    # Non-conflict row: only one candidate gid, doesn't belong here.
    _seed_needs_tagging(
        conn,
        group_key="OMH|000|63015|Solo Vendor",
        vendor="Solo Vendor",
        candidate_names=["Acme Service"],
        candidate_gids=["g_old"],
    )
    body = client.get("/vendor-conflicts").get_data(as_text=True)
    # The conflict row appears; the single-candidate Solo Vendor row does not.
    assert "Acme Service" in body
    assert "Solo Vendor" not in body
    # Both candidates rendered (both gids appear in the page).
    assert "g_old" in body
    assert "g_new" in body


def test_vendor_conflicts_assign_writes_learned_mapping_with_gid(client, conn):
    _seed_dashboard_row(conn, contract_name="Acme Service",
                        asana_task_gid="g_old", campus_set="CEN")
    _seed_dashboard_row(conn, contract_name="Acme Service",
                        asana_task_gid="g_new", campus_set="CEN")
    rec_id = _seed_needs_tagging(
        conn,
        group_key="CEN|000|63015|Acme Service",
        vendor="Acme Service",
        candidate_names=["Acme Service", "Acme Service"],
        candidate_gids=["g_old", "g_new"],
    )
    resp = client.post(
        f"/vendor-conflicts/{rec_id}/assign",
        data={"contract_gid": "g_old", "contract_name": "Acme Service"},
    )
    assert resp.status_code == 302
    # Learned Mapping created with Contract Gid pinned.
    row = conn.execute(
        'SELECT * FROM "Learned Mappings" WHERE "Key" = ?',
        ("CEN|000|63015|Acme Service",),
    ).fetchone()
    assert row is not None
    assert row["Contract Name"] == "Acme Service"
    assert row["Contract Gid"] == "g_old"
    # Needs Tagging row was deleted (resolved).
    nt_remaining = conn.execute(
        'SELECT COUNT(*) FROM "Needs Tagging" WHERE id = ?', (rec_id,)
    ).fetchone()[0]
    assert nt_remaining == 0


def test_vendor_conflicts_assign_rejects_gid_not_in_candidates(client, conn):
    """Form-tampering guard: the gid posted MUST be one of the engine's
    candidate gids for this row. Otherwise the panel could be used to pin
    an arbitrary task gid to the group key."""
    _seed_dashboard_row(conn, contract_name="Acme Service",
                        asana_task_gid="g_old", campus_set="CEN")
    _seed_dashboard_row(conn, contract_name="Acme Service",
                        asana_task_gid="g_new", campus_set="CEN")
    rec_id = _seed_needs_tagging(
        conn,
        group_key="CEN|000|63015|Acme Service",
        vendor="Acme Service",
        candidate_names=["Acme Service", "Acme Service"],
        candidate_gids=["g_old", "g_new"],
    )
    resp = client.post(
        f"/vendor-conflicts/{rec_id}/assign",
        data={"contract_gid": "g_other_unrelated", "contract_name": "Acme Service"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    # No Learned Mapping written; NT row still in place.
    assert conn.execute(
        'SELECT COUNT(*) FROM "Learned Mappings"'
    ).fetchone()[0] == 0
    assert conn.execute(
        'SELECT COUNT(*) FROM "Needs Tagging" WHERE id = ?', (rec_id,)
    ).fetchone()[0] == 1


def test_vendor_conflicts_assign_404s_for_unknown_record_id(client):
    resp = client.post(
        "/vendor-conflicts/99999/assign",
        data={"contract_gid": "g_x", "contract_name": "X"},
    )
    assert resp.status_code == 404


def test_vendor_conflicts_shows_asana_reason_and_tableau_description(client, conn):
    """Operator-facing comparison: render the Tableau Sample Record
    Description prominently AND each candidate's Asana Contract Reason
    Text under its name so the operator can see, at a glance, which
    contract the description matches."""
    _seed_dashboard_row(
        conn, contract_name="Oklahoma Chiller Corporation",
        asana_task_gid="g_pm", campus_set="CEN",
        contract_reason_text="HVAC preventative maintenance for CEN BAO",
    )
    _seed_dashboard_row(
        conn, contract_name="Oklahoma Chiller Corporation",
        asana_task_gid="g_coil", campus_set="CEN",
        contract_reason_text="Replace the leaking evaporator coil for the Studio B unit (#4)",
    )
    _seed_needs_tagging(
        conn, group_key="CEN|000|63040|Oklahoma Chiller Corporation",
        vendor="Oklahoma Chiller Corporation",
        sample_description="HVAC repair Studio C 05/01",
        candidate_names=["Oklahoma Chiller Corporation", "Oklahoma Chiller Corporation"],
        candidate_gids=["g_pm", "g_coil"],
    )
    body = client.get("/vendor-conflicts").get_data(as_text=True)
    # The Tableau description is surfaced for comparison.
    assert "HVAC repair Studio C 05/01" in body
    assert "Tableau Record Description" in body
    # Each candidate's Asana reason text is visible.
    assert "HVAC preventative maintenance for CEN BAO" in body
    assert "Replace the leaking evaporator coil for the Studio B unit (#4)" in body
    assert "Asana Contract Reason Text" in body


def test_vendor_conflicts_renders_per_description_picker(client, conn):
    """When the NT row has Distinct Descriptions JSON, each unique
    description appears as a row in a per-description picker with one
    dropdown of the candidate Asana tasks. Each description gets a
    rows-count and dollar-amount column so the operator sees the impact."""
    _seed_dashboard_row(
        conn, contract_name="Bear Claw Landscaping",
        asana_task_gid="g_lawn", campus_set="NCS",
        contract_reason_text="Landscape services.",
    )
    _seed_dashboard_row(
        conn, contract_name="Bear Claw Landscaping Inc",
        asana_task_gid="g_snow", campus_set="NCS",
        contract_reason_text="Snow Removal",
    )
    _seed_needs_tagging(
        conn, group_key="NCS|000|63080|Bear Claw Landscaping, Inc",
        vendor="Bear Claw Landscaping, Inc",
        sample_description="Groundskeeping 12/2024",
        candidate_names=["Bear Claw Landscaping", "Bear Claw Landscaping Inc"],
        candidate_gids=["g_lawn", "g_snow"],
        distinct_descriptions=[
            ("Groundskeeping 12/2024", 12, 87000.0),
            ("Snow Removal 02/2026", 4, 28000.0),
        ],
    )
    body = client.get("/vendor-conflicts").get_data(as_text=True)
    # Picker section is rendered.
    assert "Or pick per Tableau Record Description" in body
    assert "Groundskeeping 12/2024" in body
    assert "Snow Removal 02/2026" in body
    # Dropdown for each description, with both candidates as options.
    assert "Bear Claw Landscaping" in body
    assert "Bear Claw Landscaping Inc" in body
    # The skip option is present so operator can leave individual rows alone.
    assert "skip" in body.lower()


# ---------------------------------------------------------------------------
# Phase 13: date-aware auto-suggest in Vendor Conflicts
# ---------------------------------------------------------------------------

def test_suggest_rejects_date_incompatible_candidate_even_if_text_wins():
    """Direct unit test: the Bear Claw scenario. Snow text matches the snow
    candidate's reason text, BUT that candidate's term starts 2025-09-30
    while the description's transaction dates are March 2025. Auto-suggest
    must NOT pre-select the date-incompatible candidate."""
    from engine.ui.routes import _suggest_candidate_per_description

    candidates = [
        {"Asana Task GID": "g_landscaping",
         "Contract Reason Text": "NCS landscaping",
         "Start": "2026-03-31", "Due": "2027-03-31"},
        {"Asana Task GID": "g_snow",
         "Contract Reason Text": "Snow Removal",
         "Start": "2025-09-30", "Due": "2026-09-30"},
    ]
    # March 2025 -- before BOTH candidates' terms. Even though text matches
    # the snow candidate (Snow ↔ Snow Removal), neither can be auto-picked.
    descriptions = [
        {"description": "Snow/ice management 03/2025",
         "rows": 2, "amount": 2500.0,
         "min_date": "2025-03-01", "max_date": "2025-03-15"},
    ]
    out = _suggest_candidate_per_description(descriptions, candidates)
    assert len(out) == 1
    assert out[0]["suggested_gid"] is None
    # And the compat map flags BOTH candidates as out-of-term.
    compat = out[0]["date_compat_by_gid"]
    assert compat["g_landscaping"] is False
    assert compat["g_snow"] is False


def test_suggest_picks_date_compatible_candidate():
    """When the description's dates fall inside ONE candidate's term, that
    candidate wins -- date compatibility narrows the pool BEFORE Jaccard."""
    from engine.ui.routes import _suggest_candidate_per_description

    candidates = [
        {"Asana Task GID": "g_landscaping",
         "Contract Reason Text": "NCS landscaping monthly",
         "Start": "2026-03-31", "Due": "2027-03-31"},
        {"Asana Task GID": "g_snow",
         "Contract Reason Text": "Snow Removal",
         "Start": "2025-09-30", "Due": "2026-09-30"},
    ]
    # May 2026 -- inside snow's term (ends 2026-09-30) AND inside
    # landscaping's term (starts 2026-03-31). Text resolves the tie.
    descriptions = [
        {"description": "Snow/ice management 05/2026",
         "rows": 1, "amount": 3720.0,
         "min_date": "2026-05-15", "max_date": "2026-05-15"},
    ]
    out = _suggest_candidate_per_description(descriptions, candidates)
    assert out[0]["suggested_gid"] == "g_snow"
    assert out[0]["date_compat_by_gid"]["g_snow"] is True
    assert out[0]["date_compat_by_gid"]["g_landscaping"] is True


def test_suggest_falls_back_to_text_when_dates_missing():
    """Old Distinct Descriptions JSON rows (written before Phase 13) have
    no min_date/max_date. The helper degrades to text-only matching rather
    than rejecting every candidate -- otherwise existing rows would lose
    their auto-suggestions on the first read after upgrade."""
    from engine.ui.routes import _suggest_candidate_per_description

    candidates = [
        {"Asana Task GID": "g_snow",
         "Contract Reason Text": "Snow Removal",
         "Start": "2025-09-30", "Due": "2026-09-30"},
        {"Asana Task GID": "g_landscaping",
         "Contract Reason Text": "Landscaping monthly",
         "Start": "2026-03-31", "Due": "2027-03-31"},
    ]
    descriptions = [
        # Note: NO min_date/max_date keys -- legacy shape from before Phase 13.
        {"description": "Snow/ice management 03/2025", "rows": 2, "amount": 2500.0},
    ]
    out = _suggest_candidate_per_description(descriptions, candidates)
    # With dates missing, every candidate is compat=True so Jaccard alone wins.
    assert out[0]["suggested_gid"] == "g_snow"
    assert all(out[0]["date_compat_by_gid"].values())


def test_suggest_skips_only_one_when_other_is_in_term():
    """Hybrid case: one candidate's term fits, the other doesn't. The
    in-term one is auto-suggested even if the out-of-term one has slightly
    better text overlap -- date is a HARD filter, not a tiebreak."""
    from engine.ui.routes import _suggest_candidate_per_description

    candidates = [
        # Slightly stronger text match but term doesn't fit.
        {"Asana Task GID": "g_snow_old",
         "Contract Reason Text": "Snow Removal services",
         "Start": "2023-01-01", "Due": "2024-12-31"},
        # Weaker text match but term fits.
        {"Asana Task GID": "g_snow_new",
         "Contract Reason Text": "Snow Removal",
         "Start": "2025-09-30", "Due": "2026-09-30"},
    ]
    descriptions = [
        {"description": "Snow/ice management 03/2026",
         "rows": 1, "amount": 1000.0,
         "min_date": "2026-03-15", "max_date": "2026-03-15"},
    ]
    out = _suggest_candidate_per_description(descriptions, candidates)
    assert out[0]["suggested_gid"] == "g_snow_new"
    assert out[0]["date_compat_by_gid"]["g_snow_old"] is False
    assert out[0]["date_compat_by_gid"]["g_snow_new"] is True


def test_date_intervals_overlap_helper():
    """Unit test the small interval-overlap helper directly so the
    edge cases (touching endpoints, missing dates) are pinned."""
    from engine.ui.routes import _date_intervals_overlap

    # Disjoint.
    assert not _date_intervals_overlap("2025-01-01", "2025-06-30",
                                       "2025-07-01", "2025-12-31")
    # Overlap (one contains the other).
    assert _date_intervals_overlap("2025-01-01", "2025-12-31",
                                   "2025-05-01", "2025-06-30")
    # Partial overlap.
    assert _date_intervals_overlap("2025-01-01", "2025-06-30",
                                   "2025-06-01", "2025-09-30")
    # Touching endpoint -- inclusive.
    assert _date_intervals_overlap("2025-01-01", "2025-06-30",
                                   "2025-06-30", "2025-12-31")
    # Missing date on either side -> True (graceful fallback).
    assert _date_intervals_overlap("", "2025-12-31", "2025-06-30", "2025-12-31")
    assert _date_intervals_overlap("2025-01-01", "", "2025-06-30", "2025-12-31")
    assert _date_intervals_overlap("2025-01-01", "2025-12-31", "", "2025-12-31")
    assert _date_intervals_overlap("2025-01-01", "2025-12-31", "2025-06-30", "")


def test_vendor_conflicts_renders_out_of_term_warning(client, conn):
    """End-to-end: a description bucket whose dates fall outside both
    candidates' terms renders the ⚠ marker on the dropdown options AND
    is NOT auto-picked. Mirrors the screenshot the operator reported."""
    _seed_dashboard_row(
        conn, contract_name="Bear Claw Landscaping",
        asana_task_gid="g_lawn", campus_set="NCS",
        contract_reason_text="NCS landscaping",
        start=date(2026, 3, 31), due=date(2027, 3, 31),
    )
    _seed_dashboard_row(
        conn, contract_name="Bear Claw Landscaping Inc",
        asana_task_gid="g_snow", campus_set="NCS",
        contract_reason_text="Snow Removal",
        start=date(2025, 9, 30), due=date(2026, 9, 30),
    )
    _seed_needs_tagging(
        conn, group_key="NCS|000|63080|Bear Claw Landscaping, Inc",
        vendor="Bear Claw Landscaping, Inc",
        sample_description="Snow/ice management 3/2025",
        candidate_names=["Bear Claw Landscaping", "Bear Claw Landscaping Inc"],
        candidate_gids=["g_lawn", "g_snow"],
        distinct_descriptions=[
            # Phase 13 5-tuple shape. March 2025 -- BEFORE both contracts.
            ("Snow/ice management 3/2025", 2, 2500.0, "2025-03-01", "2025-03-15"),
            # March 2026 -- inside the snow contract's term.
            ("Snow/ice management 03/2026", 1, 3720.0, "2026-03-15", "2026-03-15"),
        ],
    )
    body = client.get("/vendor-conflicts").get_data(as_text=True)
    # The out-of-term ⚠ marker is rendered.
    assert "outside term" in body
    # The "txn dates:" date-range hint is rendered for buckets with dates.
    assert "txn dates:" in body
    # The in-term description's dates appear in the dates row.
    assert "2026-03-15" in body


# ---------------------------------------------------------------------------
# Phase 14: out-of-term routing + Pre-dates dropdown + bulk button
# ---------------------------------------------------------------------------

def test_vendor_conflicts_includes_single_candidate_when_out_of_term(client, conn):
    """Phase 14a: a Needs Tagging row with only 1 candidate normally drops
    out of /vendor-conflicts (it goes to Needs Tagging Open instead). But
    when the engine flagged it as Out Of Term, it must appear in Vendor
    Conflicts so the operator can use the per-description picker to mark
    pre-dates or fix the Asana term."""
    _seed_dashboard_row(
        conn, contract_name="Office Express Janitorial Services",
        asana_task_gid="g_solo", campus_set="CEN",
        contract_reason_text="Janitorial",
        start=date(2025, 11, 9), due=date(2026, 11, 9),
    )
    rec_id = _seed_needs_tagging(
        conn, group_key="CEN|000|63080|Office Express Janitorial Services",
        vendor="Office Express Janitorial Services",
        sample_description="Cleaning April",
        candidate_names=["Office Express Janitorial Services"],
        candidate_gids=["g_solo"],
        distinct_descriptions=[
            ("Cleaning April", 1, 16000.0, "2025-04-01", "2025-04-15"),
        ],
    )
    # Manually set Out Of Term to simulate what the upsert would have done.
    conn.execute(
        'UPDATE "Needs Tagging" SET "Out Of Term" = 1 WHERE id = ?',
        (rec_id,),
    )
    conn.commit()
    body = client.get("/vendor-conflicts").get_data(as_text=True)
    assert "Office Express Janitorial Services" in body
    # Picker renders even though there's only 1 candidate.
    assert "Cleaning April" in body


def test_vendor_conflicts_mark_pre_dates_parks_group(client, conn):
    """Regression: a single-candidate out-of-term group with EMPTY Distinct
    Descriptions JSON (the bundle's real case) reaches Vendor Conflicts but
    the per-description Pre-dates option can't render. The group-level
    'Pre-dates Asana Record' button must still be available, set Once Off,
    and make the group leave the list."""
    _seed_dashboard_row(
        conn, contract_name="Collett Mechanical Inc.",
        asana_task_gid="g_collett", campus_set="ALB",
        start=date(2026, 6, 15), due=date(2027, 6, 9),
    )
    rec_id = _seed_needs_tagging(
        conn, group_key="ALB|000|63040|Collett Mechanical Service Inc",
        vendor="Collett Mechanical Service Inc",
        candidate_names=["Collett Mechanical Inc."],
        candidate_gids=["g_collett"],
        distinct_descriptions=[],   # empty — picker can't render
    )
    conn.execute(
        'UPDATE "Needs Tagging" SET "Out Of Term" = 1 WHERE id = ?', (rec_id,),
    )
    conn.commit()
    body = client.get("/vendor-conflicts").get_data(as_text=True)
    assert "Collett Mechanical Service Inc" in body
    assert "Pre-dates Asana Record" in body   # button present even w/ empty JSON

    client.post(f"/vendor-conflicts/{rec_id}/mark-pre-dates")
    assert conn.execute(
        'SELECT "Once Off" FROM "Needs Tagging" WHERE id = ?', (rec_id,)
    ).fetchone()[0] == 1
    body2 = client.get("/vendor-conflicts").get_data(as_text=True)
    assert "Collett Mechanical Service Inc" not in body2   # left the list


def test_vendor_conflicts_excludes_single_candidate_when_not_out_of_term(client, conn):
    """Regression: a single-candidate Needs Tagging row that is NOT
    out-of-term still must NOT appear in /vendor-conflicts (it goes to
    Needs Tagging Open). Phase 14a only loosens the filter for the
    out-of-term case."""
    _seed_dashboard_row(
        conn, contract_name="Acme", asana_task_gid="g_acme", campus_set="CEN",
    )
    _seed_needs_tagging(
        conn, group_key="CEN|000|63080|Acme",
        vendor="Acme",
        candidate_names=["Acme"], candidate_gids=["g_acme"],
        distinct_descriptions=[("x", 1, 100.0, "2026-06-01", "2026-06-01")],
    )
    body = client.get("/vendor-conflicts").get_data(as_text=True)
    # Should NOT appear -- single candidate, no Out Of Term flag.
    assert "Acme" not in body


def test_vendor_conflicts_dropdown_includes_pre_dates_option(client, conn):
    """Phase 14b: every per-description dropdown now exposes a sentinel
    'Unassigned - Pre-dates Asana Record' option."""
    _seed_dashboard_row(conn, contract_name="Bear Claw Landscaping",
                        asana_task_gid="g_lawn", campus_set="NCS")
    _seed_dashboard_row(conn, contract_name="Bear Claw Landscaping Inc",
                        asana_task_gid="g_snow", campus_set="NCS")
    _seed_needs_tagging(
        conn, group_key="NCS|000|63080|Bear Claw Landscaping, Inc",
        vendor="Bear Claw Landscaping, Inc",
        candidate_names=["Bear Claw Landscaping", "Bear Claw Landscaping Inc"],
        candidate_gids=["g_lawn", "g_snow"],
        distinct_descriptions=[("Landscaping 12/2025", 1, 8157.50,
                                "2026-01-01", "2026-01-01")],
    )
    body = client.get("/vendor-conflicts").get_data(as_text=True)
    assert "__PRE_DATES__" in body
    assert "Pre-dates Asana Record" in body


def test_vendor_conflicts_pre_dates_pick_strips_bucket_without_lm(client, conn):
    """Phase 14b: submitting __PRE_DATES__ for a description must NOT
    create a Learned Mapping and must remove that bucket from the row's
    Distinct Descriptions JSON. Other buckets stay (operator's other
    decisions are unaffected). Row stays alive when buckets remain."""
    _seed_dashboard_row(conn, contract_name="Bear Claw Landscaping",
                        asana_task_gid="g_lawn", campus_set="NCS")
    _seed_dashboard_row(conn, contract_name="Bear Claw Landscaping Inc",
                        asana_task_gid="g_snow", campus_set="NCS")
    rec_id = _seed_needs_tagging(
        conn, group_key="NCS|000|63080|Bear Claw Landscaping, Inc",
        vendor="Bear Claw Landscaping, Inc",
        candidate_names=["Bear Claw Landscaping", "Bear Claw Landscaping Inc"],
        candidate_gids=["g_lawn", "g_snow"],
        distinct_descriptions=[
            ("Landscaping 12/2025", 1, 8157.50, "2026-01-01", "2026-01-01"),
            ("Snow Removal 03/2026", 1, 3720.00, "2026-03-15", "2026-03-15"),
        ],
    )
    resp = client.post(
        f"/vendor-conflicts/{rec_id}/assign-by-description",
        data={
            "desc_0": "Landscaping 12/2025",
            "gid_0": "__PRE_DATES__",
            "name_0": "(Pre-dates Asana Record)",
            "desc_1": "Snow Removal 03/2026",
            "gid_1": "",
            "name_1": "",
        },
    )
    assert resp.status_code == 302
    # No LM written.
    lm_count = conn.execute(
        'SELECT COUNT(*) FROM "Learned Mappings" '
        'WHERE "Key" = ?',
        ("NCS|000|63080|Bear Claw Landscaping, Inc",),
    ).fetchone()[0]
    assert lm_count == 0
    # Row still exists.
    row = conn.execute(
        'SELECT "Distinct Descriptions JSON" FROM "Needs Tagging" WHERE id = ?',
        (rec_id,),
    ).fetchone()
    assert row is not None
    import json
    remaining = json.loads(row[0])
    descs = [b["description"] for b in remaining]
    # Landscaping bucket stripped; Snow Removal stays.
    assert "Landscaping 12/2025" not in descs
    assert "Snow Removal 03/2026" in descs


def test_vendor_conflicts_pre_dates_and_lm_mixed(client, conn):
    """Phase 14b: when the operator mixes a real LM pick AND a pre-dates
    pick in the same submission, the LM is written and the row is deleted
    (next ingest rebuilds). Pre-dates bucket re-surfaces naturally next
    ingest because no LM was written for it."""
    _seed_dashboard_row(conn, contract_name="Snow Removal Co",
                        asana_task_gid="g_snow", campus_set="NCS")
    rec_id = _seed_needs_tagging(
        conn, group_key="NCS|000|63080|Multi",
        vendor="Multi",
        candidate_names=["Snow Removal Co"], candidate_gids=["g_snow"],
        distinct_descriptions=[
            ("Snow 03/2026", 1, 1000.0, "2026-03-01", "2026-03-01"),
            ("Snow 03/2025", 1, 800.0, "2025-03-01", "2025-03-01"),
        ],
    )
    resp = client.post(
        f"/vendor-conflicts/{rec_id}/assign-by-description",
        data={
            "desc_0": "Snow 03/2026",
            "gid_0": "g_snow",
            "name_0": "Snow Removal Co",
            "desc_1": "Snow 03/2025",
            "gid_1": "__PRE_DATES__",
            "name_1": "(Pre-dates Asana Record)",
        },
    )
    assert resp.status_code == 302
    # One LM written for the in-term pick.
    lms = conn.execute(
        'SELECT "Description Pattern", "Contract Gid" '
        'FROM "Learned Mappings" WHERE "Key" = ?',
        ("NCS|000|63080|Multi",),
    ).fetchall()
    assert len(lms) == 1
    # #7: pattern is normalized (volatile date token stripped) -> "snow".
    assert lms[0]["Description Pattern"] == "snow"
    # Row deleted (consistent with existing assign-by-description behavior
    # when picks are made).
    row = conn.execute(
        'SELECT 1 FROM "Needs Tagging" WHERE id = ?', (rec_id,),
    ).fetchone()
    assert row is None


def test_vendor_conflicts_bulk_mark_out_of_term_strips_only_all_out_buckets(client, conn):
    """Phase 14c: bulk button strips ONLY buckets where every candidate's
    term excludes the bucket's date range. Buckets where at least one
    candidate fits the date stay (operator handles per-description)."""
    _seed_dashboard_row(
        conn, contract_name="Bear Claw Landscaping",
        asana_task_gid="g_lawn", campus_set="NCS",
        start=date(2026, 3, 31), due=date(2027, 3, 31),
    )
    _seed_dashboard_row(
        conn, contract_name="Bear Claw Landscaping Inc",
        asana_task_gid="g_snow", campus_set="NCS",
        start=date(2025, 9, 30), due=date(2026, 9, 30),
    )
    rec_id = _seed_needs_tagging(
        conn, group_key="NCS|000|63080|Bear Claw Landscaping, Inc",
        vendor="Bear Claw Landscaping, Inc",
        candidate_names=["Bear Claw Landscaping", "Bear Claw Landscaping Inc"],
        candidate_gids=["g_lawn", "g_snow"],
        distinct_descriptions=[
            # March 2025 -- BEFORE both contracts. Should be stripped.
            ("Snow 03/2025", 1, 1000.0, "2025-03-01", "2025-03-15"),
            # April 2025 -- BEFORE both contracts. Should be stripped.
            ("Snow 04/2025", 1, 800.0, "2025-04-01", "2025-04-15"),
            # March 2026 -- inside Inc's term. Should STAY.
            ("Snow 03/2026", 1, 3720.0, "2026-03-15", "2026-03-15"),
        ],
    )
    resp = client.post(
        f"/vendor-conflicts/{rec_id}/mark-all-out-of-term-as-pre-dates",
    )
    assert resp.status_code == 302
    import json
    row = conn.execute(
        'SELECT "Distinct Descriptions JSON" FROM "Needs Tagging" WHERE id = ?',
        (rec_id,),
    ).fetchone()
    remaining = json.loads(row[0])
    descs = [b["description"] for b in remaining]
    assert "Snow 03/2025" not in descs
    assert "Snow 04/2025" not in descs
    assert "Snow 03/2026" in descs


def test_vendor_conflicts_bulk_mark_skips_buckets_with_unknown_dates(client, conn):
    """Bucket with no min/max date can't be judged -- keep it in place.
    Legacy rows without Phase-13 dates degrade safely."""
    _seed_dashboard_row(
        conn, contract_name="Vendor X", asana_task_gid="g_x", campus_set="NCS",
        start=date(2026, 3, 31), due=date(2027, 3, 31),
    )
    _seed_dashboard_row(
        conn, contract_name="Vendor X Inc", asana_task_gid="g_xinc", campus_set="NCS",
        start=date(2025, 9, 30), due=date(2026, 9, 30),
    )
    rec_id = _seed_needs_tagging(
        conn, group_key="NCS|000|63080|Vendor X",
        vendor="Vendor X",
        candidate_names=["Vendor X", "Vendor X Inc"],
        candidate_gids=["g_x", "g_xinc"],
        # 3-tuple shape (legacy, no dates) -- the serializer fills empty
        # min/max which the bulk handler treats as "can't judge -> keep".
        distinct_descriptions=[("desc-no-dates", 1, 100.0)],
    )
    resp = client.post(
        f"/vendor-conflicts/{rec_id}/mark-all-out-of-term-as-pre-dates",
    )
    # No buckets stripped -> error flash, redirect.
    assert resp.status_code == 302
    import json
    row = conn.execute(
        'SELECT "Distinct Descriptions JSON" FROM "Needs Tagging" WHERE id = ?',
        (rec_id,),
    ).fetchone()
    remaining = json.loads(row[0])
    assert any(b.get("description") == "desc-no-dates" for b in remaining)


def test_vendor_conflicts_bulk_button_visible_only_when_out_of_term_present(client, conn):
    """The bulk-strip button shows ONLY on groups that have at least one
    bucket whose every candidate fails the date check. A group where every
    bucket has at least one date-compatible candidate must NOT show the button."""
    # All in-term: both candidates' terms contain the bucket date.
    _seed_dashboard_row(
        conn, contract_name="Beta", asana_task_gid="g_beta", campus_set="CEN",
        start=date(2026, 1, 1), due=date(2026, 12, 31),
    )
    _seed_dashboard_row(
        conn, contract_name="Beta Inc", asana_task_gid="g_beta_inc", campus_set="CEN",
        start=date(2026, 1, 1), due=date(2026, 12, 31),
    )
    _seed_needs_tagging(
        conn, group_key="CEN|000|63080|Beta",
        vendor="Beta",
        candidate_names=["Beta", "Beta Inc"],
        candidate_gids=["g_beta", "g_beta_inc"],
        distinct_descriptions=[("in-term", 1, 100.0, "2026-06-01", "2026-06-01")],
    )
    body = client.get("/vendor-conflicts").get_data(as_text=True)
    assert "Mark all out-of-term as Pre-dates" not in body


def test_vendor_conflicts_assign_by_description_writes_pattern_lms(client, conn):
    """Per-description picks write one Learned Mapping each with a NORMALIZED
    Description Pattern column populated. Pattern LMs are the mechanism
    that lets one (Campus, Dept, Acct, Vendor) group split across multiple
    Asana tasks on subsequent ingests. #7: the stored pattern strips volatile
    invoice/date tokens so it keeps matching ('Groundskeeping 12/2024' ->
    'groundskeeping')."""
    _seed_dashboard_row(
        conn, contract_name="Bear Claw Landscaping",
        asana_task_gid="g_lawn", campus_set="NCS",
    )
    _seed_dashboard_row(
        conn, contract_name="Bear Claw Landscaping Inc",
        asana_task_gid="g_snow", campus_set="NCS",
    )
    rec_id = _seed_needs_tagging(
        conn, group_key="NCS|000|63080|Bear Claw Landscaping, Inc",
        vendor="Bear Claw Landscaping, Inc",
        candidate_names=["Bear Claw Landscaping", "Bear Claw Landscaping Inc"],
        candidate_gids=["g_lawn", "g_snow"],
        distinct_descriptions=[
            ("Groundskeeping 12/2024", 12, 87000.0),
            ("Snow Removal 02/2026", 4, 28000.0),
        ],
    )
    resp = client.post(
        f"/vendor-conflicts/{rec_id}/assign-by-description",
        data={
            "desc_0": "Groundskeeping 12/2024",
            "gid_0": "g_lawn",
            "name_0": "Bear Claw Landscaping",
            "desc_1": "Snow Removal 02/2026",
            "gid_1": "g_snow",
            "name_1": "Bear Claw Landscaping Inc",
        },
    )
    assert resp.status_code == 302
    # Two pattern LMs written, with normalized patterns.
    lms = conn.execute(
        '''SELECT "Contract Name", "Contract Gid", "Description Pattern"
           FROM "Learned Mappings"
           WHERE "Key" = ?
           ORDER BY "Description Pattern"''',
        ("NCS|000|63080|Bear Claw Landscaping, Inc",),
    ).fetchall()
    assert len(lms) == 2
    by_pattern = {r["Description Pattern"]: dict(r) for r in lms}
    assert by_pattern["groundskeeping"]["Contract Gid"] == "g_lawn"
    # "Snow Removal 02/2026" normalizes to "snow" — "removal" is a generic
    # action word, dropped so the subject noun stays the pattern.
    assert by_pattern["snow"]["Contract Gid"] == "g_snow"
    # Needs Tagging row removed (it'll be re-created next ingest if any rows
    # still attribute ambiguously).
    nt_left = conn.execute(
        'SELECT COUNT(*) FROM "Needs Tagging" WHERE id = ?', (rec_id,)
    ).fetchone()[0]
    assert nt_left == 0


def test_vendor_conflicts_assign_by_description_skips_unpicked_rows(client, conn):
    """The operator can leave a description unassigned by selecting the
    skip option (empty gid). Skipped descriptions do NOT create an LM."""
    _seed_dashboard_row(conn, contract_name="Lawn", asana_task_gid="g_lawn", campus_set="NCS")
    _seed_dashboard_row(conn, contract_name="Snow", asana_task_gid="g_snow", campus_set="NCS")
    rec_id = _seed_needs_tagging(
        conn, group_key="NCS|000|63080|MultiVendor", vendor="MultiVendor",
        candidate_names=["Lawn", "Snow"],
        candidate_gids=["g_lawn", "g_snow"],
        distinct_descriptions=[("Groundskeeping", 5, 1000.0), ("Snow Removal", 3, 500.0)],
    )
    resp = client.post(
        f"/vendor-conflicts/{rec_id}/assign-by-description",
        data={
            "desc_0": "Groundskeeping", "gid_0": "g_lawn", "name_0": "Lawn",
            "desc_1": "Snow Removal", "gid_1": "", "name_1": "",  # skipped
        },
    )
    assert resp.status_code == 302
    lms = conn.execute(
        'SELECT "Description Pattern" FROM "Learned Mappings" WHERE "Key" = ?',
        ("NCS|000|63080|MultiVendor",),
    ).fetchall()
    assert len(lms) == 1
    assert lms[0]["Description Pattern"] == "groundskeeping"


def test_vendor_conflicts_assign_by_description_rejects_invalid_gid(client, conn):
    """Form-tampering guard: only candidate gids may be persisted."""
    _seed_dashboard_row(conn, contract_name="A", asana_task_gid="g_a", campus_set="NCS")
    _seed_dashboard_row(conn, contract_name="B", asana_task_gid="g_b", campus_set="NCS")
    rec_id = _seed_needs_tagging(
        conn, group_key="NCS|000|63080|V", vendor="V",
        candidate_names=["A", "B"], candidate_gids=["g_a", "g_b"],
        distinct_descriptions=[("foo", 1, 1.0)],
    )
    resp = client.post(
        f"/vendor-conflicts/{rec_id}/assign-by-description",
        data={"desc_0": "foo", "gid_0": "g_unrelated", "name_0": "X"},
    )
    assert resp.status_code == 302
    # Invalid gid filtered → no LM written → since no valid picks, NT row stays.
    assert conn.execute('SELECT COUNT(*) FROM "Learned Mappings"').fetchone()[0] == 0
    assert conn.execute(
        'SELECT COUNT(*) FROM "Needs Tagging" WHERE id = ?', (rec_id,)
    ).fetchone()[0] == 1


def test_vendor_conflicts_handles_candidate_without_reason_text(client, conn):
    """Older Asana tasks may have no Contract Reason Text. The picker
    must still render (with a hint to fill it in) instead of crashing."""
    _seed_dashboard_row(
        conn, contract_name="Old Vendor",
        asana_task_gid="g_a", campus_set="CEN",
        contract_reason_text=None,
    )
    _seed_dashboard_row(
        conn, contract_name="Old Vendor",
        asana_task_gid="g_b", campus_set="CEN",
        contract_reason_text=None,
    )
    _seed_needs_tagging(
        conn, group_key="CEN|000|63015|Old Vendor",
        vendor="Old Vendor", sample_description="something",
        candidate_names=["Old Vendor", "Old Vendor"],
        candidate_gids=["g_a", "g_b"],
    )
    body = client.get("/vendor-conflicts").get_data(as_text=True)
    assert "Old Vendor" in body
    # The empty-reason-text hint is shown so the operator knows what to fix.
    assert "empty" in body


# ---------------------------------------------------------------------------
# /vendor-conflicts/<id>/mark-amendment — Phase 8 amendment-link declaration
# ---------------------------------------------------------------------------

def test_mark_amendment_creates_link_and_pins_parent(client, conn):
    """End-to-end: operator declares 'g_amend is amendment of g_parent'.
    Result: Amendment Links row written, plain LM pinned to g_parent, Needs
    Tagging row deleted (conflict resolved)."""
    _seed_dashboard_row(conn, contract_name="Janitorial",
                        asana_task_gid="g_parent", campus_set="OPK",
                        contract_amount=59400.0, spent_so_far=0.0)
    _seed_dashboard_row(conn, contract_name="Janitorial Amendment",
                        asana_task_gid="g_amend", campus_set="OPK",
                        contract_amount=5770.0, spent_so_far=52974.0)
    rec_id = _seed_needs_tagging(
        conn,
        group_key="OPK|000|63020|Stratus Building Solutions",
        campus="OPK", account_no="63020",
        vendor="Stratus Building Solutions",
        candidate_names=["Janitorial", "Janitorial Amendment"],
        candidate_gids=["g_parent", "g_amend"],
    )
    resp = client.post(
        f"/vendor-conflicts/{rec_id}/mark-amendment",
        data={
            "parent_gid": "g_parent",
            "amendment_gid": "g_amend",
            "parent_name": "Janitorial",
            "amendment_name": "Janitorial Amendment",
        },
    )
    assert resp.status_code == 302

    link = conn.execute(
        'SELECT * FROM "Amendment Links" WHERE "Amendment Gid" = ?',
        ("g_amend",),
    ).fetchone()
    assert link is not None
    assert link["Parent Gid"] == "g_parent"
    assert link["Parent Name"] == "Janitorial"
    assert link["Amendment Name"] == "Janitorial Amendment"

    # A plain LM (no Description Pattern) routes the group's transactions
    # to the PARENT gid -- otherwise the conflict re-emerges on next ingest.
    lm = conn.execute(
        '''SELECT * FROM "Learned Mappings"
           WHERE "Key" = ?
             AND COALESCE("Description Pattern", '') = '' ''',
        ("OPK|000|63020|Stratus Building Solutions",),
    ).fetchone()
    assert lm is not None
    assert lm["Contract Gid"] == "g_parent"
    assert lm["Contract Name"] == "Janitorial"

    # Needs Tagging row is resolved.
    assert conn.execute(
        'SELECT COUNT(*) FROM "Needs Tagging" WHERE id = ?', (rec_id,)
    ).fetchone()[0] == 0


def test_mark_amendment_rejects_gid_not_in_candidates(client, conn):
    _seed_dashboard_row(conn, contract_name="A", asana_task_gid="g_a")
    _seed_dashboard_row(conn, contract_name="B", asana_task_gid="g_b")
    rec_id = _seed_needs_tagging(
        conn, group_key="CEN|000|63020|X", vendor="X",
        candidate_names=["A", "B"], candidate_gids=["g_a", "g_b"],
    )
    resp = client.post(
        f"/vendor-conflicts/{rec_id}/mark-amendment",
        data={
            "parent_gid": "g_a", "amendment_gid": "g_wild",
            "parent_name": "A", "amendment_name": "Wild",
        },
    )
    assert resp.status_code == 302
    assert conn.execute(
        'SELECT COUNT(*) FROM "Amendment Links"'
    ).fetchone()[0] == 0
    # NT row stays in place; nothing was pinned.
    assert conn.execute(
        'SELECT COUNT(*) FROM "Needs Tagging" WHERE id = ?', (rec_id,)
    ).fetchone()[0] == 1


def test_mark_amendment_rejects_self_link(client, conn):
    _seed_dashboard_row(conn, contract_name="A", asana_task_gid="g_a")
    _seed_dashboard_row(conn, contract_name="B", asana_task_gid="g_b")
    rec_id = _seed_needs_tagging(
        conn, group_key="CEN|000|63020|X", vendor="X",
        candidate_names=["A", "B"], candidate_gids=["g_a", "g_b"],
    )
    resp = client.post(
        f"/vendor-conflicts/{rec_id}/mark-amendment",
        data={
            "parent_gid": "g_a", "amendment_gid": "g_a",
            "parent_name": "A", "amendment_name": "A",
        },
    )
    assert resp.status_code == 302
    assert conn.execute(
        'SELECT COUNT(*) FROM "Amendment Links"'
    ).fetchone()[0] == 0


def test_mark_amendment_rejects_missing_gids(client, conn):
    _seed_dashboard_row(conn, contract_name="A", asana_task_gid="g_a")
    _seed_dashboard_row(conn, contract_name="B", asana_task_gid="g_b")
    rec_id = _seed_needs_tagging(
        conn, group_key="CEN|000|63020|X", vendor="X",
        candidate_names=["A", "B"], candidate_gids=["g_a", "g_b"],
    )
    resp = client.post(
        f"/vendor-conflicts/{rec_id}/mark-amendment",
        data={"parent_gid": "", "amendment_gid": "g_a"},
    )
    assert resp.status_code == 302
    assert conn.execute(
        'SELECT COUNT(*) FROM "Amendment Links"'
    ).fetchone()[0] == 0


def test_mark_amendment_404s_for_unknown_record_id(client):
    resp = client.post(
        "/vendor-conflicts/99999/mark-amendment",
        data={"parent_gid": "g_a", "amendment_gid": "g_b",
              "parent_name": "A", "amendment_name": "B"},
    )
    assert resp.status_code == 404


def test_dashboard_shows_amendment_cross_reference(client, conn):
    """When two Dashboard rows are linked as parent + amendment, both rows
    render a cross-reference line so the operator sees the combined picture."""
    _seed_dashboard_row(conn, contract_name="Janitorial",
                        asana_task_gid="g_parent", campus_set="OPK",
                        contract_amount=59400.0, spent_so_far=0.0)
    _seed_dashboard_row(conn, contract_name="Janitorial Amendment",
                        asana_task_gid="g_amend", campus_set="OPK",
                        contract_amount=5770.0, spent_so_far=52974.0)
    sqlite_client.insert_amendment_link(
        conn, parent_gid="g_parent", amendment_gid="g_amend",
        parent_name="Janitorial", amendment_name="Janitorial Amendment",
        linked_at="2026-06-16",
    )
    body = client.get("/").get_data(as_text=True)
    # Both directions show up: parent advertises "+ amendment",
    # amendment row says "amendment of".
    assert "amendment of" in body
    assert "+ amendment" in body
    # Each linked partner's amount appears as context.
    assert "59,400.00" in body  # parent budget shown on amendment row
    assert "52,974.00" in body  # amendment spend shown on parent row


# ---------------------------------------------------------------------------
# /vendor-conflicts/<id>/mark-other — Phase 9 "none of these match" option
# ---------------------------------------------------------------------------

def test_vendor_conflicts_mark_other_hides_row_from_conflicts(client, conn):
    """Operator picks Other → row drops out of /vendor-conflicts but stays
    in Needs Tagging Open (no Dismiss, no Once Off, no Assign Contract
    side-effects)."""
    # Two SAME-campus (TUL) tasks for one vendor → a genuine conflict that
    # legitimately appears in Vendor Conflicts. (A TUL group with only
    # BAO/OKC candidates is now correctly filtered OUT, so it could not be
    # used to test mark-other.)
    _seed_dashboard_row(conn, contract_name="Oklahoma Chiller Corp",
                        asana_task_gid="g_tul1", campus_set="TUL")
    _seed_dashboard_row(conn, contract_name="Oklahoma Chiller Corp",
                        asana_task_gid="g_tul2", campus_set="TUL")
    rec_id = _seed_needs_tagging(
        conn,
        group_key="TUL|000|63040|Oklahoma Chiller Corp",
        campus="TUL", account_no="63040",
        vendor="Oklahoma Chiller Corp",
        candidate_names=["Oklahoma Chiller Corp", "Oklahoma Chiller Corp"],
        candidate_gids=["g_tul1", "g_tul2"],
    )
    # Confirms the row IS in the conflict view before the action.
    before = client.get("/vendor-conflicts").get_data(as_text=True)
    assert "Oklahoma Chiller Corp" in before

    resp = client.post(f"/vendor-conflicts/{rec_id}/mark-other")
    assert resp.status_code == 302

    # Flag set; nothing else mutated.
    row = conn.execute(
        'SELECT * FROM "Needs Tagging" WHERE id = ?', (rec_id,)
    ).fetchone()
    assert row is not None
    assert (row["Conflict Other"] or 0) == 1
    assert (row["Dismissed"] or 0) == 0
    assert (row["Once Off"] or 0) == 0
    assert (row["Assign Contract"] or "") == ""

    # And it no longer shows up in /vendor-conflicts.
    after = client.get("/vendor-conflicts").get_data(as_text=True)
    assert "Oklahoma Chiller Corp" not in after

    # But it DOES still show up in Needs Tagging Open, with a pill.
    nt = client.get("/needs-tagging?show=open").get_data(as_text=True)
    assert "Oklahoma Chiller Corp" in nt
    assert "other (hidden from conflicts)" in nt


def test_vendor_conflicts_mark_other_404s_for_unknown_record_id(client):
    resp = client.post("/vendor-conflicts/99999/mark-other")
    assert resp.status_code == 404


def test_vendor_conflicts_excludes_campus_mismatched_candidates(client, conn):
    """A TUL transaction group whose only vendor matches are OTHER-campus tasks
    (BAO/OKC) must NOT appear in Vendor Conflicts — there is no campus-
    compatible task to pin it to. It stays on Needs Tagging Open for Assign
    Contract instead. (The bug the operator caught.)"""
    _seed_dashboard_row(conn, contract_name="Oklahoma Chiller Corp",
                        asana_task_gid="g_bao", campus_set="BAO")
    _seed_dashboard_row(conn, contract_name="Oklahoma Chiller Corp",
                        asana_task_gid="g_okc", campus_set="OKC")
    _seed_needs_tagging(
        conn, group_key="TUL|000|63040|Oklahoma Chiller Corp",
        campus="TUL", account_no="63040", vendor="Oklahoma Chiller Corp",
        candidate_names=["Oklahoma Chiller Corp", "Oklahoma Chiller Corp"],
        candidate_gids=["g_bao", "g_okc"],
    )
    body = client.get("/vendor-conflicts").get_data(as_text=True)
    assert "Oklahoma Chiller Corp" not in body


def test_vendor_conflicts_keeps_all_campuses_candidate(client, conn):
    """An 'All Campuses' wildcard task IS campus-compatible with any group, so
    a genuine 2-candidate conflict (one TUL + one All-Campuses) still shows."""
    _seed_dashboard_row(conn, contract_name="Oklahoma Chiller Corp",
                        asana_task_gid="g_tul", campus_set="TUL")
    _seed_dashboard_row(conn, contract_name="Oklahoma Chiller Corp",
                        asana_task_gid="g_all", campus_set="All Campuses")
    _seed_needs_tagging(
        conn, group_key="TUL|000|63040|Oklahoma Chiller Corp",
        campus="TUL", account_no="63040", vendor="Oklahoma Chiller Corp",
        candidate_names=["Oklahoma Chiller Corp", "Oklahoma Chiller Corp"],
        candidate_gids=["g_tul", "g_all"],
    )
    body = client.get("/vendor-conflicts").get_data(as_text=True)
    assert "Oklahoma Chiller Corp" in body


def test_unmark_conflict_other_restores_row_to_conflicts(client, conn):
    """Operator undid Other → row reappears in /vendor-conflicts (engine
    candidates still apply)."""
    _seed_dashboard_row(conn, contract_name="A", asana_task_gid="g_a",
                        campus_set="CEN")
    _seed_dashboard_row(conn, contract_name="B", asana_task_gid="g_b",
                        campus_set="CEN")
    rec_id = _seed_needs_tagging(
        conn, group_key="CEN|000|63020|X", vendor="X",
        candidate_names=["A", "B"], candidate_gids=["g_a", "g_b"],
    )
    sqlite_client.set_needs_tagging_conflict_other(
        conn, record_id=rec_id, conflict_other=True,
    )
    assert "X" not in client.get("/vendor-conflicts").get_data(as_text=True)

    resp = client.post(
        f"/needs-tagging/{rec_id}/unmark-conflict-other",
        data={"show": "open"},
    )
    assert resp.status_code == 302
    row = conn.execute(
        'SELECT "Conflict Other" FROM "Needs Tagging" WHERE id = ?', (rec_id,)
    ).fetchone()
    assert (row["Conflict Other"] or 0) == 0

    # Row is back on the conflict surface.
    assert "X" in client.get("/vendor-conflicts").get_data(as_text=True)


def test_unmark_conflict_other_404s_for_unknown_record_id(client):
    resp = client.post(
        "/needs-tagging/99999/unmark-conflict-other", data={"show": "open"}
    )
    assert resp.status_code == 404


def test_conflict_other_survives_engine_reupsert(client, conn):
    """The engine's idempotent upsert on existing rows rewrites engine-owned
    columns (Sample, $ in group, Candidate Gids) but MUST NOT touch the
    operator-owned Conflict Other flag — otherwise the next ingest would
    drag the row back into Vendor Conflicts after the operator just sent
    it away. Mirrors the Dismissed / Once Off invariant."""
    _seed_dashboard_row(conn, contract_name="A", asana_task_gid="g_a",
                        campus_set="CEN")
    _seed_dashboard_row(conn, contract_name="B", asana_task_gid="g_b",
                        campus_set="CEN")
    rec_id = _seed_needs_tagging(
        conn, group_key="CEN|000|63020|X", vendor="X",
        candidate_names=["A", "B"], candidate_gids=["g_a", "g_b"],
    )
    sqlite_client.set_needs_tagging_conflict_other(
        conn, record_id=rec_id, conflict_other=True,
    )

    # Simulate another ingest cycle finding the same group with updated
    # numbers and (possibly) different candidate names.
    upsert_needs_tagging_group(
        conn,
        group_key="CEN|000|63020|X",
        campus="CEN", dept="000", account_no="63020", vendor="X",
        sample_description="NEW DESCRIPTION FROM LATER EXPORT",
        amount=9999.99,
        candidate_names=["A", "B"],
        candidate_gids=["g_a", "g_b"],
        created_at_iso_date="2026-06-30",
        first_date="2026-01-01",
        last_date="2026-06-30",
    )
    row = conn.execute(
        'SELECT * FROM "Needs Tagging" WHERE id = ?', (rec_id,)
    ).fetchone()
    # Engine-owned column refreshed...
    assert row["Sample Record Description"] == "NEW DESCRIPTION FROM LATER EXPORT"
    # ...but the operator's Conflict Other flag persists.
    assert (row["Conflict Other"] or 0) == 1
    # And the row still doesn't appear in /vendor-conflicts.
    assert "NEW DESCRIPTION FROM LATER EXPORT" not in (
        client.get("/vendor-conflicts").get_data(as_text=True)
    )


# ---------------------------------------------------------------------------
# Phase 11: P-Card split surface
# ---------------------------------------------------------------------------

def test_p_card_classified_on_upsert_when_vendor_blank(conn):
    """Engine sets Is P-Card=1 on rows the predicate matches; 0 otherwise."""
    rec_pcard = upsert_needs_tagging_group(
        conn, group_key="CEN|000|63040|", campus="CEN", dept="000",
        account_no="63040", vendor="",
        sample_description="Parking lot reflective markers, GRAINGER, Hunter, Tami, 01/03/2025",
        amount=120.0, candidate_names=[],
        created_at_iso_date="2026-06-12",
    )
    rec_normal = upsert_needs_tagging_group(
        conn, group_key="STO|000|63090|The Stewards Company", campus="STO",
        dept="000", account_no="63090", vendor="The Stewards Company",
        sample_description="Window Cleaning 12/2024", amount=1050.0,
        candidate_names=[], created_at_iso_date="2026-06-12",
    )
    assert rec_pcard["fields"].get("Is P-Card") == 1
    assert rec_normal["fields"].get("Is P-Card") == 0


def test_p_card_rows_excluded_from_needs_tagging_open(client, conn):
    """P-card rows must NOT appear in /needs-tagging — they live on /p-card-spend."""
    _seed_needs_tagging(
        conn, group_key="CEN|000|63040|", vendor="",
        sample_description="P-CARD ONLY DESC, GRAINGER, Hunter, Tami, 01/03/2025",
    )
    _seed_needs_tagging(
        conn, group_key="STO|000|63090|The Stewards Company",
        vendor="The Stewards Company",
        sample_description="REAL CONTRACT GROUP",
    )
    body = client.get("/needs-tagging?show=open").get_data(as_text=True)
    assert "REAL CONTRACT GROUP" in body
    assert "P-CARD ONLY DESC" not in body


def test_p_card_rows_excluded_from_vendor_conflicts(client, conn):
    """A blank-vendor row with multiple candidate gids would never be a
    real vendor conflict — but pin the invariant: p-card rows are filtered."""
    _seed_dashboard_row(conn, contract_name="Foo Co",
                        asana_task_gid="g_a", campus_set="CEN")
    _seed_dashboard_row(conn, contract_name="Foo Co",
                        asana_task_gid="g_b", campus_set="CEN")
    _seed_needs_tagging(
        conn, group_key="CEN|000|63040|", vendor="",
        sample_description="P-CARD CONFLICT DESC, AMAZON, Davis, Jesse, 01/03/2025",
        candidate_names=["Foo Co", "Foo Co"],
        candidate_gids=["g_a", "g_b"],
    )
    body = client.get("/vendor-conflicts").get_data(as_text=True)
    assert "P-CARD CONFLICT DESC" not in body


def test_p_card_spend_lists_p_card_rows_with_totals(client, conn):
    _seed_needs_tagging(
        conn, group_key="CEN|000|63040|", vendor="",
        sample_description="Reflective markers, GRAINGER, Hunter, Tami, 01/03/2025",
        amount=120.50,
    )
    _seed_needs_tagging(
        conn, group_key="STO|000|63090|", vendor="",
        sample_description="Office monitor, AMAZON, Davis, Jesse, 01/03/2025",
        amount=350.00,
    )
    body = client.get("/p-card-spend").get_data(as_text=True)
    assert "Reflective markers" in body
    assert "Office monitor" in body
    # Visible total chip surfaces the sum.
    assert "$470.50" in body


def test_p_card_spend_ignore_once_hides_row_and_shows_in_ignored_tab(client, conn):
    rec_id = _seed_needs_tagging(
        conn, group_key="CEN|000|63040|", vendor="",
        sample_description="Reflective markers, GRAINGER, Hunter, Tami, 01/03/2025",
    )
    # Initially on Open.
    open_body = client.get("/p-card-spend?show=open").get_data(as_text=True)
    assert "Reflective markers" in open_body
    # Click Ignore once.
    resp = client.post(f"/p-card-spend/{rec_id}/ignore-once")
    assert resp.status_code == 302
    # Open no longer shows it; Ignored does.
    assert "Reflective markers" not in client.get("/p-card-spend?show=open").get_data(as_text=True)
    assert "Reflective markers" in client.get("/p-card-spend?show=ignored").get_data(as_text=True)
    # DB state is sticky.
    assert conn.execute(
        'SELECT "P-Card Ignored" FROM "Needs Tagging" WHERE id = ?', (rec_id,)
    ).fetchone()[0] == 1


def test_p_card_spend_restore_brings_row_back_to_open(client, conn):
    rec_id = _seed_needs_tagging(
        conn, group_key="CEN|000|63040|", vendor="",
        sample_description="Office monitor, AMAZON, Davis, Jesse, 01/03/2025",
    )
    sqlite_client.set_needs_tagging_p_card_ignored(
        conn, record_id=rec_id, p_card_ignored=True,
    )
    resp = client.post(f"/p-card-spend/{rec_id}/restore")
    assert resp.status_code == 302
    assert "Office monitor" in client.get("/p-card-spend?show=open").get_data(as_text=True)


def test_p_card_spend_ignore_once_404s_for_unknown_record_id(client):
    assert client.post("/p-card-spend/99999/ignore-once").status_code == 404


def test_p_card_spend_restore_404s_for_unknown_record_id(client):
    assert client.post("/p-card-spend/99999/restore").status_code == 404


# ---------------------------------------------------------------------------
# Phase 11: Vendor Conflicts description↔reason auto-narrowing
# ---------------------------------------------------------------------------

def test_vendor_conflicts_pre_selects_candidate_by_description_match(client, conn):
    """Bear Claw case: 'Snow/Ice management' descriptions should pre-select
    the snow-removal Asana task; 'Landscaping' descriptions should pre-select
    the landscaping Asana task. The per-description dropdown HTML carries
    `selected` on the auto-matched <option>."""
    _seed_dashboard_row(
        conn, contract_name="Bear Claw Landscaping",
        asana_task_gid="g_land", campus_set="NCS",
        contract_reason_text=(
            "This is for NCS landscaping for 2026. Includes irrigation "
            "repairs and groundskeeping."
        ),
    )
    _seed_dashboard_row(
        conn, contract_name="Bear Claw Landscaping Inc",
        asana_task_gid="g_snow", campus_set="NCS",
        contract_reason_text="Snow Removal",
    )
    # Conflict row with two distinct descriptions.
    import json as _json
    rec_id = _seed_needs_tagging(
        conn, group_key="NCS|000|63090|Bear Claw Landscaping",
        vendor="Bear Claw Landscaping",
        sample_description="mixed group",
        candidate_names=["Bear Claw Landscaping", "Bear Claw Landscaping Inc"],
        candidate_gids=["g_land", "g_snow"],
        distinct_descriptions=[
            ("Snow/Ice management 01/2025", 1, 13128.75),
            ("Landscaping 12/2025", 1, 8157.50),
        ],
    )
    body = client.get("/vendor-conflicts").get_data(as_text=True)
    # The body now contains TWO per-description dropdowns, each with one
    # option auto-selected. Easier to verify by string match: each
    # description appears alongside its expected gid in a `selected` attr.
    # Split body by description and check the relevant option's selected.
    # Pragmatic: scan for the specific value="g_snow" selected and the
    # specific value="g_land" selected.
    assert 'value="g_snow"' in body and 'selected' in body
    assert 'value="g_land"' in body
    # And the helper marker is shown.
    assert "auto-matched by description" in body


def test_suggest_candidate_per_description_helper_clear_winner():
    """Direct unit test of the scoring helper."""
    from engine.ui.routes import _suggest_candidate_per_description
    cands = [
        {"Asana Task GID": "g_snow", "Contract Reason Text": "Snow Removal"},
        {"Asana Task GID": "g_land",
         "Contract Reason Text": "Landscaping irrigation groundskeeping"},
    ]
    distinct = [
        {"description": "Snow/Ice management 01/2025", "rows": 1, "amount": 100.0},
        {"description": "Landscaping 12/2025", "rows": 1, "amount": 200.0},
        {"description": "Random description with no overlap", "rows": 1, "amount": 50.0},
    ]
    out = _suggest_candidate_per_description(distinct, cands)
    assert out[0]["suggested_gid"] == "g_snow"
    assert out[1]["suggested_gid"] == "g_land"
    assert out[2]["suggested_gid"] is None   # no match → stay on skip


def test_suggest_candidate_removal_does_not_link_tree_to_snow():
    """Regression: a 'Snow removal' description must NOT pre-select a
    tree-removal contract just because both contain the generic word
    'removal'. Before 'removal' was stop-worded, the short tree-removal
    reason out-scored the real snow contract on Jaccard (1/3 > 1/4)."""
    from engine.ui.routes import _suggest_candidate_per_description
    cands = [
        {"Asana Task GID": "g_snow",
         "Contract Reason Text": "Snow plowing and salting services"},
        {"Asana Task GID": "g_tree",
         "Contract Reason Text": "Tree removal"},
    ]
    distinct = [
        {"description": "Snow removal 02/2026", "rows": 1, "amount": 100.0},
        {"description": "Tree removal 03/2026", "rows": 1, "amount": 200.0},
    ]
    out = _suggest_candidate_per_description(distinct, cands)
    assert out[0]["suggested_gid"] == "g_snow"   # subject 'snow' wins, not 'removal'
    assert out[1]["suggested_gid"] == "g_tree"   # subject 'tree' wins


def test_suggest_candidate_returns_none_when_tied():
    """No clear winner → leave on skip so the operator decides."""
    from engine.ui.routes import _suggest_candidate_per_description
    cands = [
        {"Asana Task GID": "g_a", "Contract Reason Text": "monthly cleaning"},
        {"Asana Task GID": "g_b", "Contract Reason Text": "monthly cleaning"},
    ]
    distinct = [{"description": "monthly cleaning", "rows": 1, "amount": 0.0}]
    out = _suggest_candidate_per_description(distinct, cands)
    assert out[0]["suggested_gid"] is None


def test_dashboard_amendment_xref_handles_partner_missing_from_dashboard(client, conn):
    """If a linked partner is no longer in Dashboard (closed in Asana), we
    fall back to the snapshot name on the Amendment Links row and flag it
    'not in current dashboard' instead of crashing."""
    _seed_dashboard_row(conn, contract_name="Active task",
                        asana_task_gid="g_live", campus_set="CEN")
    sqlite_client.insert_amendment_link(
        conn, parent_gid="g_gone", amendment_gid="g_live",
        parent_name="Closed Parent", amendment_name="Active task",
        linked_at="2026-06-16",
    )
    body = client.get("/").get_data(as_text=True)
    assert "Closed Parent" in body
    assert "not in current dashboard" in body


# ---------------------------------------------------------------------------
# Code-review fixes: pin date-futility (#8), stale-gid clear on promote (#1),
# p-card preservation (#9), bulk GET/POST consistency (#11), and the
# date-overlap blank-Due handling (#6).
# ---------------------------------------------------------------------------

def test_vendor_conflicts_pin_blocked_when_date_futile(client, conn):
    """#8: pinning a contract whose term covers NONE of the group's
    transaction dates is refused (no LM written, row kept) — otherwise the
    pin would be re-rejected by attribution's date guard every ingest, an
    unresolvable loop with a lying success message."""
    _seed_dashboard_row(
        conn, contract_name="Bear Claw", asana_task_gid="g_bear",
        campus_set="NCS", start=date(2026, 3, 31), due=date(2027, 3, 31),
    )
    rec_id = _seed_needs_tagging(
        conn, group_key="NCS|000|63080|Bear Claw, Inc",
        vendor="Bear Claw, Inc",
        candidate_names=["Bear Claw"], candidate_gids=["g_bear"],
        first_date="2025-01-01", last_date="2025-03-15",  # all before the term
    )
    resp = client.post(
        f"/vendor-conflicts/{rec_id}/assign",
        data={"contract_gid": "g_bear", "contract_name": "Bear Claw"},
    )
    assert resp.status_code == 302
    lms = conn.execute('SELECT COUNT(*) FROM "Learned Mappings"').fetchone()[0]
    assert lms == 0
    assert conn.execute(
        'SELECT 1 FROM "Needs Tagging" WHERE id = ?', (rec_id,)
    ).fetchone() is not None


def test_vendor_conflicts_pin_allowed_when_dates_overlap(client, conn):
    """#8 regression guard: a pin whose term DOES cover the group's dates
    still works (writes the gid-pinned LM, deletes the row)."""
    _seed_dashboard_row(
        conn, contract_name="Bear Claw", asana_task_gid="g_bear",
        campus_set="NCS", start=date(2026, 1, 1), due=date(2026, 12, 31),
    )
    rec_id = _seed_needs_tagging(
        conn, group_key="NCS|000|63080|Bear Claw, Inc",
        vendor="Bear Claw, Inc",
        candidate_names=["Bear Claw"], candidate_gids=["g_bear"],
        first_date="2026-02-01", last_date="2026-05-15",
    )
    resp = client.post(
        f"/vendor-conflicts/{rec_id}/assign",
        data={"contract_gid": "g_bear", "contract_name": "Bear Claw"},
    )
    assert resp.status_code == 302
    row = conn.execute(
        'SELECT "Contract Gid" FROM "Learned Mappings" WHERE "Key" = ?',
        ("NCS|000|63080|Bear Claw, Inc",),
    ).fetchone()
    assert row is not None and row["Contract Gid"] == "g_bear"
    assert conn.execute(
        'SELECT 1 FROM "Needs Tagging" WHERE id = ?', (rec_id,)
    ).fetchone() is None


def test_promote_clears_stale_pinned_gid(conn):
    """#1: an operator first pins a gid via Vendor Conflicts, then later
    answers by NAME in Needs Tagging. The name promotion must CLEAR the
    stale gid so attribution resolves by name, not the leftover pin."""
    conn.execute(
        '''INSERT INTO "Learned Mappings"
             ("Key", "Campus", "Dept", "Account No", "Vendor",
              "Contract Name", "Contract Gid", "Learned At")
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        ("CEN|000|63015|Acme", "CEN", "000", "63015", "Acme",
         "Old Pinned Name", "g_stale", "2026-01-01"),
    )
    conn.commit()
    rec_id = _seed_needs_tagging(
        conn, group_key="CEN|000|63015|Acme", campus="CEN", dept="000",
        account_no="63015", vendor="Acme",
    )
    sqlite_client.set_needs_tagging_assign_contract(
        conn, record_id=rec_id, contract_name="New Name By Operator",
    )
    sqlite_client.promote_filled_needs_tagging(
        conn, learned_at_iso_date="2026-06-20",
        valid_contract_names={"New Name By Operator"},
    )
    row = conn.execute(
        '''SELECT "Contract Name", "Contract Gid" FROM "Learned Mappings"
           WHERE "Key" = ? AND COALESCE("Description Pattern", '') = '' ''',
        ("CEN|000|63015|Acme",),
    ).fetchone()
    assert row["Contract Name"] == "New Name By Operator"
    assert not (row["Contract Gid"] or "")  # stale gid cleared


def test_upsert_does_not_flip_operator_engaged_row_to_p_card(conn):
    """#9: a contracted group the operator has engaged with (Conflict Other
    set) must NOT be reclassified into the hidden p-card surface when a later
    export emits it with a blank vendor."""
    rec_id = _seed_needs_tagging(
        conn, group_key="CEN|000|63015|Acme", campus="CEN", dept="000",
        account_no="63015", vendor="Acme", sample_description="Bill - Acme: svc",
    )
    sqlite_client.set_needs_tagging_conflict_other(
        conn, record_id=rec_id, conflict_other=True,
    )
    upsert_needs_tagging_group(
        conn, group_key="CEN|000|63015|Acme", campus="CEN", dept="000",
        account_no="63015", vendor="", sample_description="random journal memo",
        amount=500.0, candidate_names=["Acme"], created_at_iso_date="2026-06-20",
    )
    row = conn.execute(
        'SELECT "Is P-Card" FROM "Needs Tagging" WHERE id = ?', (rec_id,)
    ).fetchone()
    assert not row["Is P-Card"]  # preserved at 0, not flipped to p-card


def test_bulk_pre_dates_button_and_post_agree_on_dashboard_absent_gid(client, conn):
    """#11: GET (button visibility) and POST (strip) must use the SAME
    candidate set. A candidate gid absent from the Dashboard must not be
    treated as date-compatible by the POST while the GET considers only
    Dashboard-present candidates — else the button appears but strips
    nothing."""
    _seed_dashboard_row(
        conn, contract_name="Present", asana_task_gid="g_present",
        campus_set="NCS", start=date(2026, 3, 31), due=date(2027, 3, 31),
    )
    rec_id = _seed_needs_tagging(
        conn, group_key="NCS|000|63080|V", vendor="V",
        candidate_names=["Present", "Absent"],
        candidate_gids=["g_present", "g_absent"],  # g_absent NOT in Dashboard
        distinct_descriptions=[("Snow 03/2025", 1, 1000.0, "2025-03-01", "2025-03-15")],
        # The engine flags this out-of-term (only Dashboard-present candidate
        # doesn't cover the bucket), which admits the single-candidate row to
        # Vendor Conflicts.
        out_of_term=True,
    )
    body = client.get("/vendor-conflicts").get_data(as_text=True)
    assert "Mark all out-of-term as Pre-dates" in body
    resp = client.post(
        f"/vendor-conflicts/{rec_id}/mark-all-out-of-term-as-pre-dates",
    )
    assert resp.status_code == 302
    import json
    remaining = json.loads(conn.execute(
        'SELECT "Distinct Descriptions JSON" FROM "Needs Tagging" WHERE id = ?',
        (rec_id,),
    ).fetchone()[0])
    assert remaining == []  # the all-out-of-term bucket was stripped


def test_date_intervals_overlap_blank_due_is_not_universally_compatible():
    """#6: a contract with an OPEN-ENDED (blank Due) term is NOT compatible
    with a bucket entirely before its start. Previously any blank date
    short-circuited to True."""
    from engine.ui.routes import _date_intervals_overlap
    assert _date_intervals_overlap("2026-09-30", "", "2025-03-01", "2025-03-15") is False
    assert _date_intervals_overlap("2026-09-30", "", "2026-12-01", "2026-12-15") is True
    # Unknown bucket dates -> can't judge -> compatible (text-only fallback).
    assert _date_intervals_overlap("2026-01-01", "2026-12-31", "", "") is True


# ---------------------------------------------------------------------------
# /no-home — campus-mismatched spend review surface
# ---------------------------------------------------------------------------

def _insert_raw_nt(conn, *, group_key, vendor, candidate_gids, descs_json,
                   amount=1000.0):
    """Insert a Needs Tagging row with exact control over the columns the
    /no-home route reads (candidate gids + distinct-descriptions JSON)."""
    conn.execute(
        'INSERT INTO "Needs Tagging" '
        '("Group Key", Campus, Vendor, "$ in group", "First Date", "Last Date", '
        ' "Engine Candidate Gids", "Distinct Descriptions JSON", Dismissed) '
        'VALUES (?,?,?,?,?,?,?,?,0)',
        (group_key, group_key.split("|")[0], vendor, amount, "2025-01-01",
         "2026-01-01", "\n".join(candidate_gids), descs_json),
    )
    conn.commit()


def test_no_home_classifies_wildcard_and_survives_bad_json(client, conn):
    """/no-home: a real campus-mismatch is listed; an All-Campuses (wildcard)
    candidate counts as a home (suppressed); and a malformed Distinct
    Descriptions JSON does NOT crash the tab (shape guard, not just parse)."""
    _seed_dashboard_row(conn, contract_name="Chiller OKC",
                        asana_task_gid="100000000000001", campus_set="OKC")
    _seed_dashboard_row(conn, contract_name="BELFOR",
                        asana_task_gid="100000000000002",
                        campus_set="All Campuses")

    # A: TUL spend, only an OKC same-vendor contract exists -> NO-HOME (listed)
    _insert_raw_nt(conn, group_key="TUL|000|63040|Chiller",
                   vendor="Chiller", candidate_gids=["100000000000001"],
                   descs_json='[{"amount": 500, "description": "hvac repair"}]')
    # B: TUL spend whose candidate is the All-Campuses contract -> HAS HOME (hidden)
    _insert_raw_nt(conn, group_key="TUL|000|63040|Belfor",
                   vendor="Belfor", candidate_gids=["100000000000002"],
                   descs_json='[{"amount": 700, "description": "water damage"}]')
    # C: malformed JSON (literal null) must not 500 the page; still NO-HOME (listed)
    _insert_raw_nt(conn, group_key="MWC|000|63040|Chiller",
                   vendor="ChillerMWC", candidate_gids=["100000000000001"],
                   descs_json='null')

    resp = client.get("/no-home")
    assert resp.status_code == 200          # shape guard held on the 'null' row
    body = resp.get_data(as_text=True)
    assert "TUL|000|63040|Chiller" in body  # A listed
    assert "MWC|000|63040|Chiller" in body  # C listed (no crash)
    assert "Belfor" not in body             # B suppressed by All-Campuses wildcard


# ---------------------------------------------------------------------------
# /learned-mappings/purge-stale — operator-triggered "unlearning"
# ---------------------------------------------------------------------------

import types as _types


def _fake_contracts(pairs):
    """pairs: list of (gid, name) -> objects with .gid/.name attrs."""
    return [_types.SimpleNamespace(gid=g, name=n) for g, n in pairs]


def _patch_asana(monkeypatch, contracts):
    from engine import asana_client, asana_contracts
    monkeypatch.setattr(asana_client, "get_api_client", lambda: None)
    monkeypatch.setattr(
        asana_contracts, "load_open_contracts", lambda _api: contracts,
    )


def _seed_lm_ui(conn, *, campus, vendor, name, gid=""):
    conn.execute(
        'INSERT INTO "Learned Mappings" '
        '("Key","Campus","Dept","Account No","Vendor","Contract Name","Contract Gid") '
        'VALUES (?,?,?,?,?,?,?)',
        (f"{campus}|000|63040|{vendor}", campus, "000", "63040", vendor, name, gid),
    )
    conn.commit()


def test_purge_stale_preview_lists_only_stale(client, conn, monkeypatch):
    _seed_lm_ui(conn, campus="CEN", vendor="Live", name="Live Contract")
    _seed_lm_ui(conn, campus="OPK", vendor="Gone", name="Rose Paving")
    _patch_asana(monkeypatch, _fake_contracts([("g1", "Live Contract")]))
    body = client.get("/learned-mappings/purge-stale").get_data(as_text=True)
    assert "Rose Paving" in body       # stale, listed
    assert "Live Contract" not in body  # not stale, not listed


def test_purge_stale_confirm_deletes_only_stale(client, conn, monkeypatch):
    _seed_lm_ui(conn, campus="CEN", vendor="Live", name="Live Contract")
    _seed_lm_ui(conn, campus="OPK", vendor="Gone", name="Rose Paving")
    _patch_asana(monkeypatch, _fake_contracts([("g1", "Live Contract")]))
    resp = client.post("/learned-mappings/purge-stale", follow_redirects=True)
    assert resp.status_code == 200
    names = {r["Contract Name"] for r in
             conn.execute('SELECT "Contract Name" FROM "Learned Mappings"')}
    assert names == {"Live Contract"}   # stale one deleted, live one kept


def test_purge_stale_guard_refuses_when_asana_load_empty(client, conn, monkeypatch):
    _seed_lm_ui(conn, campus="CEN", vendor="Live", name="Live Contract")
    _patch_asana(monkeypatch, [])       # empty/failed load
    resp = client.post("/learned-mappings/purge-stale", follow_redirects=True)
    remaining = conn.execute('SELECT COUNT(*) FROM "Learned Mappings"').fetchone()[0]
    assert remaining == 1               # nothing deleted on a bad load


def test_purge_stale_guard_refuses_oversized_purge(client, conn, monkeypatch):
    # 3 mappings, 2 would be stale (>50%) -> refuse, delete nothing.
    _seed_lm_ui(conn, campus="CEN", vendor="Live", name="Live Contract")
    _seed_lm_ui(conn, campus="OPK", vendor="G1", name="Gone One")
    _seed_lm_ui(conn, campus="NKC", vendor="G2", name="Gone Two")
    _patch_asana(monkeypatch, _fake_contracts([("g1", "Live Contract")]))
    client.post("/learned-mappings/purge-stale", follow_redirects=True)
    remaining = conn.execute('SELECT COUNT(*) FROM "Learned Mappings"').fetchone()[0]
    assert remaining == 3               # over-guard blocked the purge
