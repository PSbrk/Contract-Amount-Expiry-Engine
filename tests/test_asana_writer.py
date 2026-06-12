"""Tests for engine.asana_writer — the Step 5 Asana update path.

Covers diff idempotency, payload shape, dry-run safety, WRITE_TEST_CONTRACT
scope-down, error capture, and None-clear semantics. Real Asana API calls
are stubbed via a minimal fake TasksApi — no live network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

from config import settings
from engine.asana_contracts import Contract
from engine.asana_writer import (
    FieldDelta,
    WriteResult,
    WriteRunSummary,
    apply_writes,
    build_custom_fields_payload,
    diff_dashboard_vs_current,
    summarize,
)
from engine.compute import DashboardRow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SENTINEL = object()  # marks "use default which matches dashboard default"


def _contract(
    *,
    gid: str = "gid-acme",
    name: str = "Acme",
    current_spent_so_far=_SENTINEL,
    current_pct_spent=_SENTINEL,
    current_spending_rate=_SENTINEL,
    current_spending_rate_alarm=_SENTINEL,
    current_alarms=_SENTINEL,
) -> Contract:
    """Contract helper whose current_* defaults match the _dashboard()
    defaults. Tests that target one field only need to override that field
    on Contract OR on Dashboard to produce a focused diff; everything else
    stays in sync."""
    def d(arg, default):
        return default if arg is _SENTINEL else arg
    return Contract(
        gid=gid, name=name,
        campus_options=frozenset({"CEN"}),
        contract_amount=10000.0,
        target_start=date(2026, 1, 1),
        due_on=date(2026, 12, 31),
        status="Active",
        expire_countdown=None,
        pm_email=None,
        section_name="Active - Compliant",
        current_spent_so_far=d(current_spent_so_far, 5000.0),
        current_pct_spent=d(current_pct_spent, 50.0),
        current_spending_rate=d(current_spending_rate, 1.0),
        current_spending_rate_alarm=d(current_spending_rate_alarm, None),
        current_alarms=d(current_alarms, "Clear"),
    )


def _dashboard(
    *,
    gid: str = "gid-acme",
    name: str = "Acme",
    spent_so_far: float = 5000.0,
    pct_spent: float | None = 50.0,
    spending_rate: float | None = 1.0,
    spending_rate_alarm: str | None = None,
    alarms: str = "Clear",
) -> DashboardRow:
    return DashboardRow(
        contract_name=name,
        asana_task_gid=gid,
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


# ---------------------------------------------------------------------------
# Diff — idempotency
# ---------------------------------------------------------------------------

def test_diff_returns_empty_when_dashboard_matches_current_exactly():
    """The canonical idempotency case: re-running on identical state writes
    nothing."""
    c = _contract(
        current_spent_so_far=5000.0,
        current_pct_spent=50.0,
        current_spending_rate=1.0,
        current_spending_rate_alarm=None,
        current_alarms="Clear",
    )
    d = _dashboard()
    assert diff_dashboard_vs_current(d, c) == []


def test_diff_detects_spent_so_far_change():
    c = _contract(current_spent_so_far=5000.0)
    d = _dashboard(spent_so_far=6000.0)
    deltas = diff_dashboard_vs_current(d, c)
    field_names = [x.field_name for x in deltas]
    assert "Spent so far" in field_names
    delta = next(x for x in deltas if x.field_name == "Spent so far")
    assert delta.old_value == 5000.0
    assert delta.new_value == 6000.0


def test_diff_detects_alarm_band_change():
    c = _contract(current_spending_rate_alarm=None)
    d = _dashboard(spending_rate_alarm="75%")
    deltas = diff_dashboard_vs_current(d, c)
    names = [x.field_name for x in deltas]
    assert names == ["Spending Rate Alarm"]


def test_diff_detects_alarms_clear_to_alarm_transition():
    c = _contract(current_alarms="Clear")
    d = _dashboard(alarms="ALARM")
    deltas = diff_dashboard_vs_current(d, c)
    assert any(x.field_name == "Alarms" for x in deltas)


def test_diff_detects_none_to_value_transition():
    """A never-set field (None) transitioning to a number triggers a write."""
    c = _contract(current_spending_rate=None)
    d = _dashboard(spending_rate=1.5)
    deltas = diff_dashboard_vs_current(d, c)
    assert any(x.field_name == "Spending Rate" for x in deltas)


def test_diff_detects_value_to_none_transition():
    """A previously-set field transitioning back to None (pace guard
    blanking, band dropping below 75%) triggers a write to CLEAR the cell."""
    c = _contract(
        current_spending_rate=1.5,
        current_spending_rate_alarm="75%",
        current_alarms="ALARM",
    )
    d = _dashboard(
        spending_rate=None,        # pace guard kicked in
        spending_rate_alarm=None,  # band dropped below 75%
        alarms="Clear",
    )
    deltas = diff_dashboard_vs_current(d, c)
    names = {x.field_name for x in deltas}
    assert names == {"Spending Rate", "Spending Rate Alarm", "Alarms"}


def test_diff_absorbs_subcent_float_drift_on_numbers():
    """Number comparison tolerates a sub-cent delta to absorb float fuzz.
    A computed 75.50 against an Asana-stored 75.5000001 should NOT trigger
    a write (would cause a spurious activity-log entry every run)."""
    c = _contract(current_spent_so_far=5000.0000001)
    d = _dashboard(spent_so_far=5000.0)
    deltas = diff_dashboard_vs_current(d, c)
    assert deltas == []


def test_diff_catches_meaningful_cent_difference():
    """Spec is about preventing no-op rewrites, not hiding real changes.
    A 1-cent delta is meaningful and must be written."""
    c = _contract(current_spent_so_far=5000.00)
    d = _dashboard(spent_so_far=5000.01)
    deltas = diff_dashboard_vs_current(d, c)
    # 0.01 < 0.005 tolerance? No, 0.01 > 0.005 — must be flagged.
    assert any(x.field_name == "Spent so far" for x in deltas)


# ---------------------------------------------------------------------------
# Payload construction
# ---------------------------------------------------------------------------

def test_payload_uses_field_gids_as_keys():
    deltas = [
        FieldDelta("Spent so far", 0.0, 1234.56),
        FieldDelta("% Spent", None, 50.0),
    ]
    payload = build_custom_fields_payload(deltas)
    assert payload == {
        settings.ASANA_FIELD_SPENT_SO_FAR: 1234.56,
        settings.ASANA_FIELD_PCT_SPENT: 50.0,
    }


def test_payload_resolves_enum_option_name_to_gid():
    """Asana's API takes option GIDs for enum writes, NOT option names.
    The writer must translate '75%' to its GID before sending."""
    deltas = [FieldDelta("Spending Rate Alarm", None, "75%")]
    payload = build_custom_fields_payload(deltas)
    expected_gid = settings.ASANA_SPENDING_RATE_ALARM_OPTIONS["75%"]
    assert payload == {settings.ASANA_FIELD_SPENDING_RATE_ALARM: expected_gid}


def test_payload_resolves_alarms_alarm_to_gid():
    deltas = [FieldDelta("Alarms", "Clear", "ALARM")]
    payload = build_custom_fields_payload(deltas)
    expected_gid = settings.ASANA_ALARMS_OPTIONS["ALARM"]
    assert payload == {settings.ASANA_FIELD_ALARMS: expected_gid}


def test_payload_preserves_none_for_enum_clear():
    """Setting an enum to None at the API level clears the field. We must
    NOT translate None into some sentinel string."""
    deltas = [FieldDelta("Spending Rate Alarm", "75%", None)]
    payload = build_custom_fields_payload(deltas)
    assert payload == {settings.ASANA_FIELD_SPENDING_RATE_ALARM: None}


def test_payload_rejects_unknown_enum_option():
    """A typo in compute.py would otherwise spawn a confusing Asana 4xx;
    fail loudly in the writer instead."""
    deltas = [FieldDelta("Alarms", "Clear", "TYPO")]
    with pytest.raises(ValueError, match="TYPO"):
        build_custom_fields_payload(deltas)


# ---------------------------------------------------------------------------
# apply_writes — dry-run safety
# ---------------------------------------------------------------------------

class _RecordingTasksApi:
    """Stand-in for asana.TasksApi that records update_task calls instead
    of hitting the network."""

    def __init__(self):
        self.calls: list[tuple[dict, str, dict]] = []

    def update_task(self, body, task_gid, opts=None):
        self.calls.append((body, task_gid, opts or {}))
        return {"data": {"gid": task_gid}}


@pytest.fixture
def fake_tasks_api(monkeypatch):
    """Patch asana.TasksApi(api_client) to return a recording fake."""
    fake = _RecordingTasksApi()
    import engine.asana_writer as writer
    monkeypatch.setattr(writer.asana, "TasksApi", lambda api_client: fake)
    return fake


def test_apply_writes_dry_run_never_calls_asana(fake_tasks_api):
    """The default-build-mode dry-run guard must hold absolutely."""
    c = _contract(current_spent_so_far=0.0)
    d = _dashboard(spent_so_far=100.0)
    result = apply_writes(api_client=object(), dash=d, contract=c, dry_run=True)
    assert result.dry_run is True
    assert result.deltas != ()              # diff DID run
    assert fake_tasks_api.calls == []       # but no API call


def test_apply_writes_no_change_skips_with_reason(fake_tasks_api):
    """Idempotency: matching state → no API call, even in non-dry-run."""
    c = _contract(
        current_spent_so_far=5000.0, current_pct_spent=50.0,
        current_spending_rate=1.0, current_spending_rate_alarm=None,
        current_alarms="Clear",
    )
    d = _dashboard()
    result = apply_writes(api_client=object(), dash=d, contract=c, dry_run=False)
    assert result.skipped_reason == "no_change"
    assert result.deltas == ()
    assert fake_tasks_api.calls == []


def test_apply_writes_live_sends_only_changed_fields(fake_tasks_api):
    """Only the differing fields land in the API payload — Asana's activity
    log shouldn't log fields that didn't actually change."""
    c = _contract(
        current_spent_so_far=5000.0,        # matches dash
        current_pct_spent=40.0,             # differs (50.0 in dash)
        current_spending_rate=1.0,          # matches
        current_spending_rate_alarm=None,   # matches
        current_alarms="Clear",             # matches
    )
    d = _dashboard()  # spent=5000, pct=50, rate=1, alarm=None, alarms=Clear

    result = apply_writes(api_client=object(), dash=d, contract=c, dry_run=False)
    assert result.dry_run is False
    assert result.error is None
    assert {x.field_name for x in result.deltas} == {"% Spent"}
    # Exactly one API call.
    assert len(fake_tasks_api.calls) == 1
    body, task_gid, opts = fake_tasks_api.calls[0]
    assert task_gid == c.gid
    # Body has only the ONE changed field's GID.
    assert body == {
        "data": {
            "custom_fields": {settings.ASANA_FIELD_PCT_SPENT: 50.0},
        },
    }


def test_apply_writes_test_contract_filter_skips_other_tasks(fake_tasks_api):
    """WRITE_TEST_CONTRACT scope-down — only the named task receives writes."""
    c_target = _contract(gid="target", current_spent_so_far=0.0)
    c_other = _contract(gid="other", current_spent_so_far=0.0)
    d_target = _dashboard(gid="target", spent_so_far=100.0)
    d_other = _dashboard(gid="other", spent_so_far=200.0)

    r_target = apply_writes(api_client=object(), dash=d_target, contract=c_target,
                             dry_run=False, test_contract_gid="target")
    r_other = apply_writes(api_client=object(), dash=d_other, contract=c_other,
                            dry_run=False, test_contract_gid="target")

    assert r_target.skipped_reason is None
    assert r_target.deltas != ()
    assert r_other.skipped_reason == "test_contract_filter"
    assert r_other.deltas == ()
    # Only one API call — for the target contract.
    assert len(fake_tasks_api.calls) == 1
    assert fake_tasks_api.calls[0][1] == "target"


def test_apply_writes_captures_api_error_without_propagating(fake_tasks_api, monkeypatch):
    """An Asana API failure on one contract must NOT abort the whole run.
    The error is captured in the WriteResult so the caller can summarize."""
    def raise_on_update(body, task_gid, opts=None):
        raise RuntimeError("simulated 500 from Asana")
    fake_tasks_api.update_task = raise_on_update

    c = _contract(current_spent_so_far=0.0)
    d = _dashboard(spent_so_far=100.0)
    result = apply_writes(api_client=object(), dash=d, contract=c, dry_run=False)
    assert result.error is not None
    assert "simulated 500" in result.error
    assert result.dry_run is False


def test_apply_writes_dry_run_with_test_contract_filter_combined(fake_tasks_api):
    """Both gates active — the non-target task is filtered out before the
    dry-run log even fires."""
    c = _contract(gid="other", current_spent_so_far=0.0)
    d = _dashboard(gid="other", spent_so_far=100.0)
    result = apply_writes(api_client=None, dash=d, contract=c,
                           dry_run=True, test_contract_gid="target")
    assert result.skipped_reason == "test_contract_filter"
    assert fake_tasks_api.calls == []


def test_apply_writes_dry_run_safe_with_none_api_client_and_real_diff(monkeypatch):
    """Pin the docstring promise: dry-run is safe even with api_client=None
    AND a non-empty diff. The writer must not even instantiate TasksApi
    in that path. If a regression ever calls TasksApi in dry-run mode,
    this fixture's exploding stub fails the test."""
    import engine.asana_writer as writer

    def _explode(*args, **kwargs):
        raise AssertionError("TasksApi must NOT be instantiated in dry-run mode")
    monkeypatch.setattr(writer.asana, "TasksApi", _explode)

    c = _contract(current_spent_so_far=0.0)
    d = _dashboard(spent_so_far=100.0)
    result = apply_writes(api_client=None, dash=d, contract=c, dry_run=True)
    assert result.dry_run is True
    assert result.deltas != ()


