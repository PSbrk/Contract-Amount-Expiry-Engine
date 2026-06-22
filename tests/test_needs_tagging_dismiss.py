"""Tests for the Phase-7 Needs Tagging features:
  - First Date / Last Date columns populated by upsert_needs_tagging_group
  - Dismissed flag + set_needs_tagging_dismissed helper
  - upsert is a no-op against Dismissed=1 rows (no re-surface)
  - cleanup_stale skips Dismissed=1 rows (no auto-delete)
  - UI: list filters by ?show=open|dismissed|all
  - UI: POST /needs-tagging/<id>/dismiss and /undismiss

The existing test_sqlite_client.py covers the pre-dismiss behavior of
upsert + cleanup; this file adds the differential cases without
re-asserting things that are still true.
"""

from __future__ import annotations

import sqlite3

import pytest

from engine import sqlite_client
from engine.sqlite_client import (
    cleanup_stale_needs_tagging,
    ensure_schema,
    set_needs_tagging_dismissed,
    set_needs_tagging_once_off,
    upsert_needs_tagging_group,
)
from engine.ui import create_app


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    yield c
    c.close()


@pytest.fixture
def client(conn):
    app = create_app(conn=conn)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test"
    return app.test_client()


def _all_nt(conn):
    return [dict(r) for r in conn.execute('SELECT * FROM "Needs Tagging"').fetchall()]


# ---------------------------------------------------------------------------
# Date columns
# ---------------------------------------------------------------------------

def test_upsert_persists_first_and_last_date(conn):
    upsert_needs_tagging_group(
        conn,
        group_key="CEN|000|63015|Acme",
        campus="CEN", dept="000", account_no="63015", vendor="Acme",
        sample_description="x",
        amount=100.0,
        candidate_names=[],
        created_at_iso_date="2026-06-15",
        first_date="2025-01-08",
        last_date="2025-11-30",
    )
    rows = _all_nt(conn)
    assert len(rows) == 1
    assert rows[0]["First Date"] == "2025-01-08"
    assert rows[0]["Last Date"] == "2025-11-30"


def test_upsert_refreshes_first_and_last_date_on_re_upsert(conn):
    """A subsequent run with extended/changed date bounds must update the
    stored window so the operator's lookup span stays current."""
    common = dict(
        group_key="CEN|000|63015|Acme",
        campus="CEN", dept="000", account_no="63015", vendor="Acme",
        sample_description="x",
        amount=100.0,
        candidate_names=[],
        created_at_iso_date="2026-06-15",
    )
    upsert_needs_tagging_group(
        conn, **common, first_date="2025-01-08", last_date="2025-03-30",
    )
    upsert_needs_tagging_group(
        conn, **common, first_date="2025-01-08", last_date="2025-11-30",
    )
    rows = _all_nt(conn)
    assert len(rows) == 1
    assert rows[0]["First Date"] == "2025-01-08"
    assert rows[0]["Last Date"] == "2025-11-30"


def test_upsert_first_and_last_date_default_to_empty(conn):
    """Older callers that pre-date the date params must still work; the
    new columns default to empty string at the dataclass level."""
    upsert_needs_tagging_group(
        conn,
        group_key="CEN|000|63015|Acme",
        campus="CEN", dept="000", account_no="63015", vendor="Acme",
        sample_description="x",
        amount=100.0,
        candidate_names=[],
        created_at_iso_date="2026-06-15",
    )
    rows = _all_nt(conn)
    assert rows[0]["First Date"] == ""
    assert rows[0]["Last Date"] == ""


# ---------------------------------------------------------------------------
# Dismissed flag at the storage layer
# ---------------------------------------------------------------------------

def test_set_dismissed_toggles_the_flag(conn):
    upsert_needs_tagging_group(
        conn,
        group_key="CEN|000|63015|Acme",
        campus="CEN", dept="000", account_no="63015", vendor="Acme",
        sample_description="x", amount=100.0, candidate_names=[],
        created_at_iso_date="2026-06-15",
    )
    rec_id = _all_nt(conn)[0]["id"]
    assert _all_nt(conn)[0]["Dismissed"] == 0

    set_needs_tagging_dismissed(conn, record_id=rec_id, dismissed=True)
    assert _all_nt(conn)[0]["Dismissed"] == 1

    set_needs_tagging_dismissed(conn, record_id=rec_id, dismissed=False)
    assert _all_nt(conn)[0]["Dismissed"] == 0


