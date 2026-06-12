"""Per-contract computation — spec §7, §8, §9.

Takes the in-scope DataFrame + AttributionRun + open Contracts + today's date,
returns a list of DashboardRow — one per LIVE contract. Pure logic; no I/O.

Live gate (spec §7):
  (section == "Active - Compliant" OR Contract Status == "Active")
  AND start <= today
  AND today <= due_on (if due_on set)
  AND Expire countdown != "EXPIRED!"

Pending Onboarding / future-start / past-due / expired contracts produce no
row this run — their previous Dashboard row keeps its last values
("freeze last values" per spec). Step 6 will add change-detection on top.

Start fallback: Target Start Date if set, else due_on - 12 months.
Term: due_on - start (calendar days). If due_on missing, term defaults to
365 days from start.

Spent so far — CRITICAL term-window filter: only attributed transactions
dated within [start, min(today, due_on)] count. The Tableau export contains
predecessor-term spend at the same vendor/campus/account; without this
filter a freshly-created contract inherits the prior contract's spend.

% Spent: spent / contract_amount * 100, stored as a percentage number.
Spending Rate: (%spent fraction) / (%time elapsed fraction). Blank when
elapsed < 30 days (pace guard) or contract_amount missing/zero.

Spending Rate Alarm band: <75 blank; 75-89 "75%"; 90-99 "90%"; ==100 (with
small tolerance) "100%"; >100 "Over".

Alarms: ALARM when (%spent >= 75) OR (spending_rate >= RUNAWAY_PACE AND
spent_so_far >= MIN_SPEND_FLOOR). Else Clear.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

import pandas as pd
from dateutil.relativedelta import relativedelta

from config import settings
from engine.asana_contracts import Contract
from engine.attribution import AttributionRun


log = logging.getLogger(__name__)


# ==100 rounding tolerance — a contract that lands at 99.999% should still
# show 100% band; bills landing at 100.001% should show Over. Wide enough
# to absorb float rounding, narrow enough not to swallow real differences.
_ROUNDING_TOL = 0.005

DEFAULT_TERM_DAYS: int = settings.DEFAULT_TERM_MONTHS * 365 // 12  # = 365 for 12 months


@dataclass(frozen=True)
class DashboardRow:
    """One Dashboard table row for one live contract.

    Mirrors the Airtable Dashboard schema. Numeric fields are floats; date
    fields are Python date objects (ISO-serialized on write). None means
    "leave the cell empty" — important for fields like Spending Rate that
    are deliberately blanked by the pace guard.
    """
    contract_name: str
    asana_task_gid: str
    campus_set: str                       # comma-joined option names
    contract_amount: float | None
    spent_so_far: float
    pct_spent: float | None               # percentage number (75.00 == 75%)
    spending_rate: float | None           # pace ratio (1.00 = on pace)
    spending_rate_alarm: str | None       # "75%" / "90%" / "100%" / "Over" / None
    alarms: str                           # "Clear" or "ALARM"
    start: date
    due: date | None
    status: str | None
    pm_email: str | None
    last_updated: date


# ---------------------------------------------------------------------------
# Live gate
# ---------------------------------------------------------------------------

def passes_live_gate(c: Contract, today: date) -> tuple[bool, str | None]:
    """True iff this contract should be computed this run.

    Returns (passes, skip_reason). skip_reason is a short label for logging
    when False ("not_active", "future_start", "past_due", "expired",
    "no_start_data"). Aligns with spec §7's "Pending/not-yet-started → leave
    fields untouched" and "Stop once past due_on or EXPIRED → freeze last
    values".
    """
    section_ok = c.section_name == settings.ASANA_WRITE_GATE_SECTION
    status_ok = c.status == "Active"
    if not (section_ok or status_ok):
        return False, "not_active"

    if c.expire_countdown == "EXPIRED!":
        return False, "expired"

    start = compute_start(c)
    if start is None:
        return False, "no_start_data"
    if start > today:
        return False, "future_start"
    if c.due_on is not None and today > c.due_on:
        return False, "past_due"

    return True, None


# ---------------------------------------------------------------------------
# Start / term math
# ---------------------------------------------------------------------------

def compute_start(c: Contract) -> date | None:
    """Per spec §7: Target Start Date if set, else due_on - 12 months."""
    if c.target_start is not None:
        return c.target_start
    if c.due_on is not None:
        # relativedelta(months=12) is calendar-accurate (1 year of months) —
        # avoids the 365-day approximation diverging across leap years.
        return c.due_on - relativedelta(months=12)
    return None


def compute_term_days(start: date, due_on: date | None) -> int:
    """term = due_on - start (days). Defaults to DEFAULT_TERM_DAYS when due_on
    missing.

    Clamps to at least 1 day to avoid a division-by-zero in pace math when
    an operator has malformed contract dates (start == due_on). Logs a
    warning when start > due_on so the data-quality issue surfaces; the
    live gate independently filters past-due contracts so this branch
    should be unreachable from compute_dashboard, but compute_term_days is
    a public helper.
    """
    if due_on is None:
        return DEFAULT_TERM_DAYS
    days = (due_on - start).days
    if days < 1:
        if days < 0:
            log.warning(
                "compute_term_days: due_on (%s) is before start (%s); "
                "clamping term to 1 day. Investigate this contract.",
                due_on, start,
            )
        return 1
    return days


# ---------------------------------------------------------------------------
# Attribution lookup → per-row contract column
# ---------------------------------------------------------------------------

def annotate_with_contract(
    in_scope_df: pd.DataFrame,
    run: AttributionRun,
) -> pd.DataFrame:
    """Return a copy of in_scope_df with a "_contract_name" column joined
    in from the attribution run. Rows in groups that are ambiguous /
    unmatched / dropped get _contract_name = None.

    Empty input is handled cleanly — a contract list with zero in-scope
    rows is a real production case (brand-new contract before any
    transactions land).
    """
    group_to_contract: dict[tuple[str, str, str, str], str] = {}
    for r in run.results:
        if r.status in ("auto", "learned") and r.contract_name:
            group_to_contract[(r.campus, r.dept, r.account_no, r.vendor)] = r.contract_name

    df = in_scope_df.copy()
    if len(df) == 0:
        df["_contract_name"] = pd.Series([], dtype="object")
        return df
    df["_contract_name"] = [
        group_to_contract.get((c, d, a, v))
        for c, d, a, v in zip(
            df["Campus"], df["Dept"], df["Account No"], df["Vendor"]
        )
    ]
    return df


# ---------------------------------------------------------------------------
# Per-contract spend (term-window)
# ---------------------------------------------------------------------------

def compute_spent_in_term(
    annotated_df: pd.DataFrame,
    contract_name: str,
    start: date,
    end: date,
) -> float:
    """Signed sum of attributed rows for `contract_name` whose Date is in
    [start, end] inclusive. end should be min(today, due_on).

    Empty DataFrame returns 0.0 — a contract with no attributed rows is a
    real production case."""
    if len(annotated_df) == 0:
        return 0.0
    mask = (
        (annotated_df["_contract_name"] == contract_name)
        & (annotated_df["Date"] >= pd.Timestamp(start))
        & (annotated_df["Date"] <= pd.Timestamp(end))
    )
    return float(annotated_df.loc[mask, "Amount"].sum())


# ---------------------------------------------------------------------------
# % spent / pace / alarm
# ---------------------------------------------------------------------------

def compute_pct_spent(spent: float, contract_amount: float | None) -> float | None:
    """Stored as a percentage NUMBER (75.0 == 75%). None when the
    contract amount is missing or zero — we won't divide by it."""
    if contract_amount is None or contract_amount == 0:
        return None
    return round((spent / contract_amount) * 100.0, 2)


