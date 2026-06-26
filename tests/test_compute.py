"""Tests for engine.compute — per-contract dashboard computation.

Pins every spec §7 / §8 / §9 formula. The term-window date filter on
attributed spend (critical guard against predecessor-term spend at the same
vendor/campus/account) is exercised in test_compute_spent_in_term_excludes_*.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from config import settings
from engine.asana_contracts import Contract
from engine.attribution import AttributionResult, AttributionRun
from engine.compute import (
    DashboardRow,
    annotate_with_contract,
    compute_alarm_band,
    compute_alarms,
    compute_dashboard,
    compute_pct_spent,
    compute_spending_rate,
    compute_spent_in_term,
    compute_start,
    compute_term_days,
    passes_live_gate,
)


def _contract(
    name: str = "Acme",
    *,
    gid: str = "g1",
    campus_options: frozenset[str] = frozenset({"CEN"}),
    contract_amount: float | None = 10000.0,
    target_start: date | None = date(2026, 1, 1),
    due_on: date | None = date(2026, 12, 31),
    status: str | None = "Active",
    expire_countdown: str | None = None,
    section_name: str | None = "Active - Compliant",
    pm_email: str | None = None,
) -> Contract:
    return Contract(
        gid=gid, name=name,
        campus_options=campus_options,
        contract_amount=contract_amount,
        target_start=target_start,
        due_on=due_on,
        status=status,
        expire_countdown=expire_countdown,
        pm_email=pm_email,
        section_name=section_name,
    )


def _df(*rows: dict) -> pd.DataFrame:
    """Build a small in-scope DataFrame from kwarg dicts.

    Dates passed as YYYY-MM-DD strings or date objects; converted to
    pd.Timestamp so comparisons line up with the parser's output dtype.
    """
    df = pd.DataFrame(rows)
    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"])
    return df


# ---------------------------------------------------------------------------
# passes_live_gate
# ---------------------------------------------------------------------------

def test_live_gate_passes_active_compliant_section():
    c = _contract(section_name="Active - Compliant", status="Active")
    ok, reason = passes_live_gate(c, date(2026, 6, 1))
    assert ok is True
    assert reason is None


def test_live_gate_passes_active_status_even_in_other_section():
    """Spec §7: section == Active - Compliant OR Contract Status == Active."""
    c = _contract(section_name="Some Other Section", status="Active")
    ok, _ = passes_live_gate(c, date(2026, 6, 1))
    assert ok is True


def test_live_gate_excludes_pending_onboarding():
    c = _contract(section_name="Pending Onboarding", status="Pending")
    ok, reason = passes_live_gate(c, date(2026, 6, 1))
    assert ok is False
    assert reason == "not_active"


def test_live_gate_excludes_expired_countdown():
    c = _contract(expire_countdown="EXPIRED!")
    ok, reason = passes_live_gate(c, date(2026, 6, 1))
    assert ok is False
    assert reason == "expired"


def test_live_gate_excludes_future_start():
    c = _contract(target_start=date(2027, 1, 1))
    ok, reason = passes_live_gate(c, date(2026, 6, 1))
    assert ok is False
    assert reason == "future_start"


def test_live_gate_excludes_past_due():
    """Spec §7: 'Stop once past due_on or EXPIRED → freeze last values'."""
    c = _contract(due_on=date(2025, 12, 31))
    ok, reason = passes_live_gate(c, date(2026, 6, 1))
    assert ok is False
    assert reason == "past_due"


def test_live_gate_excludes_contract_with_no_start_data():
    """No target_start AND no due_on means we can't compute term math."""
    c = _contract(target_start=None, due_on=None)
    ok, reason = passes_live_gate(c, date(2026, 6, 1))
    assert ok is False
    assert reason == "no_start_data"


# ---------------------------------------------------------------------------
# Start / term math
# ---------------------------------------------------------------------------

def test_compute_start_uses_target_start_when_set():
    c = _contract(target_start=date(2026, 3, 15), due_on=date(2027, 6, 1))
    assert compute_start(c) == date(2026, 3, 15)


def test_compute_start_falls_back_to_due_minus_12_months():
    c = _contract(target_start=None, due_on=date(2026, 12, 31))
    # 12 calendar months back from 2026-12-31 = 2025-12-31
    assert compute_start(c) == date(2025, 12, 31)