def test_upsert_is_noop_against_dismissed_row(conn):
    """Re-upsert against a dismissed row must NOT refresh sample / amount /
    candidates -- the operator already told us to leave this group alone.
    Preserves the audit trail of state-as-of-dismissal."""
    upsert_needs_tagging_group(
        conn,
        group_key="CEN|000|63015|Acme",
        campus="CEN", dept="000", account_no="63015", vendor="Acme",
        sample_description="ORIGINAL sample", amount=100.0,
        candidate_names=["Acme SaaS"],
        created_at_iso_date="2026-06-15",
        first_date="2025-01-01", last_date="2025-06-01",
    )
    rec_id = _all_nt(conn)[0]["id"]
    set_needs_tagging_dismissed(conn, record_id=rec_id, dismissed=True)

    # Subsequent run brings a much-changed picture for this group.
    upsert_needs_tagging_group(
        conn,
        group_key="CEN|000|63015|Acme",
        campus="CEN", dept="000", account_no="63015", vendor="Acme",
        sample_description="DIFFERENT sample", amount=999999.99,
        candidate_names=["Acme Inc", "Acme LLC"],
        created_at_iso_date="2026-06-15",
        first_date="2025-12-01", last_date="2026-06-15",
    )

    row = _all_nt(conn)[0]
    assert row["Dismissed"] == 1
    assert row["Sample Record Description"] == "ORIGINAL sample"
    assert row["$ in group"] == 100.0
    assert row["First Date"] == "2025-01-01"
    assert row["Last Date"] == "2025-06-01"
    assert "Acme SaaS" in (row["Engine Candidates"] or "")
    assert "Acme Inc" not in (row["Engine Candidates"] or "")


def test_cleanup_stale_skips_dismissed_rows(conn):
    """A dismissed row whose group is no longer in the live set must NOT be
    deleted -- otherwise the next run re-detects the group, re-creates the
    row, and the operator has to dismiss it all over again."""
    upsert_needs_tagging_group(
        conn,
        group_key="CEN|000|63015|StaleVendor",
        campus="CEN", dept="000", account_no="63015", vendor="StaleVendor",
        sample_description="x", amount=10.0, candidate_names=[],
        created_at_iso_date="2026-06-15",
    )
    rec_id = _all_nt(conn)[0]["id"]
    set_needs_tagging_dismissed(conn, record_id=rec_id, dismissed=True)

    # Run cleanup with an empty live set -- nothing is live; the row IS
    # stale by group-key membership but it's dismissed, so kept.
    deleted = cleanup_stale_needs_tagging(conn, live_group_keys=set())
    assert deleted == 0
    assert len(_all_nt(conn)) == 1


def test_cleanup_stale_still_deletes_undismissed_unfilled(conn):
    """Regression guard for the dismiss filter: ordinary stale rows
    (no Assign Contract + not dismissed) still get cleaned."""
    upsert_needs_tagging_group(
        conn,
        group_key="CEN|000|63015|StaleVendor",
        campus="CEN", dept="000", account_no="63015", vendor="StaleVendor",
        sample_description="x", amount=10.0, candidate_names=[],
        created_at_iso_date="2026-06-15",
    )
    deleted = cleanup_stale_needs_tagging(conn, live_group_keys=set())
    assert deleted == 1
    assert _all_nt(conn) == []


# ---------------------------------------------------------------------------
# UI routes
# ---------------------------------------------------------------------------

def _seed_nt(conn, group_key: str, *, dismissed: bool = False,
             assigned: str = "", amount: float = 100.0) -> int:
    upsert_needs_tagging_group(
        conn,
        group_key=group_key,
        campus="CEN", dept="000", account_no="63015",
        vendor=group_key.rsplit("|", 1)[-1],
        sample_description=f"sample for {group_key}",
        amount=amount, candidate_names=[],
        created_at_iso_date="2026-06-15",
        first_date="2025-03-15", last_date="2025-11-22",
    )
    rec_id = _all_nt(conn)[-1]["id"]
    if dismissed:
        set_needs_tagging_dismissed(conn, record_id=rec_id, dismissed=True)
    if assigned:
        sqlite_client.set_needs_tagging_assign_contract(
            conn, record_id=rec_id, contract_name=assigned,
        )
    return rec_id


