"""Tests for engine.state — change detection (spec §10).

Pins the diff categories: first_run, decrease (always flag), large_swing
(threshold-gated), alarm_transition (both directions), band_transition,
crossed_100. build_review_flags formats correctly. summarize_findings
counts every category. The spec's "never auto-suppress" promise: every
diff produces at least the corresponding finding even if also large or
also alarm-transitioning.
"""

from __future__ import annotations

from datetime import date

import pytest

from config import settings
from engine.compute import DashboardRow
from engine.state import (
    ChangeFinding,
    StatePrior,
    build_review_flags,
    diff_against_prior,
    summarize_findings,
)


def _dash(
    *,
    contract_name: str = "Acme",
    spent_so_far: float = 5000.0,
    pct_spent: float | None = 50.0,
    spending_rate: float | None = 1.0,
    spending_rate_alarm: str | None = None,
    alarms: str = "Clear",
) -> DashboardRow:
    return DashboardRow(
        contract_name=contract_name,
        asana_task_gid="gid-" + contract_name,
        campus_set="CEN",
        contract_amount=10000.0,
        spent_so_far=spent_so_far,
        pct_spent=pct_spent,
        spending_rate=spending_rate,
        spending_rate_alarm=spending_rate_alarm,
        alarms=alarms,
        start=date(2026, 1, 1),
        due=date(2026, 12, 31),
        status="Active",
        pm_email=None,
        last_updated=date(2026, 6, 12),
    )


def _prior(
    *,
    contract_name: str = "Acme",
    asana_task_gid: str = "gid-Acme",
    prior_spent: float | None = 5000.0,
    prior_pct_spent: float | None = 50.0,
    prior_spending_rate: float | None = 1.0,
    prior_spending_rate_alarm: str | None = None,
    prior_alarms: str | None = "Clear",
) -> StatePrior:
    return StatePrior(
        contract_name=contract_name,
        asana_task_gid=asana_task_gid,
        prior_spent=prior_spent,
        prior_pct_spent=prior_pct_spent,
        prior_spending_rate=prior_spending_rate,
        prior_spending_rate_alarm=prior_spending_rate_alarm,
        prior_alarms=prior_alarms,
        last_processed_hash="abc123",
        last_updated_at=date(2026, 6, 11),
    )


# ---------------------------------------------------------------------------
# First-run
# ---------------------------------------------------------------------------

def test_first_run_produces_informational_finding():
    """No prior State → engine emits a `first_run` finding so the operator
    sees the first-touch event. NOT a Review concern but useful in audit."""
    findings = diff_against_prior(_dash(), prior=None)
    assert len(findings) == 1
    assert findings[0].category == "first_run"
    assert findings[0].contract_name == "Acme"


# ---------------------------------------------------------------------------
# No change — re-running on identical state produces no findings
# ---------------------------------------------------------------------------

def test_identical_state_produces_no_findings():
    """Idempotency: same prior + same dash → empty findings. The operator's
    Review Flags list stays clean on quiet days."""
    findings = diff_against_prior(_dash(), _prior())
    assert findings == []


# ---------------------------------------------------------------------------
# Decrease — any magnitude always flags (spec §10)
# ---------------------------------------------------------------------------

def test_decrease_above_noise_floor_flags():
    """A 1-cent decrease (well above the 0.005 float-noise floor) flags.
    Net-signed sum monotonicity is the invariant; any meaningful drop
    indicates a correction credit hit."""
    findings = diff_against_prior(
        _dash(spent_so_far=4999.99),
        _prior(prior_spent=5000.00),
    )
    categories = [f.category for f in findings]
    assert "decrease" in categories
    decrease = next(f for f in findings if f.category == "decrease")
    assert "decreased" in decrease.detail.lower()
    assert "5,000" in decrease.detail or "5000" in decrease.detail


def test_decrease_within_noise_floor_does_not_flag():
    """A 0.004 cent decrease is float-storage drift, not a real correction.
    Without this guard, Airtable round-tripping 5000.0 ↔ 4999.9999999...
    would spuriously flag every quiet run."""
    findings = diff_against_prior(
        _dash(spent_so_far=4999.999),
        _prior(prior_spent=5000.000),
    )
    categories = [f.category for f in findings]
    assert "decrease" not in categories


def test_large_decrease_flags_BOTH_decrease_and_large_swing():
    """Spec §10 "especially a decrease or large swing" — both signals are
    operator-relevant for the same contract when both apply. Operator
    scanning the Large Swings section shouldn't miss large-magnitude
    decreases."""
    findings = diff_against_prior(
        _dash(spent_so_far=0.0),
        _prior(prior_spent=settings.REVIEW_LARGE_DELTA_DOLLARS + 5000.0),
    )
    categories = {f.category for f in findings}
    assert "decrease" in categories
    assert "large_swing" in categories


# ---------------------------------------------------------------------------
# Large swing — threshold-gated
# ---------------------------------------------------------------------------

def test_increase_below_threshold_does_not_flag():
    """Normal week-over-week growth should not pollute Review Flags."""
    findings = diff_against_prior(
        _dash(spent_so_far=5500.0),
        _prior(prior_spent=5000.0),  # +$500, well below threshold
    )
    categories = {f.category for f in findings}
    assert "decrease" not in categories
    assert "large_swing" not in categories


def test_increase_at_or_above_threshold_flags_as_large_swing():
    findings = diff_against_prior(
        _dash(spent_so_far=5000.0 + settings.REVIEW_LARGE_DELTA_DOLLARS),
        _prior(prior_spent=5000.0),
    )
    categories = {f.category for f in findings}
    assert "large_swing" in categories