def test_compute_start_is_none_when_neither_set():
    c = _contract(target_start=None, due_on=None)
    assert compute_start(c) is None


def test_compute_term_days_uses_due_minus_start():
    assert compute_term_days(date(2026, 1, 1), date(2026, 12, 31)) == 364


def test_compute_term_days_defaults_when_due_missing():
    from engine.compute import DEFAULT_TERM_DAYS
    assert compute_term_days(date(2026, 1, 1), None) == DEFAULT_TERM_DAYS


# ---------------------------------------------------------------------------
# Attribution annotation
# ---------------------------------------------------------------------------

def _att_result(contract_name: str | None, *, campus="CEN", dept="000",
                account_no="63015", vendor="Acme", status="auto",
                gid: str = "g1") -> AttributionResult:
    """Build an AttributionResult in the new (GID-bearing) shape.

    For status='auto'/'learned' callers, the gid defaults to 'g1' to match the
    default gid of _contract() — so AttributionRun.row_gids in tests can map
    transaction Record Nos onto the contract directly.
    """
    contract_gid = gid if contract_name and status in ("auto", "learned") else None
    return AttributionResult(
        group_key=f"{campus}|{dept}|{account_no}|{vendor}",
        campus=campus, dept=dept, account_no=account_no, vendor=vendor,
        status=status,
        contract_name=contract_name,
        contract_gid=contract_gid,
        candidate_names=(contract_name,) if contract_name else (),
        candidate_gids=(contract_gid,) if contract_gid else (),
        rows=1, amount=0.0, sample_description="",
    )


def _run(*results: AttributionResult, row_gids=None) -> AttributionRun:
    """Build an AttributionRun with a POSITIONAL row_gids tuple inferred from
    the results when not provided — one entry per result, in order, equal to
    that result's contract_gid (None for unattributed). These tests construct
    their df rows in the SAME order as the results, so position aligns. Pass
    `row_gids` explicitly (a positional sequence) to model multi-row-per-group
    or mismatch scenarios.
    """
    if row_gids is None:
        row_gids = tuple(r.contract_gid for r in results)
    return AttributionRun(results=tuple(results), row_gids=tuple(row_gids))


def test_annotate_with_contract_joins_attribution_onto_df():
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme", "Date": "2026-06-01", "Amount": 100.0,
         "Record Description": "x"},
        {"Record No": "R2", "Campus": "OMH", "Dept": "107", "Account No": "63020",
         "Vendor": "Beta", "Date": "2026-06-02", "Amount": 200.0,
         "Record Description": "y"},
    )
    run = AttributionRun(
        results=(
            _att_result("Acme Contract", campus="CEN", vendor="Acme", gid="g_acme"),
            _att_result(None, campus="OMH", vendor="Beta", status="unmatched"),
        ),
        row_gids=("g_acme", None),
    )
    annotated = annotate_with_contract(df, run)
    assert annotated.loc[annotated["Record No"] == "R1", "_contract_gid"].iloc[0] == "g_acme"
    # Unmatched row gets None.
    assert pd.isna(annotated.loc[annotated["Record No"] == "R2", "_contract_gid"].iloc[0])


def test_annotate_with_contract_skips_dropped_and_ambiguous():
    """Only auto + learned groups get a contract name pinned onto the df.
    Dropped (INT) and ambiguous/unmatched groups are not attributed."""
    df = _df(
        {"Record No": "R1", "Campus": "INT", "Dept": "000", "Account No": "63015",
         "Vendor": "X", "Date": "2026-06-01", "Amount": 100.0,
         "Record Description": ""},
        {"Record No": "R2", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Y", "Date": "2026-06-01", "Amount": 100.0,
         "Record Description": ""},
    )
    run = AttributionRun(
        results=(
            _att_result(None, campus="INT", vendor="X", status="dropped"),
            _att_result(None, campus="CEN", vendor="Y", status="ambiguous"),
        ),
        row_gids=(None, None),
    )
    annotated = annotate_with_contract(df, run)
    assert pd.isna(annotated["_contract_gid"]).all()


# ---------------------------------------------------------------------------
# Term-window spend (the critical predecessor-term guard)
# ---------------------------------------------------------------------------