def test_get_needs_tagging_default_hides_dismissed(conn, client):
    _seed_nt(conn, "CEN|000|63015|OpenVendor")
    _seed_nt(conn, "CEN|000|63015|DismissedVendor", dismissed=True)

    body = client.get("/needs-tagging").get_data(as_text=True)
    assert "OpenVendor" in body
    assert "DismissedVendor" not in body
    # Header chip surfaces how many dismissed are hidden.
    assert "1 dismissed (hidden)" in body


def test_get_needs_tagging_show_dismissed_shows_only_dismissed(conn, client):
    _seed_nt(conn, "CEN|000|63015|OpenVendor")
    _seed_nt(conn, "CEN|000|63015|DismissedVendor", dismissed=True)

    body = client.get("/needs-tagging?show=dismissed").get_data(as_text=True)
    assert "DismissedVendor" in body
    assert "OpenVendor" not in body


def test_get_needs_tagging_show_all_shows_both(conn, client):
    _seed_nt(conn, "CEN|000|63015|OpenVendor")
    _seed_nt(conn, "CEN|000|63015|DismissedVendor", dismissed=True)

    body = client.get("/needs-tagging?show=all").get_data(as_text=True)
    assert "OpenVendor" in body
    assert "DismissedVendor" in body


def test_get_needs_tagging_renders_first_and_last_date(conn, client):
    _seed_nt(conn, "CEN|000|63015|DatedVendor")
    body = client.get("/needs-tagging").get_data(as_text=True)
    assert "2025-03-15" in body
    assert "2025-11-22" in body


def test_post_dismiss_marks_row_and_redirects(conn, client):
    rec_id = _seed_nt(conn, "CEN|000|63015|TargetVendor")

    resp = client.post(f"/needs-tagging/{rec_id}/dismiss")
    assert resp.status_code == 302
    assert _all_nt(conn)[0]["Dismissed"] == 1


def test_post_undismiss_restores_row(conn, client):
    rec_id = _seed_nt(conn, "CEN|000|63015|TargetVendor", dismissed=True)
    assert _all_nt(conn)[0]["Dismissed"] == 1

    resp = client.post(f"/needs-tagging/{rec_id}/undismiss")
    assert resp.status_code == 302
    assert _all_nt(conn)[0]["Dismissed"] == 0


def test_post_dismiss_on_unknown_id_404s(conn, client):
    resp = client.post("/needs-tagging/9999/dismiss")
    assert resp.status_code == 404


def test_post_dismiss_preserves_assign_contract_and_notes(conn, client):
    """Dismiss is orthogonal to assign. If the operator dismisses a row that
    already has an Assign Contract, the assignment stays. (promote_filled
    will still promote it on the next run, which is the safer behavior --
    the operator can re-dismiss if they really mean it.)"""
    rec_id = _seed_nt(conn, "CEN|000|63015|TargetVendor", assigned="Acme SaaS")

    client.post(f"/needs-tagging/{rec_id}/dismiss")
    row = _all_nt(conn)[0]
    assert row["Dismissed"] == 1
    assert row["Assign Contract"] == "Acme SaaS"


# ---------------------------------------------------------------------------
# Once Off — third state alongside Open and Dismissed
# ---------------------------------------------------------------------------

def test_set_once_off_snapshots_last_date_as_anchor(conn):
    """Marking a row as Once Off captures the row's CURRENT Last Date as
    the anchor — that's what the next ingest compares against to decide
    whether to re-surface."""
    upsert_needs_tagging_group(
        conn,
        group_key="CEN|000|63080|OneOffVendor",
        campus="CEN", dept="000", account_no="63080", vendor="OneOffVendor",
        sample_description="x", amount=500.0, candidate_names=[],
        created_at_iso_date="2026-06-15",
        first_date="2025-04-01", last_date="2025-04-01",
    )
    rec_id = _all_nt(conn)[0]["id"]
    set_needs_tagging_once_off(conn, record_id=rec_id, once_off=True)

    row = _all_nt(conn)[0]
    assert row["Once Off"] == 1
    assert row["Once Off Anchor"] == "2025-04-01"


