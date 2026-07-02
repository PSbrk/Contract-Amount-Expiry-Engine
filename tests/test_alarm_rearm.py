"""Automatic per-band alarm re-arm state machine (2026-07-02).

Pins the transitions: fire on band climb, mute within a band, Over stays loud,
below-75% follows the base alarm and clears the high-water.
"""

from __future__ import annotations

from engine.alarm_rearm import (
    ALARM,
    PREVIOUSLY_ALARMED,
    RearmPlan,
    plan_rearm,
)


def test_fresh_climb_to_75_fires_and_queues_reset():
    assert plan_rearm("75%", "", "ALARM") == RearmPlan(ALARM, "75%", True)


def test_sitting_in_same_band_is_muted_no_reset():
    assert plan_rearm("75%", "75%", "ALARM") == RearmPlan(PREVIOUSLY_ALARMED, "75%", False)


def test_each_higher_band_re_fires():
    assert plan_rearm("90%", "75%", "ALARM") == RearmPlan(ALARM, "90%", True)
    assert plan_rearm("100%", "90%", "ALARM") == RearmPlan(ALARM, "100%", True)


def test_over_is_always_loud_and_never_reset():
    # Climbing INTO Over stays ALARM (fires via the diff) and never re-arms.
    assert plan_rearm("Over", "100%", "ALARM") == RearmPlan(ALARM, "Over", False)
    # Already at Over: still ALARM, no reset.
    assert plan_rearm("Over", "Over", "ALARM") == RearmPlan(ALARM, "Over", False)


def test_dip_within_an_already_fired_band_does_not_re_fire():
    # Refund drops 90% -> 75% band but we already alarmed at 90%: stay muted,
    # keep the high-water at 90% (don't lower it).
    assert plan_rearm("75%", "90%", "ALARM") == RearmPlan(PREVIOUSLY_ALARMED, "90%", False)


def test_below_75_follows_base_alarm_and_clears_high_water():
    # Dropped below 75% entirely (budget raised / big refund): clear so a later
    # climb fires fresh.
    assert plan_rearm("", "90%", "Clear") == RearmPlan("Clear", "", False)
    # Pace-runaway alarm below 75% still surfaces via the base alarm.
    assert plan_rearm("", "", "ALARM") == RearmPlan("ALARM", "", False)


def test_first_ever_run_at_over_fires_once_stays():
    # A brand-new contract already Over on its first evaluation.
    assert plan_rearm("Over", "", "ALARM") == RearmPlan(ALARM, "Over", False)
