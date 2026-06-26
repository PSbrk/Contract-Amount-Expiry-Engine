"""CapEx tier — account 63015 attribution + computation.

The opex tier guesses (fuzzy vendor + term window). CapEx does not: a row coded
to settings.CAPEX_ACCOUNT_NO carries a Tableau `Project ID` that matches an
Asana contract's `CapEx ID` exactly (after normalize_capex_id). So this module
is a deterministic join, not a guess — and it diverges from opex in three ways:

  1. Aggregate by CapEx ID, not by contract. One project's spend is the sum of
     ALL its Tableau rows (every vendor, every invoice).
  2. NO term window. CapEx is cumulative project-to-date; there is no annual
     Target-Start floor (that whole headache belongs to opex only).
  3. One CapEx ID can span MANY Asana contracts (observed: 18+). The single
     project %/band/alarm is BROADCAST identically to every live contract that
     carries the ID, as ordinary DashboardRows flagged is_capex=True. The writer
     then leaves Asana's `Spending Rate` (annual pace) untouched for them.

The budget denominator lives in a Google Doc, not Asana — the operator enters
it once per CapEx ID and it's stored locally. Until a budget exists, a project
computes no % and writes nothing (Needs-Budget queue). Outcomes a CapEx ID can
land in, all surfaced, NOTHING silently dropped:

  - rows               : live contract(s) + known budget → computed + broadcast.
  - needs_budget       : live contract(s) but no budget yet → prompt the operator.
  - spend_no_contract  : Tableau spend but no live Asana contract → parked + shown,
                         NOT prompted (historical / pre-setup; per the 209-ID
                         scoping decision, budget prompts are Asana-driven only).
  - awaiting_capex_id  : 63015 rows with a BLANK Project ID → parked + shown,
                         no %, no alarm, no write (the tolerated-for-now case).

Pure logic — no Asana, no SQLite. Caller supplies contracts + the budget dict.
Band/alarm math is reused from engine.compute so opex and CapEx never drift.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import pandas as pd

from config import settings
from engine.asana_contracts import Contract, normalize_capex_id
from engine.compute import (
    DashboardRow,
    compute_alarm_band,
    compute_alarms,
    compute_pct_spent,
    line_dict,
)


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CapExRun:
    # One DashboardRow per live contract under a budgeted CapEx ID. The
    # spend/%/band/alarms are PROJECT-level, identical across the contracts
    # sharing a CapEx ID; spending_rate is None and is_capex=True so the writer
    # skips Asana's Spending Rate.
    rows: tuple[DashboardRow, ...] = ()
    # (capex_id, project_spend, n_live_contracts) — live contract(s), no budget.
    needs_budget: tuple[tuple[str, float, int], ...] = ()
    # (capex_id, project_spend) — Tableau spend with no live contract. Parked.
    spend_no_contract: tuple[tuple[str, float], ...] = ()
    # 63015 rows with a blank Project ID, aggregated. Parked.
    awaiting_amount: float = 0.0
    awaiting_rows: int = 0

    def summary_dict(self) -> dict[str, object]:
        return {
            "broadcast_contracts": len(self.rows),
            # distinct projects = distinct (spend, budget) signatures across the
            # broadcast rows (contracts sharing a CapEx ID share the signature).
            "distinct_projects_computed":
                len({(r.spent_so_far, r.contract_amount) for r in self.rows}),
            "needs_budget": len(self.needs_budget),
            "spend_no_contract": len(self.spend_no_contract),
            "awaiting_capex_id_rows": self.awaiting_rows,
            "awaiting_capex_id_amount": round(self.awaiting_amount, 2),
        }


def _capex_live(c: Contract) -> bool:
    """Write-gate for a CapEx contract: a real, active contract in the project.

    Deliberately OMITS the opex date checks (future_start / past_due) — CapEx
    has no term window, so a project past its nominal due date or not-yet-started
    still accrues against its budget.
    # ponytail: section-or-status only; add date gating if projects ever need it.
    """
    return (
        c.section_name == settings.ASANA_WRITE_GATE_SECTION
        or c.status == "Active"
    )


def compute_capex(
    in_scope_df: pd.DataFrame,
    contracts: list[Contract],
    budgets: dict[str, float],
    today: date,
) -> CapExRun:
    """Attribute + compute the CapEx (63015) tier.

    in_scope_df must already be ACCOUNTS_IN_SCOPE × DEPTS_IN_SCOPE filtered with
    Amount as a signed float (engine.ingest + engine.filters). budgets maps a
    normalized CapEx ID → dollar budget (operator-entered, from SQLite).
    """
    acc = settings.CAPEX_ACCOUNT_NO
    cap_df = in_scope_df[in_scope_df["Account No"] == acc]
    if len(cap_df) == 0:
        return CapExRun()

    # Normalize Project ID the SAME way Asana CapEx IDs are normalized, or the
    # join silently fragments on stray whitespace / case.
    pid = cap_df["Project ID"].map(normalize_capex_id)
    blank = pid.isna()
    awaiting_amount = float(cap_df.loc[blank, "Amount"].sum())
    awaiting_rows = int(blank.sum())

    # Cumulative spend per project — NO term window.
    dated = cap_df.loc[~blank].copy()
    dated["_cid"] = pid[~blank]
    spend_by_id: dict[str, float] = {
        cid: float(amt)
        for cid, amt in dated.groupby("_cid")["Amount"].sum().items()
    }

    # Live contracts grouped by their CapEx ID (one ID → many contracts).
    live_by_id: dict[str, list[Contract]] = {}
    for c in contracts:
        if c.capex_id and _capex_live(c):
            live_by_id.setdefault(c.capex_id, []).append(c)

    rows: list[DashboardRow] = []
    needs_budget: list[tuple[str, float, int]] = []
    spend_no_contract: list[tuple[str, float]] = []

    for cid in sorted(set(spend_by_id) | set(live_by_id)):
        spend = round(spend_by_id.get(cid, 0.0), 2)
        live = live_by_id.get(cid, [])
        if not live:
            # Spend landed for a project with no live Asana contract → park it
            # (visible, not prompted). Skip zero-net projects (fully reversed).
            if spend:
                spend_no_contract.append((cid, spend))
            continue
        budget = budgets.get(cid)
        if budget is None:
            needs_budget.append((cid, spend, len(live)))
            continue
        pct = compute_pct_spent(spend, budget)
        band = compute_alarm_band(pct)
        # CapEx has no pace → spending_rate=None; compute_alarms then trips
        # purely on the budget band (≥75%), exactly the opex band rule.
        alarms = compute_alarms(pct, None, spend)
        for c in live:
            rows.append(DashboardRow(
                contract_name=c.name,
                asana_task_gid=c.gid,
                campus_set=", ".join(sorted(c.campus_options)),
                contract_amount=budget,          # the project budget = denominator
                spent_so_far=spend,              # full project aggregate, broadcast
                pct_spent=pct,
                spending_rate=None,              # no pace for CapEx
                spending_rate_alarm=band,
                alarms=alarms,
                # CapEx has no term; surface the contract's own dates if present
                # (display only — not used in CapEx compute).
                start=c.target_start or c.due_on or today,
                due=c.due_on,
                status=c.status,
                pm_email=c.pm_email,
                last_updated=today,
                contract_reason_text=c.contract_reason_text,
                is_capex=True,
            ))

    run = CapExRun(
        rows=tuple(rows),
        needs_budget=tuple(sorted(needs_budget, key=lambda t: -t[1])),
        spend_no_contract=tuple(sorted(spend_no_contract, key=lambda t: -t[1])),
        awaiting_amount=awaiting_amount,
        awaiting_rows=awaiting_rows,
    )
    log.info("capex: %s", run.summary_dict())
    return run


def capex_lines(
    in_scope_df: pd.DataFrame,
    contracts: list[Contract],
    today: date,
) -> list[dict]:
    """One line dict per 63015 Tableau row, attributed (broadcast) to EVERY
    live contract sharing its CapEx ID — mirroring compute_capex's join and
    its broadcast. Always in-term (CapEx has no term window). Powers the
    Dashboard drill-down for CapEx contracts.
    """
    acc = settings.CAPEX_ACCOUNT_NO
    cap_df = in_scope_df[in_scope_df["Account No"] == acc]
    if len(cap_df) == 0:
        return []
    pid = cap_df["Project ID"].map(normalize_capex_id)
    dated = cap_df.loc[~pid.isna()].copy()
    dated["_cid"] = pid[~pid.isna()]

    live_by_id: dict[str, list[Contract]] = {}
    for c in contracts:
        if c.capex_id and _capex_live(c):
            live_by_id.setdefault(c.capex_id, []).append(c)

    lines: list[dict] = []
    # ponytail: broadcast duplicates each project row across its N contracts —
    # bounded (63015 rows are few). Switch to a single project-keyed table if
    # the duplication ever matters.
    for _, row in dated.iterrows():
        for c in live_by_id.get(row["_cid"], []):
            lines.append(line_dict(c.gid, row, True, "capex"))
    return lines


__all__ = [
    "CapExRun",
    "compute_capex",
    "capex_lines",
]


# ---------------------------------------------------------------------------
# Runnable self-check — `python -m engine.capex`. Asserts the broadcast,
# the four landing states, and that blanks are parked not dropped.
# ---------------------------------------------------------------------------

def demo() -> None:
    from datetime import date as _d

    def C(gid, name, cid, *, section="Active - Compliant", status="Active"):
        return Contract(
            gid=gid, name=name, campus_options=frozenset(), contract_amount=None,
            target_start=None, due_on=None, status=status, expire_countdown=None,
            pm_email=None, section_name=section, capex_id=normalize_capex_id(cid),
        )

    contracts = [
        C("g1", "Heartland Pavement", " ffe001428 "),   # dirty + lowercase
        C("g2", "Heartland Pavement", "FFE001428"),
        C("g3", "Future Project", "RMD000999"),          # live, no budget
        C("g4", "Pending Co", "FFE000001", section="Pending Onboarding", status="Pending"),
    ]
    budgets = {"FFE001428": 100_000.0}

    df = pd.DataFrame({
        "Account No": ["63015", "63015", "63015", "63015", "63015", "63040"],
        "Project ID": ["FFE001428", "FFE001428", "RMD000999", "", "ZZZ999", "X"],
        "Amount":     [60_000.0,    30_000.0,    5_000.0,     1_234.0, 7_777.0, 99.0],
        "Date":       pd.to_datetime(["2026-01-01"] * 6),
    })

    run = compute_capex(df, contracts, budgets, _d(2026, 6, 24))

    # Broadcast: $90k / $100k = 90% to BOTH g1 and g2, same figures.
    by_gid = {r.asana_task_gid: r for r in run.rows}
    assert set(by_gid) == {"g1", "g2"}, by_gid.keys()
    for g in ("g1", "g2"):
        assert by_gid[g].spent_so_far == 90_000.0
        assert by_gid[g].contract_amount == 100_000.0
        assert by_gid[g].pct_spent == 90.0
        assert by_gid[g].spending_rate is None          # no pace
        assert by_gid[g].spending_rate_alarm == "90%"
        assert by_gid[g].alarms == "ALARM"               # ≥75%
        assert by_gid[g].is_capex is True

    assert run.needs_budget == (("RMD000999", 5_000.0, 1),), run.needs_budget
    assert run.spend_no_contract == (("ZZZ999", 7_777.0),), run.spend_no_contract
    assert run.awaiting_amount == 1_234.0 and run.awaiting_rows == 1
    assert "g4" not in by_gid                            # Pending Onboarding not live

    # capex_lines: the two FFE001428 rows broadcast to BOTH g1 and g2 → 4 lines,
    # all in-term; the RMD000999 row goes to g3 (live, even without a budget).
    lines = capex_lines(df, contracts, _d(2026, 6, 24))
    ffe = [l for l in lines if l["gid"] in ("g1", "g2")]
    assert len(ffe) == 4 and all(l["in_term"] for l in ffe), ffe
    assert all(l["tier"] == "capex" for l in lines)
    assert {l["gid"] for l in lines} == {"g1", "g2", "g3"}, {l["gid"] for l in lines}

    print("capex.demo OK:", run.summary_dict())


if __name__ == "__main__":
    demo()