def test_compute_spent_in_term_excludes_predecessor_period_spend():
    """Spec §7's critical guard: the Tableau export contains predecessor-term
    spend at the same vendor/campus/account. Term filter must exclude it.
    Without this, a freshly-created contract inherits prior contract spend."""
    df = pd.DataFrame({
        "Date": pd.to_datetime(["2024-06-01", "2026-01-15", "2026-06-30"]),
        "Amount": [50000.0, 1000.0, 2000.0],
        "_contract_gid": ["g_acme", "g_acme", "g_acme"],
    })
    # Current term is 2026-01-01 to 2026-12-31. The 2024-06-01 row is
    # predecessor-term spend that must NOT count.
    spent = compute_spent_in_term(df, "g_acme", date(2026, 1, 1), date(2026, 12, 31))
    assert spent == pytest.approx(3000.0)


def test_compute_spent_in_term_excludes_post_today_spend():
    """end = min(today, due_on). Rows dated AFTER end (a future-effective
    charge in the export) must not count toward today's tally."""
    df = pd.DataFrame({
        "Date": pd.to_datetime(["2026-06-15", "2027-01-01"]),
        "Amount": [1000.0, 9999.0],
        "_contract_gid": ["g_acme", "g_acme"],
    })
    today = date(2026, 6, 30)
    spent = compute_spent_in_term(df, "g_acme", date(2026, 1, 1), today)
    assert spent == pytest.approx(1000.0)


def test_compute_spent_in_term_ignores_other_contracts():
    df = pd.DataFrame({
        "Date": pd.to_datetime(["2026-06-01", "2026-06-02"]),
        "Amount": [100.0, 200.0],
        "_contract_gid": ["g_acme", "g_beta"],
    })
    spent = compute_spent_in_term(df, "g_acme", date(2026, 1, 1), date(2026, 12, 31))
    assert spent == pytest.approx(100.0)


def test_compute_spent_in_term_sums_signed_amounts():
    """Credits arrive as negative (parser's parens cleanup). Spent = signed
    sum, so a credit reduces total spend."""
    df = pd.DataFrame({
        "Date": pd.to_datetime(["2026-06-01", "2026-06-02"]),
        "Amount": [1000.0, -250.0],
        "_contract_gid": ["g_acme", "g_acme"],
    })
    spent = compute_spent_in_term(df, "g_acme", date(2026, 1, 1), date(2026, 12, 31))
    assert spent == pytest.approx(750.0)


# ---------------------------------------------------------------------------
# % Spent
# ---------------------------------------------------------------------------

def test_compute_pct_spent_basic():
    assert compute_pct_spent(7500.0, 10000.0) == 75.0
    assert compute_pct_spent(5000.0, 10000.0) == 50.0


def test_compute_pct_spent_handles_zero_contract_amount():
    assert compute_pct_spent(100.0, 0) is None


def test_compute_pct_spent_handles_none_contract_amount():
    assert compute_pct_spent(100.0, None) is None


def test_compute_pct_spent_handles_negative_spent():
    """A heavily-credited contract may have negative spent. % spent goes
    negative — that's fine, no special handling."""
    assert compute_pct_spent(-100.0, 1000.0) == -10.0


# ---------------------------------------------------------------------------
# Spending Rate (pace ratio)
# ---------------------------------------------------------------------------

def test_spending_rate_blank_when_elapsed_under_pace_guard():
    """29 days into a 365-day term, pace guard blanks the rate so a
    brand-new contract can't trip a misleading rate alarm."""
    assert compute_spending_rate(50.0, date(2026, 6, 1), date(2026, 6, 29), 365) is None


def test_spending_rate_basic_on_pace():
    """%spent fraction / %time fraction = 0.5 / 0.5 = 1.0.

    Use an exact half-of-term anchor to avoid floor/ceil ambiguity:
    term = 365 days, elapsed = 182 days → fraction ≈ 0.4986 (close enough
    to 0.5 that the ratio lands ~1.00 within abs=0.02)."""
    rate = compute_spending_rate(50.0, date(2026, 1, 1), date(2026, 7, 2), 365)
    assert rate == pytest.approx(1.0, abs=0.02)