# ---------------------------------------------------------------------------
# Alarm transition — both directions
# ---------------------------------------------------------------------------

def test_alarms_clear_to_alarm_flags():
    findings = diff_against_prior(
        _dash(alarms="ALARM", spending_rate_alarm="75%", pct_spent=80.0),
        _prior(prior_alarms="Clear", prior_spending_rate_alarm=None,
                prior_pct_spent=50.0),
    )
    categories = {f.category for f in findings}
    assert "alarm_transition" in categories


def test_alarms_alarm_to_clear_also_flags():
    """Recovery is also operator-relevant — possibly a real recovery,
    possibly a misfire that needs investigating."""
    findings = diff_against_prior(
        _dash(alarms="Clear", spending_rate_alarm=None, pct_spent=50.0),
        _prior(prior_alarms="ALARM", prior_spending_rate_alarm="75%",
                prior_pct_spent=80.0),
    )
    categories = {f.category for f in findings}
    assert "alarm_transition" in categories


# ---------------------------------------------------------------------------
# Band transition
# ---------------------------------------------------------------------------

def test_band_transition_none_to_75_flags():
    findings = diff_against_prior(
        _dash(spending_rate_alarm="75%"),
        _prior(prior_spending_rate_alarm=None),
    )
    categories = {f.category for f in findings}
    assert "band_transition" in categories


def test_band_transition_75_to_over_flags():
    findings = diff_against_prior(
        _dash(spending_rate_alarm="Over"),
        _prior(prior_spending_rate_alarm="75%"),
    )
    categories = {f.category for f in findings}
    assert "band_transition" in categories


# ---------------------------------------------------------------------------
# Crossed 100% in both directions
# ---------------------------------------------------------------------------

def test_crossed_100_upward_flags():
    findings = diff_against_prior(
        _dash(pct_spent=105.0, spending_rate_alarm="Over"),
        _prior(prior_pct_spent=90.0, prior_spending_rate_alarm="90%"),
    )
    categories = {f.category for f in findings}
    assert "crossed_100" in categories


def test_crossed_100_downward_flags():
    """Operator raised Contract Amount in Asana → %spent fell from
    105% to 80%. Unusual; surface."""
    findings = diff_against_prior(
        _dash(pct_spent=80.0, spending_rate_alarm="75%"),
        _prior(prior_pct_spent=105.0, prior_spending_rate_alarm="Over"),
    )
    categories = {f.category for f in findings}
    assert "crossed_100" in categories


def test_no_crossed_100_flag_when_both_below():
    findings = diff_against_prior(
        _dash(pct_spent=80.0),
        _prior(prior_pct_spent=70.0),
    )
    categories = {f.category for f in findings}
    assert "crossed_100" not in categories


# ---------------------------------------------------------------------------
# Multiple findings can co-exist for one contract
# ---------------------------------------------------------------------------

def test_multiple_findings_for_one_contract():
    """Spec §10 never auto-suppresses. A contract that simultaneously trips
    ALARM and crosses 100% with a large swing produces multiple findings."""
    findings = diff_against_prior(
        _dash(
            spent_so_far=5000.0 + settings.REVIEW_LARGE_DELTA_DOLLARS + 1000,
            pct_spent=120.0,
            spending_rate_alarm="Over",
            alarms="ALARM",
        ),
        _prior(
            prior_spent=5000.0,
            prior_pct_spent=50.0,
            prior_spending_rate_alarm=None,
            prior_alarms="Clear",
        ),
    )
    categories = {f.category for f in findings}
    assert "large_swing" in categories
    assert "alarm_transition" in categories
    assert "band_transition" in categories
    assert "crossed_100" in categories


# ---------------------------------------------------------------------------
# build_review_flags / summarize_findings
# ---------------------------------------------------------------------------

def test_build_review_flags_empty_when_no_findings():
    assert build_review_flags([]) == ""


def test_build_review_flags_groups_by_category_with_decreases_first():
    findings = [
        ChangeFinding("A", "decrease", "Spent decreased by $300"),
        ChangeFinding("B", "alarm_transition", "Clear -> ALARM"),
        ChangeFinding("C", "large_swing", "Spent jumped $20,000"),
        ChangeFinding("D", "first_run", "first compute"),
    ]
    text = build_review_flags(findings)
    # Decreases section appears first, followed by alarm transitions, then
    # large swings. first_run is INTENTIONALLY excluded from the rendered
    # text (would flood the Run Log on Day 1).
    decrease_pos = text.find("Decreases")
    alarm_pos = text.find("Alarms transitions")
    large_pos = text.find("Large swings")
    assert 0 <= decrease_pos < alarm_pos < large_pos
    assert "First-run" not in text
    assert "first compute" not in text


def test_build_review_flags_returns_empty_when_only_first_run_findings():
    """Day-1 case: every live contract emits first_run. review_flags must
    NOT render a giant flood of informational rows. Counts still happen in
    summarize_findings for the Run Log notes line."""
    findings = [
        ChangeFinding("A", "first_run", "first compute"),
        ChangeFinding("B", "first_run", "first compute"),
        ChangeFinding("C", "first_run", "first compute"),
    ]
    assert build_review_flags(findings) == ""


def test_summarize_findings_counts_each_category():
    findings = [
        ChangeFinding("A", "decrease", "x"),
        ChangeFinding("B", "decrease", "y"),
        ChangeFinding("C", "alarm_transition", "z"),
        ChangeFinding("D", "first_run", "w"),
    ]
    s = summarize_findings(findings)
    assert s["decrease"] == 2
    assert s["alarm_transition"] == 1
    assert s["first_run"] == 1
    assert s["large_swing"] == 0