def test_apply_writes_raises_runtime_error_on_live_without_api_client():
    """Configuration mistake: dry_run=False with api_client=None must raise
    a clear RuntimeError rather than the confusing crash from
    asana.TasksApi(None)."""
    c = _contract(current_spent_so_far=0.0)
    d = _dashboard(spent_so_far=100.0)
    with pytest.raises(RuntimeError, match="api_client"):
        apply_writes(api_client=None, dash=d, contract=c, dry_run=False)


def test_diff_number_tolerance_boundary():
    """Pin the exact 0.005 boundary: a delta of 0.005 IS flagged; a delta
    just below it is absorbed. The Asana automation rule re-fires on a
    spurious rewrite of an unchanged ALARM, so the boundary semantic is
    operator-visible."""
    from engine.asana_writer import _NUMBER_TOLERANCE
    assert _NUMBER_TOLERANCE == 0.005, (
        "Tolerance constant moved; update callers + this test together."
    )
    # Delta of exactly 0.005 — flagged (>= boundary).
    c = _contract(current_spent_so_far=5000.000)
    d = _dashboard(spent_so_far=5000.005)
    deltas = diff_dashboard_vs_current(d, c)
    assert any(x.field_name == "Spent so far" for x in deltas)
    # Delta of 0.004999 — absorbed (< boundary).
    c = _contract(current_spent_so_far=5000.000)
    d = _dashboard(spent_so_far=5000.004999)
    deltas = diff_dashboard_vs_current(d, c)
    assert not any(x.field_name == "Spent so far" for x in deltas)


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------