def test_spending_rate_double_pace():
    """100% spent at ~50% time elapsed → pace ratio ~2.0 (runaway)."""
    rate = compute_spending_rate(100.0, date(2026, 1, 1), date(2026, 7, 2), 365)
    assert rate == pytest.approx(2.0, abs=0.05)


def test_spending_rate_returns_none_when_pct_spent_is_none():
    assert compute_spending_rate(None, date(2026, 1, 1), date(2026, 6, 1), 365) is None


def test_spending_rate_returns_none_when_term_days_zero():
    # Pathological case — should not divide by zero.
    assert compute_spending_rate(50.0, date(2026, 1, 1), date(2026, 6, 1), 0) is None


# ---------------------------------------------------------------------------
# Spending Rate Alarm bands
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pct,expected", [
    (None, None),
    (0.0, None),
    (74.99, None),
    (75.0, "75%"),
    (89.99, "75%"),
    (90.0, "90%"),
    (99.99, "90%"),         # below 100 (with rounding tolerance) → still "90%"
    (100.0, "100%"),
    (99.996, "100%"),       # within rounding tolerance on the lower side → "100%"
    (100.0 + 1e-10, "100%"),  # float fuzz upward → still "100%"
    (100.005, "Over"),      # spec wording: "100.001 → Over" — anything strictly
                            # above 100 (beyond float fuzz) is Over.
    (100.01, "Over"),
    (150.0, "Over"),
    (200.0, "Over"),
])
def test_compute_alarm_band_boundaries(pct, expected):
    assert compute_alarm_band(pct) == expected


# ---------------------------------------------------------------------------
# Alarms binary
# ---------------------------------------------------------------------------

def test_alarms_clear_below_75_pct():
    assert compute_alarms(pct_spent=50.0, spending_rate=1.0, spent_so_far=5000.0) == "Clear"


def test_alarms_alarm_at_75_pct():
    assert compute_alarms(pct_spent=75.0, spending_rate=1.0, spent_so_far=5000.0) == "ALARM"


def test_alarms_alarm_via_runaway_pace_when_above_min_spend():
    """Runaway pace (rate ≥ 2.0) AND spent ≥ MIN_SPEND_FLOOR (1000)."""
    assert compute_alarms(
        pct_spent=50.0,
        spending_rate=settings.RUNAWAY_PACE,
        spent_so_far=settings.MIN_SPEND_FLOOR,
    ) == "ALARM"


def test_alarms_clear_when_runaway_pace_below_min_spend_floor():
    """Runaway pace but spent below the floor — the floor is the guard
    against brand-new tiny contracts tripping alarm via small denominators."""
    assert compute_alarms(
        pct_spent=50.0,
        spending_rate=settings.RUNAWAY_PACE,
        spent_so_far=settings.MIN_SPEND_FLOOR - 1.0,
    ) == "Clear"


def test_alarms_clear_when_pct_spent_and_rate_are_none():
    """Brand-new contract: pace guard returns None for spending_rate, and
    contract may not have %spent computable. Should be Clear."""
    assert compute_alarms(pct_spent=None, spending_rate=None, spent_so_far=0.0) == "Clear"


def test_alarms_alarm_when_over_budget():
    assert compute_alarms(pct_spent=150.0, spending_rate=1.2, spent_so_far=15000.0) == "ALARM"


# ---------------------------------------------------------------------------
# compute_dashboard end-to-end
# ---------------------------------------------------------------------------

def test_compute_dashboard_basic_live_contract():
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme", "Date": "2026-03-15", "Amount": 5000.0,
         "Record Description": "x"},
    )
    run = _run(_att_result("Acme Contract", campus="CEN", vendor="Acme"))
    contracts = [_contract(name="Acme Contract", contract_amount=10000.0)]
    rows, skip = compute_dashboard(df, run, contracts, today=date(2026, 6, 1))
    assert len(rows) == 1
    r = rows[0]
    assert r.contract_name == "Acme Contract"
    assert r.spent_so_far == pytest.approx(5000.0)
    assert r.pct_spent == pytest.approx(50.0)
    assert r.alarms == "Clear"  # 50% < 75%
    assert r.spending_rate is not None  # past 30-day pace guard