def compute_spending_rate(
    pct_spent: float | None,
    start: date,
    today: date,
    term_days: int,
) -> float | None:
    """Pace ratio: %spent fraction / %time elapsed fraction.

    Returns None when:
    - elapsed < PACE_GUARD_DAYS (the 30-day brand-new-contract guard)
    - pct_spent is None
    - elapsed fraction is zero
    """
    elapsed_days = (today - start).days
    if elapsed_days < settings.PACE_GUARD_DAYS:
        return None
    if pct_spent is None:
        return None
    if term_days <= 0:
        return None
    elapsed_fraction = elapsed_days / term_days
    if elapsed_fraction <= 0:
        return None
    return round((pct_spent / 100.0) / elapsed_fraction, 2)


def compute_alarm_band(pct_spent: float | None) -> str | None:
    """Spec §8 budget bands.

    - <75            blank
    - 75 <= x < 90   "75%"
    - 90 <= x < 100  "90%"
    - == 100         "100%" (within _ROUNDING_TOL)
    - > 100          "Over"
    """
    if pct_spent is None:
        return None
    if pct_spent < 75.0:
        return None
    if pct_spent < 90.0:
        return "75%"
    if pct_spent < 100.0 - _ROUNDING_TOL:
        return "90%"
    # Upper bound tightened to float fuzz only — a real 100.01 (which is what
    # the spec's "100.001 → Over" example becomes after the upstream round(2))
    # must land in "Over", not "100%". Symmetric tolerance would have masked
    # this when pct_spent arrived unrounded from a future code path.
    if pct_spent <= 100.0 + 1e-9:
        return "100%"
    return "Over"