def test_unmark_once_off_clears_anchor(conn):
    upsert_needs_tagging_group(
        conn,
        group_key="CEN|000|63080|OneOffVendor",
        campus="CEN", dept="000", account_no="63080", vendor="OneOffVendor",
        sample_description="x", amount=500.0, candidate_names=[],
        created_at_iso_date="2026-06-15",
        first_date="2025-04-01", last_date="2025-04-01",
    )
    rec_id = _all_nt(conn)[0]["id"]
    set_needs_tagging_once_off(conn, record_id=rec_id, once_off=True)
    set_needs_tagging_once_off(conn, record_id=rec_id, once_off=False)

    row = _all_nt(conn)[0]
    assert row["Once Off"] == 0
    assert row["Once Off Anchor"] is None


def test_upsert_is_noop_when_once_off_and_no_new_activity(conn):
    """Once Off snoozes the row. Subsequent --ingest runs that bring no
    NEW transactions (Last Date unchanged from the anchor) must NOT
    refresh the row -- the operator's snapshot of state-at-marking stays."""
    upsert_needs_tagging_group(
        conn,
        group_key="CEN|000|63080|OneOffVendor",
        campus="CEN", dept="000", account_no="63080", vendor="OneOffVendor",
        sample_description="ORIGINAL sample", amount=500.0,
        candidate_names=["X"], created_at_iso_date="2026-06-15",
        first_date="2025-04-01", last_date="2025-04-01",
    )
    rec_id = _all_nt(conn)[0]["id"]
    set_needs_tagging_once_off(conn, record_id=rec_id, once_off=True)

    # Re-ingest with the SAME Last Date — no new activity.
    upsert_needs_tagging_group(
        conn,
        group_key="CEN|000|63080|OneOffVendor",
        campus="CEN", dept="000", account_no="63080", vendor="OneOffVendor",
        sample_description="DIFFERENT sample", amount=9999.99,
        candidate_names=["Y"], created_at_iso_date="2026-06-15",
        first_date="2025-04-01", last_date="2025-04-01",
    )
    row = _all_nt(conn)[0]
    assert row["Once Off"] == 1
    assert row["Once Off Anchor"] == "2025-04-01"
    assert row["Sample Record Description"] == "ORIGINAL sample"
    assert row["$ in group"] == 500.0


def test_upsert_resurfaces_once_off_when_new_activity_arrives(conn):
    """If a future export has transactions DATED AFTER the anchor, the
    once-off flag clears and the row resurfaces with fresh aggregates."""
    upsert_needs_tagging_group(
        conn,
        group_key="CEN|000|63080|OneOffVendor",
        campus="CEN", dept="000", account_no="63080", vendor="OneOffVendor",
        sample_description="snapshot", amount=500.0, candidate_names=["X"],
        created_at_iso_date="2026-06-15",
        first_date="2025-04-01", last_date="2025-04-01",
    )
    rec_id = _all_nt(conn)[0]["id"]
    set_needs_tagging_once_off(conn, record_id=rec_id, once_off=True)

    # Next ingest — a new row dated AFTER the anchor pulls the group back.
    upsert_needs_tagging_group(
        conn,
        group_key="CEN|000|63080|OneOffVendor",
        campus="CEN", dept="000", account_no="63080", vendor="OneOffVendor",
        sample_description="new activity!", amount=1500.0, candidate_names=["Y"],
        created_at_iso_date="2026-09-01",
        first_date="2025-04-01", last_date="2026-08-15",
    )
    row = _all_nt(conn)[0]
    assert row["Once Off"] == 0
    assert row["Once Off Anchor"] is None
    assert row["Sample Record Description"] == "new activity!"
    assert row["Last Date"] == "2026-08-15"