def test_compute_dashboard_skips_non_live_contracts():
    """A contract list with one live + one Pending Onboarding + one
    EXPIRED. Only the live one produces a row; the others increment
    skip_counts."""
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme", "Date": "2026-03-15", "Amount": 100.0,
         "Record Description": "x"},
    )
    run = _run(_att_result("Live", vendor="Acme"))
    contracts = [
        _contract(name="Live", gid="g1", section_name="Active - Compliant"),
        _contract(name="Pending", gid="g2", section_name="Pending Onboarding",
                  status="Pending"),
        _contract(name="Expired", gid="g3", expire_countdown="EXPIRED!"),
    ]
    rows, skip = compute_dashboard(df, run, contracts, today=date(2026, 6, 1))
    assert {r.contract_name for r in rows} == {"Live"}
    assert skip["not_active"] == 1
    assert skip["expired"] == 1


def test_compute_dashboard_alarm_trips_via_budget():
    """%spent crossing 75% trips Alarms=ALARM."""
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme", "Date": "2026-03-15", "Amount": 8000.0,
         "Record Description": "x"},
    )
    run = _run(_att_result("Acme", vendor="Acme"))
    contracts = [_contract(name="Acme", contract_amount=10000.0)]
    rows, _ = compute_dashboard(df, run, contracts, today=date(2026, 6, 1))
    assert len(rows) == 1
    assert rows[0].spending_rate_alarm == "75%"
    assert rows[0].alarms == "ALARM"


def test_compute_dashboard_amendment_budget_folds_into_parent():
    """An amendment adds budget to its parent. The parent's % / band are
    computed against the COMBINED budget, and the row reports the combined
    amount as Contract Amount so the row reads consistently. Models the real
    Stratus case: $91,590 spent, $59,400 parent + $5,770 amendment."""
    df = _df(
        {"Record No": "R1", "Campus": "OPK", "Dept": "000", "Account No": "63090",
         "Vendor": "Stratus", "Date": "2026-03-15", "Amount": 91590.0,
         "Record Description": "x"},
    )
    run = _run(_att_result("Stratus Building Solutions", campus="OPK",
                           account_no="63090", vendor="Stratus"))
    contracts = [_contract(name="Stratus Building Solutions",
                           campus_options=frozenset({"OPK"}),
                           contract_amount=59400.0)]
    # Parent budget alone: 91590 / 59400 = 154.19%.
    rows, _ = compute_dashboard(df, run, contracts, today=date(2026, 6, 1))
    assert rows[0].pct_spent == pytest.approx(154.19, abs=0.01)
    assert rows[0].contract_amount == pytest.approx(59400.0)
    # Fold in the $5,770 amendment: combined 65170, 91590 / 65170 = 140.55%.
    rows2, _ = compute_dashboard(
        df, run, contracts, today=date(2026, 6, 1),
        amendment_budgets={"g1": 5770.0},
    )
    assert rows2[0].contract_amount == pytest.approx(65170.0)
    assert rows2[0].pct_spent == pytest.approx(140.55, abs=0.05)
    assert rows2[0].spending_rate_alarm == "Over"  # still over, on combined budget


def test_attributed_lines_splits_in_and_out_of_term():
    """attributed_lines flags each assigned row in_term iff its Date falls in
    [start, min(today, due)] — the same window compute_spent_in_term uses."""
    from engine.compute import attributed_lines
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63040",
         "Vendor": "Acme", "Date": "2026-03-15", "Amount": 100.0,
         "Record Description": "in term", "Reference": "ref-1"},
        {"Record No": "R2", "Campus": "CEN", "Dept": "000", "Account No": "63040",
         "Vendor": "Acme", "Date": "2025-06-15", "Amount": 50.0,
         "Record Description": "pre term", "Reference": "ref-2"},
    )
    run = _run(_att_result("Acme", vendor="Acme"), row_gids=("g1", "g1"))
    contracts = [_contract(name="Acme", gid="g1")]  # term 2026-01-01 .. 2026-12-31
    lines = attributed_lines(df, run, contracts, today=date(2026, 6, 1))
    by_ref = {l["reference"]: l for l in lines}
    assert by_ref["ref-1"]["in_term"] is True
    assert by_ref["ref-2"]["in_term"] is False      # 2025 pre-dates the term
    assert by_ref["ref-1"]["vendor"] == "Acme"
    assert all(l["tier"] == "opex" and l["gid"] == "g1" for l in lines)


