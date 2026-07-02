"""Automatic per-band alarm re-arm (2026-07-02).

The binary Asana ``Alarms`` field re-fires at each % Spent band so the operator
gets one email per threshold crossing, then self-mutes so the next band is a
fresh edge:

    <75%   -> follow the base binary alarm (Clear, or ALARM on pace runaway);
              high-water cleared so a later climb fires fresh.
    75/90/100 -> on CLIMB into the band: write ALARM (fires the email), then
              after a delay re-arm the field to "Previously Alarmed" so the NEXT
              band is a fresh "Previously Alarmed" -> ALARM edge.
              While sitting in the same (or a lower) already-fired band: stay
              "Previously Alarmed" (muted).
    Over   -> always ALARM, never re-armed (stays loud).

The "email is the tracking record" model: once the ALARM edge fires, Asana's
automation emails the operator; the field can then reset without losing the
record. A dedicated high-water (engine.sqlite_client Alarm Rearm table), NOT
last-run's band, so an oscillation within a band (refund then re-spend) doesn't
re-fire. Manual "Resolved Contracts" is a SEPARATE operator mute and takes
precedence in the writer; this is the default for un-resolved contracts.

Pure logic — no Asana, no DB. The caller supplies the current band, the stored
high-water, and the base binary alarm; it returns what to write and persist.
"""

from __future__ import annotations

from dataclasses import dataclass

ALARM = "ALARM"
CLEAR = "Clear"
PREVIOUSLY_ALARMED = "Previously Alarmed"

# % Spent band severity (mirrors asana_writer.band_severity; kept local so this
# module stays dependency-free and independently testable).
_BAND_SEVERITY: dict[str, int] = {"": 0, "75%": 1, "90%": 2, "100%": 3, "Over": 4}


def _sev(band: str | None) -> int:
    return _BAND_SEVERITY.get((band or "").strip(), 0)


@dataclass(frozen=True)
class RearmPlan:
    """What the writer should do for one contract this run."""
    desired_alarms: str   # value for the main write pass (diffed vs Asana)
    stored_band: str      # new high-water to persist ("" => clear the row)
    fire_reset: bool      # True => after the delay, force-write PREVIOUSLY_ALARMED


def plan_rearm(
    current_band: str | None,
    last_alarmed_band: str | None,
    base_alarms: str,
) -> RearmPlan:
    """Decide the Alarms write + high-water for one contract.

    current_band    : this run's % Spent band ("", "75%", "90%", "100%", "Over").
    last_alarmed_band: stored high-water (highest band already fired), "" if none.
    base_alarms     : compute_alarms() result ("Clear"/"ALARM") — carries the
                      pace-runaway signal, used only below 75% where there is no
                      band to drive the re-arm.
    """
    cur = _sev(current_band)
    last = _sev(last_alarmed_band)

    if cur == 0:
        # Below 75%: no band re-arm. Follow the base binary (pace may still
        # ALARM). Clear the high-water so a later climb fires fresh.
        return RearmPlan(desired_alarms=base_alarms, stored_band="", fire_reset=False)

    if cur == 4:
        # Over budget: always loud, never re-armed.
        return RearmPlan(desired_alarms=ALARM, stored_band="Over", fire_reset=False)

    if cur > last:
        # Climbed into a new, higher band -> fire (email), reset after the delay.
        return RearmPlan(
            desired_alarms=ALARM,
            stored_band=(current_band or "").strip(),
            fire_reset=True,
        )

    # Same or lower band we've already fired at -> stay muted. Keep the
    # high-water at the worst band seen (don't lower it on a dip).
    return RearmPlan(
        desired_alarms=PREVIOUSLY_ALARMED,
        stored_band=(last_alarmed_band or "").strip(),
        fire_reset=False,
    )


__all__ = ["RearmPlan", "plan_rearm", "ALARM", "CLEAR", "PREVIOUSLY_ALARMED"]


def _demo() -> None:
    """Runnable self-check of the band state machine."""
    # Fresh climb 0 -> 75 fires and queues a reset.
    p = plan_rearm("75%", "", "ALARM")
    assert p == RearmPlan(ALARM, "75%", True), p
    # Sitting in the same band stays muted, no reset.
    p = plan_rearm("75%", "75%", "ALARM")
    assert p == RearmPlan(PREVIOUSLY_ALARMED, "75%", False), p
    # Climb to the next band fires again.
    p = plan_rearm("90%", "75%", "ALARM")
    assert p == RearmPlan(ALARM, "90%", True), p
    # Over is always loud, never reset.
    p = plan_rearm("Over", "100%", "ALARM")
    assert p == RearmPlan(ALARM, "Over", False), p
    p = plan_rearm("Over", "Over", "ALARM")
    assert p == RearmPlan(ALARM, "Over", False), p
    # Dip within an already-fired band does not re-fire.
    p = plan_rearm("75%", "90%", "ALARM")
    assert p == RearmPlan(PREVIOUSLY_ALARMED, "90%", False), p
    # Below 75% follows the base alarm and clears the high-water.
    assert plan_rearm("", "90%", "Clear") == RearmPlan("Clear", "", False)
    assert plan_rearm("", "", "ALARM") == RearmPlan("ALARM", "", False)  # pace
    print("alarm_rearm: all checks pass")


if __name__ == "__main__":
    _demo()