def test_upsert_resurfaces_once_off_with_empty_anchor_when_dates_arrive(conn):
    """#10: a group parked Once Off while it had NO parsable dates stores an
    EMPTY anchor. When a later export brings real dated activity, the group
    must resurface — an empty anchor previously made the resurface guard
    falsy, suppressing the row forever."""
    upsert_needs_tagging_group(
        conn,
        group_key="CEN|000|63080|NoDateVendor",
        campus="CEN", dept="000", account_no="63080", vendor="NoDateVendor",
        sample_description="undated snapshot", amount=500.0,
        candidate_names=["X"], created_at_iso_date="2026-06-15",
        first_date="", last_date="",  # no parsable dates at mark time
    )
    rec_id = _all_nt(conn)[0]["id"]
    set_needs_tagging_once_off(conn, record_id=rec_id, once_off=True)
    # Anchor was captured as empty.
    assert (_all_nt(conn)[0]["Once Off Anchor"] or "") == ""

    # Later export brings real dated activity.
    upsert_needs_tagging_group(
        conn,
        group_key="CEN|000|63080|NoDateVendor",
        campus="CEN", dept="000", account_no="63080", vendor="NoDateVendor",
        sample_description="now it has dates", amount=1500.0,
        candidate_names=["Y"], created_at_iso_date="2026-09-01",
        first_date="2026-08-01", last_date="2026-08-15",
    )
    row = _all_nt(conn)[0]
    assert row["Once Off"] == 0
    assert row["Once Off Anchor"] is None
    assert row["Sample Record Description"] == "now it has dates"


def test_cleanup_stale_skips_once_off_rows(conn):
    """Once Off rows are NEVER deleted by cleanup_stale — the anchor would
    be lost and the re-surface logic would break."""
    upsert_needs_tagging_group(
        conn,
        group_key="CEN|000|63080|OneOffVendor",
        campus="CEN", dept="000", account_no="63080", vendor="OneOffVendor",
        sample_description="x", amount=10.0, candidate_names=[],
        created_at_iso_date="2026-06-15",
        first_date="2025-04-01", last_date="2025-04-01",
    )
    rec_id = _all_nt(conn)[0]["id"]
    set_needs_tagging_once_off(conn, record_id=rec_id, once_off=True)

    deleted = cleanup_stale_needs_tagging(conn, live_group_keys=set())
    assert deleted == 0
    assert len(_all_nt(conn)) == 1


def test_post_mark_once_off_writes_flag_and_anchor(client, conn):
    rec_id = _seed_nt(conn, "CEN|000|63080|TargetVendor")
    # Refresh the row's Last Date so the anchor isn't blank.
    conn.execute(
        'UPDATE "Needs Tagging" SET "Last Date" = ? WHERE id = ?',
        ("2025-11-22", rec_id),
    )
    conn.commit()

    resp = client.post(f"/needs-tagging/{rec_id}/mark-once-off")
    assert resp.status_code == 302
    row = _all_nt(conn)[0]
    assert row["Once Off"] == 1
    assert row["Once Off Anchor"] == "2025-11-22"


def test_post_unmark_once_off_clears_flag_and_anchor(client, conn):
    rec_id = _seed_nt(conn, "CEN|000|63080|TargetVendor")
    conn.execute(
        'UPDATE "Needs Tagging" SET "Last Date" = ? WHERE id = ?',
        ("2025-11-22", rec_id),
    )
    conn.commit()
    client.post(f"/needs-tagging/{rec_id}/mark-once-off")

    resp = client.post(f"/needs-tagging/{rec_id}/unmark-once-off")
    assert resp.status_code == 302
    row = _all_nt(conn)[0]
    assert row["Once Off"] == 0
    assert row["Once Off Anchor"] is None


def test_post_mark_once_off_404s_for_unknown_id(client):
    resp = client.post("/needs-tagging/9999/mark-once-off")
    assert resp.status_code == 404


def test_get_needs_tagging_once_off_view_shows_only_once_off(conn, client):
    _seed_nt(conn, "CEN|000|63080|OpenVendor")
    once_id = _seed_nt(conn, "CEN|000|63080|OnceOffVendor")
    set_needs_tagging_once_off(conn, record_id=once_id, once_off=True)

    body = client.get("/needs-tagging?show=once_off").get_data(as_text=True)
    assert "OnceOffVendor" in body
    assert "OpenVendor" not in body


def test_get_needs_tagging_open_view_hides_once_off(conn, client):
    _seed_nt(conn, "CEN|000|63080|OpenVendor")
    once_id = _seed_nt(conn, "CEN|000|63080|OnceOffVendor")
    set_needs_tagging_once_off(conn, record_id=once_id, once_off=True)

    body = client.get("/needs-tagging").get_data(as_text=True)
    assert "OpenVendor" in body
    assert "OnceOffVendor" not in body
    # The header chip should announce that once-off rows are hidden.
    assert "1 once-off (hidden)" in body