def test_attributed_lines_omits_unmatched_rows():
    """A row whose group didn't attribute cleanly (gid None) is NOT a line —
    it belongs to Needs Tagging, not a contract. This is the Clear Creek $0
    case: nothing assigned, so the drill-down is correctly empty."""
    from engine.compute import attributed_lines
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63040",
         "Vendor": "Mystery", "Date": "2026-03-15", "Amount": 100.0,
         "Record Description": "x", "Reference": "r"},
    )
    run = _run(_att_result(None, vendor="Mystery", status="unmatched"))
    contracts = [_contract(name="Acme", gid="g1")]
    assert attributed_lines(df, run, contracts, today=date(2026, 6, 1)) == []


def test_compute_dashboard_predecessor_term_spend_does_not_inflate_new_contract():
    """End-to-end regression for the spec §7 critical guard. A 2024 charge
    at the same vendor/campus/account as a 2026 contract must NOT count
    toward the 2026 contract's Spent so far."""
    df = _df(
        # Old charge — out of current term
        {"Record No": "R0", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme", "Date": "2024-06-01", "Amount": 50000.0,
         "Record Description": "prior contract"},
        # In-term charge
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme", "Date": "2026-03-15", "Amount": 1000.0,
         "Record Description": "new contract"},
    )
    # Both rows attributed to the same contract gid; the date-window filter
    # in compute_spent_in_term is what excludes R0, not the attribution layer.
    run = _run(
        _att_result("Acme", vendor="Acme"),
        row_gids=("g1", "g1"),
    )
    # Contract started 2026-01-01 — old charge should be excluded.
    contracts = [_contract(name="Acme", target_start=date(2026, 1, 1),
                            due_on=date(2026, 12, 31), contract_amount=10000.0)]
    rows, _ = compute_dashboard(df, run, contracts, today=date(2026, 6, 1))
    assert len(rows) == 1
    # Only the 1000 in-term charge should count, not the 50000 predecessor.
    assert rows[0].spent_so_far == pytest.approx(1000.0)
    assert rows[0].pct_spent == pytest.approx(10.0)


def test_compute_dashboard_brand_new_contract_has_blank_spending_rate():
    """Pace guard: a contract that started < 30 days ago has Spending Rate
    None even if % Spent is computable."""
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme", "Date": "2026-06-01", "Amount": 1000.0,
         "Record Description": "x"},
    )
    run = _run(_att_result("Acme", vendor="Acme"))
    contracts = [_contract(name="Acme", target_start=date(2026, 5, 25),
                            contract_amount=10000.0)]
    # Today is 7 days into the term (< 30 day guard).
    rows, _ = compute_dashboard(df, run, contracts, today=date(2026, 6, 1))
    assert len(rows) == 1
    assert rows[0].pct_spent == pytest.approx(10.0)
    assert rows[0].spending_rate is None


def test_compute_dashboard_dashboard_row_carries_campus_set_string():
    """Campus options are stored as a sorted comma-joined string in
    Dashboard. Sort makes the upsert byte-identical on re-runs."""
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme", "Date": "2026-03-15", "Amount": 100.0,
         "Record Description": "x"},
    )
    run = _run(_att_result("Acme", vendor="Acme"))
    contracts = [_contract(
        name="Acme",
        campus_options=frozenset({"OMH", "CEN", "SBA"}),
    )]
    rows, _ = compute_dashboard(df, run, contracts, today=date(2026, 6, 1))
    assert rows[0].campus_set == "CEN, OMH, SBA"