def compute_alarms(
    pct_spent: float | None,
    spending_rate: float | None,
    spent_so_far: float,
) -> str:
    """Spec §9: binary roll-up. ALARM if any:
    - %spent >= 75 (any budget band reached)
    - runaway pace: spending_rate >= RUNAWAY_PACE AND spent_so_far >= MIN_SPEND_FLOOR

    The MIN_SPEND_FLOOR guard prevents brand-new / trivial contracts whose
    pace ratio looks high because the denominator (time elapsed) is small
    from tripping the binary alarm.
    """
    if pct_spent is not None and pct_spent >= 75.0:
        return "ALARM"
    if (spending_rate is not None
            and spending_rate >= settings.RUNAWAY_PACE
            and spent_so_far >= settings.MIN_SPEND_FLOOR):
        return "ALARM"
    return "Clear"


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def compute_dashboard(
    in_scope_df: pd.DataFrame,
    run: AttributionRun,
    contracts: Iterable[Contract],
    today: date,
) -> tuple[list[DashboardRow], dict[str, int]]:
    """Compute one DashboardRow per live contract.

    Returns (rows, skip_summary). skip_summary aggregates the reasons
    non-live contracts were skipped, for operator-visible Run Log notes:
        {"not_active": N, "expired": N, "future_start": N, "past_due": N,
         "no_start_data": N}
    """
    annotated = annotate_with_contract(in_scope_df, run)
    rows: list[DashboardRow] = []
    skip_counts: dict[str, int] = {
        "not_active": 0, "expired": 0, "future_start": 0,
        "past_due": 0, "no_start_data": 0,
    }

    for c in contracts:
        passes, reason = passes_live_gate(c, today)
        if not passes:
            if reason in skip_counts:
                skip_counts[reason] += 1
            continue

        start = compute_start(c)
        # passes_live_gate guarantees start is not None here, but keep the
        # narrowing explicit for the type checker / future maintainers.
        assert start is not None

        term_days = compute_term_days(start, c.due_on)
        end = c.due_on if c.due_on is not None and c.due_on < today else today

        spent = compute_spent_in_term(annotated, c.name, start, end)
        pct_spent = compute_pct_spent(spent, c.contract_amount)
        spending_rate = compute_spending_rate(pct_spent, start, today, term_days)
        band = compute_alarm_band(pct_spent)
        alarms = compute_alarms(pct_spent, spending_rate, spent)

        rows.append(DashboardRow(
            contract_name=c.name,
            asana_task_gid=c.gid,
            campus_set=", ".join(sorted(c.campus_options)),
            contract_amount=c.contract_amount,
            spent_so_far=round(spent, 2),
            pct_spent=pct_spent,
            spending_rate=spending_rate,
            spending_rate_alarm=band,
            alarms=alarms,
            start=start,
            due=c.due_on,
            status=c.status,
            pm_email=c.pm_email,
            last_updated=today,
        ))

    log.info(
        "compute_dashboard: %d live row(s); skipped %s",
        len(rows), skip_counts,
    )
    return rows, skip_counts


__all__ = [
    "DashboardRow",
    "passes_live_gate",
    "compute_start",
    "compute_term_days",
    "annotate_with_contract",
    "compute_spent_in_term",
    "compute_pct_spent",
    "compute_spending_rate",
    "compute_alarm_band",
    "compute_alarms",
    "compute_dashboard",
]