def test_summarize_buckets_each_result_correctly():
    """contracts_changed counts results with deltas that weren't skipped or
    errored, regardless of dry-run vs live — that's how main.py uses it for
    the operator-visible 'contracts with changes' line."""
    results = [
        WriteResult("g1", "Changed Dry", (FieldDelta("Spent so far", 0.0, 1.0),), True),
        WriteResult("g2", "Changed Live", (FieldDelta("% Spent", 0.0, 50.0),), False),
        WriteResult("g3", "No Change", (), False, skipped_reason="no_change"),
        WriteResult("g4", "Filtered", (), False, skipped_reason="test_contract_filter"),
        WriteResult("g5", "Errored", (FieldDelta("Alarms", "Clear", "ALARM"),),
                    False, error="boom"),
    ]
    s = summarize(results, dry_run=False)
    assert s.contracts_evaluated == 5
    assert s.contracts_changed == 2   # g1 (dry, would write) + g2 (live, did write)
    assert s.contracts_no_change == 1
    assert s.contracts_filtered == 1
    assert s.contracts_errored == 1
    # fields_would_write counts dry-run results' deltas (g1: 1 field).
    assert s.fields_would_write == 1
    # fields_written counts live, no-error, non-skipped (g2: 1 field).
    # g5 also had a delta but errored, so excluded.
    assert s.fields_written == 1