def test_compute_dashboard_skip_counts_include_every_reason():
    """Pins that compute_dashboard's skip_counts dict accumulates ALL five
    reasons. A regression that silently drops one would pass the unit
    passes_live_gate tests but break the Run Log skip breakdown."""
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme", "Date": "2026-03-15", "Amount": 100.0,
         "Record Description": "x"},
    )
    run = _run(_att_result("OnlyLive", vendor="Acme"))
    contracts = [
        _contract(name="OnlyLive"),
        _contract(name="Pend", gid="g_pend", section_name="Pending Onboarding",
                  status="Pending"),
        _contract(name="Exp", gid="g_exp", expire_countdown="EXPIRED!"),
        _contract(name="Fut", gid="g_fut", target_start=date(2027, 1, 1)),
        _contract(name="Past", gid="g_past", due_on=date(2025, 12, 31)),
        _contract(name="NoData", gid="g_nodata",
                  target_start=None, due_on=None),
    ]
    rows, skip = compute_dashboard(df, run, contracts, today=date(2026, 6, 1))
    assert len(rows) == 1
    assert skip == {
        "not_active": 1,
        "expired": 1,
        "future_start": 1,
        "past_due": 1,
        "no_start_data": 1,
    }


def test_compute_dashboard_runaway_pace_trips_alarm_below_75_pct():
    """End-to-end pin for the runaway-pace alarms branch: %spent < 75 but
    pace ratio >= 2.0 AND spent >= MIN_SPEND_FLOOR. Without this test, a
    regression that misorders compute_alarms's two clauses or drops the
    MIN_SPEND_FLOOR guard wouldn't be caught at the integration layer.

    Construction: 50% spent at ~16% time elapsed → pace ≈ 3.1. Contract
    Amount $100k keeps %spent at 50 (below the 75% band) so the budget
    branch can't be the one tripping the alarm.
    """
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme", "Date": "2026-02-15", "Amount": 50000.0,
         "Record Description": "x"},
    )
    run = _run(_att_result("Acme", vendor="Acme"))
    contracts = [_contract(
        name="Acme",
        contract_amount=100000.0,
        target_start=date(2026, 1, 1),
        due_on=date(2026, 12, 31),
    )]
    rows, _ = compute_dashboard(df, run, contracts, today=date(2026, 3, 1))
    assert len(rows) == 1
    r = rows[0]
    assert r.pct_spent < 75.0           # under budget band
    assert r.spending_rate is not None   # past 30-day pace guard
    assert r.spending_rate >= 2.0       # runaway
    assert r.spent_so_far >= settings.MIN_SPEND_FLOOR
    assert r.alarms == "ALARM"           # runaway-pace branch trips alarm
    assert r.spending_rate_alarm is None  # no budget band reached


def test_compute_term_days_clamps_when_due_before_start(caplog):
    """An operator data-entry error (due_on before start) is clamped to a
    1-day term AND surfaces a warning so the bad data is visible."""
    import logging
    with caplog.at_level(logging.WARNING, logger="engine.compute"):
        result = compute_term_days(date(2026, 6, 1), date(2025, 12, 31))
    assert result == 1
    assert "due_on" in caplog.text and "before start" in caplog.text


def test_term_window_filter_is_inclusive_on_both_ends():
    """A transaction dated exactly on the start date OR exactly on min(today,
    due_on) must be included. The reviewer flagged this as worth pinning."""
    df = pd.DataFrame({
        "Date": pd.to_datetime(["2026-01-01", "2026-06-30", "2025-12-31"]),
        "Amount": [100.0, 200.0, 500.0],  # last is pre-start, must be excluded
        "_contract_gid": ["g_acme", "g_acme", "g_acme"],
    })
    spent = compute_spent_in_term(df, "g_acme", date(2026, 1, 1), date(2026, 6, 30))
    # Both boundary rows count; the predecessor 2025-12-31 row does not.
    assert spent == pytest.approx(300.0)


def test_compute_dashboard_contract_with_no_attribution_has_zero_spent():
    """A live contract with no transactions attributed to it still gets a
    Dashboard row — Spent so far = 0. Real production case for brand-new
    contracts before any transactions land in the Tableau export."""
    # Empty DataFrame but with the expected columns so annotate_with_contract
    # has a stable shape to copy.
    df = pd.DataFrame(columns=[
        "Record No", "Campus", "Dept", "Account No", "Vendor",
        "Record Description", "Date", "Amount",
    ])
    run = AttributionRun(results=())
    contracts = [_contract(name="LonelyContract", contract_amount=10000.0)]
    rows, _ = compute_dashboard(df, run, contracts, today=date(2026, 6, 1))
    assert len(rows) == 1
    assert rows[0].spent_so_far == 0.0
    assert rows[0].pct_spent == 0.0
    assert rows[0].alarms == "Clear"
