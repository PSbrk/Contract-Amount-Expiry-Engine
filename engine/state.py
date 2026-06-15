"""Step 6: change detection against prior State.

The State table holds, per contract, the values the engine wrote on its
last successful run (Spent so far, % Spent, Spending Rate, Spending Rate
Alarm, Alarms) plus the file hash that fed those values and a timestamp.

On each run, after compute_dashboard produces the new DashboardRows, this
module diffs them against the prior State entries and surfaces noteworthy
changes into the Run Log's review_flags column for an operator to eyeball.

Spec §10:
  Each run, recompute every contract's net total and diff against State.
  Tableau is authoritative; corrections appear as a credit on one account
  + a debit on another — do NOT try to pair them. Flag any contract whose
  total changed, especially a decrease or large swing, into a Review list
  (Run Log) for a human to eyeball. Never auto-suppress.

Diff categories the engine emits (CAN co-exist; one contract may carry
several findings in a single run — spec "never auto-suppress"):

- `first_run` — no prior State for this contract. Tracked in counts but
  NOT rendered into review_flags (would flood the Run Log on Day 1).
- `decrease` — Spent so far fell by more than the float-noise floor.
  Always flagged regardless of magnitude because in a net-signed sum a
  decrease indicates a Tableau correction credit hit.
- `large_swing` — |new - prior| >= REVIEW_LARGE_DELTA_DOLLARS. Co-emits
  with `decrease` when the drop is large (operator scanning Large Swings
  for a downstream report shouldn't miss the big drops).
- `alarm_transition` — Alarms flipped (Clear → ALARM or vice versa).
- `band_transition` — Spending Rate Alarm band changed.
- `crossed_100` — % Spent crossed 100% in either direction (inclusive
  on the prior=100.0 boundary).

Deliberate trade-off (spec interpretation): the spec asks to "Flag any
contract whose total changed", but the engine does NOT emit a finding for
a quiet sub-threshold increase. The Dashboard already reflects the new
value, and a finding-per-cent would flood review_flags on every run. The
threshold filters noise; decreases (always flagged) and large swings (>=
$10K) are the actionable cases. If you tune this, update both
REVIEW_LARGE_DELTA_DOLLARS in settings and the docstring on the relevant
build_review_flags section header.

Pure logic — no Airtable / Asana I/O.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from config import settings
from engine.compute import DashboardRow


log = logging.getLogger(__name__)


# Float-noise tolerance for prior-vs-new spent comparison. Mirrors the
# tolerance asana_writer._NUMBER_TOLERANCE uses for the same problem space —
# Airtable round-tripping a number through its JSON representation can shift
# the last bit of a float, producing spurious "decreased from $5,000.00 to
# $5,000.00 (-$0.00)" findings without this guard.
_DECREASE_NOISE_FLOOR: float = 0.005


@dataclass(frozen=True)
class StatePrior:
    """The State table row for one contract — values from the last run.

    asana_task_gid is the stable identity. Contract Name is held as a
    human label; on rename, the engine still finds the prior State via GID
    rather than orphaning the row.

    last_processed_hash is loaded for audit-trail continuity (operator can
    see which Tableau file fed these priors). The diff logic does NOT key
    off it today; persisting it keeps the data available for a future use
    case (e.g. detect that the same file is being reprocessed).
    """
    contract_name: str
    asana_task_gid: str
    prior_spent: float | None
    prior_pct_spent: float | None
    prior_spending_rate: float | None
    prior_spending_rate_alarm: str | None
    prior_alarms: str | None
    last_processed_hash: str | None
    last_updated_at: date | None


@dataclass(frozen=True)
class ChangeFinding:
    """One noteworthy change for one contract."""
    contract_name: str
    category: str          # "first_run" | "decrease" | "large_swing" |
                            #  "alarm_transition" | "band_transition" | "crossed_100"
    detail: str            # operator-facing summary, e.g.
                            #   "Spent decreased from $1500.00 to $1200.00 (-$300.00)"


def _money(v: float) -> str:
    """Render a float as a signed $-amount, e.g. $1,234.56 or -$1,234.56.

    Sign goes BEFORE the dollar sign (standard accounting form) so a
    decrease detail reads "Spent decreased from $1,500.00 to $1,200.00
    (-$300.00)" rather than the ambiguous "$-300.00"."""
    if v < 0:
        return f"-${abs(v):,.2f}"
    return f"${v:,.2f}"


def diff_against_prior(
    dash: DashboardRow,
    prior: StatePrior | None,
) -> list[ChangeFinding]:
    """Return the list of noteworthy findings for one contract's diff.

    `prior=None` means no State row existed for this contract — this is
    the contract's first measured run. We surface that as informational
    so the Run Log shows the first-touch event, but it isn't a Review
    Flag concern.
    """
    findings: list[ChangeFinding] = []

    if prior is None:
        findings.append(ChangeFinding(
            contract_name=dash.contract_name,
            category="first_run",
            detail=(
                f"first compute (no prior State). Spent so far "
                f"{_money(dash.spent_so_far)}; Alarms={dash.alarms}."
            ),
        ))
        return findings

    # --- Spent so far movement ------------------------------------------------
    # decrease and large_swing are INDEPENDENT — a $50k drop emits BOTH so an
    # operator filtering by "Large swings" doesn't miss large-magnitude
    # decreases. Spec §10 "especially a decrease or large swing" reads as a
    # hierarchy of emphasis, not mutual exclusion.
    if prior.prior_spent is not None:
        delta = dash.spent_so_far - prior.prior_spent
        # Decrease branch — flag only when below the float-noise floor.
        # Without the tolerance, an Airtable round-tripped 4999.9999 vs a
        # newly-computed 5000.0 would falsely flag a $0.00 "decrease".
        if delta < -_DECREASE_NOISE_FLOOR:
            findings.append(ChangeFinding(
                contract_name=dash.contract_name,
                category="decrease",
                detail=(
                    f"Spent decreased from {_money(prior.prior_spent)} to "
                    f"{_money(dash.spent_so_far)} ({_money(delta)}). "
                    f"Likely a Tableau correction credit; verify."
                ),
            ))
        # Large-swing branch — independent of sign. abs(delta) ≥ threshold.
        if abs(delta) >= settings.REVIEW_LARGE_DELTA_DOLLARS:
            findings.append(ChangeFinding(
                contract_name=dash.contract_name,
                category="large_swing",
                detail=(
                    f"Spent moved by {_money(delta)} (from "
                    f"{_money(prior.prior_spent)} to {_money(dash.spent_so_far)}). "
                    f"Threshold {_money(settings.REVIEW_LARGE_DELTA_DOLLARS)}."
                ),
            ))

    # --- Alarms binary transition --------------------------------------------
    # We flag both directions — Clear→ALARM is the headline event, ALARM→Clear
    # is the resolution event the operator may also want to see (could indicate
    # a real recovery OR a misfire that needs investigating).
    if prior.prior_alarms is not None and prior.prior_alarms != dash.alarms:
        findings.append(ChangeFinding(
            contract_name=dash.contract_name,
            category="alarm_transition",
            detail=(
                f"Alarms transitioned {prior.prior_alarms!r} -> {dash.alarms!r}."
            ),
        ))

    # --- Band transition -----------------------------------------------------
    # Spending Rate Alarm is the granular detail (75%/90%/100%/Over). A change
    # in band — including None → "75%" or "Over" → "100%" — is worth surfacing
    # so the operator can spot escalation or recovery.
    if prior.prior_spending_rate_alarm != dash.spending_rate_alarm:
        findings.append(ChangeFinding(
            contract_name=dash.contract_name,
            category="band_transition",
            detail=(
                f"Spending Rate Alarm band {prior.prior_spending_rate_alarm!r}"
                f" -> {dash.spending_rate_alarm!r}."
            ),
        ))

    # --- % Spent crossed 100 -------------------------------------------------
    # Tripping over is the headline event; falling back below is unusual
    # (operator likely raised Contract Amount or recorded a large credit).
    # Boundary semantics: prior=100.0 exactly counts as "at 100"; the cross
    # happens when one side is < 100 and the other is >= 100, OR when prior
    # is exactly 100.0 and new moves off it in either direction.
    prior_pct = prior.prior_pct_spent
    new_pct = dash.pct_spent
    if prior_pct is not None and new_pct is not None:
        prior_at_or_below = prior_pct <= 100.0
        prior_at_or_above = prior_pct >= 100.0
        new_above = new_pct > 100.0
        new_below = new_pct < 100.0
        crossed = (
            (prior_at_or_below and new_above)
            or (prior_at_or_above and new_below)
        )
        if crossed:
            findings.append(ChangeFinding(
                contract_name=dash.contract_name,
                category="crossed_100",
                detail=(
                    f"% Spent crossed 100%: {prior_pct:.2f}% -> {new_pct:.2f}%."
                ),
            ))

    return findings


def build_review_flags(findings: list[ChangeFinding]) -> str:
    """Compose the Run Log review_flags text from the per-contract findings.

    Empty input → empty string (operator-friendly: only renders text when
    there's something to flag). Categorized so the operator can read
    decreases / large swings / alarm transitions at a glance.

    `first_run` findings are INTENTIONALLY EXCLUDED from the rendered text:
    on Day 1 the operator gets one finding per live contract, which would
    flood Review Flags. The counts still appear in summarize_findings so
    the Run Log notes line can mention them.
    """
    if not findings:
        return ""

    by_cat: dict[str, list[ChangeFinding]] = {}
    for f in findings:
        if f.category == "first_run":
            continue  # informational, not for the operator-facing flag block
        by_cat.setdefault(f.category, []).append(f)

    if not by_cat:
        return ""

    lines: list[str] = []

    # Order matters — most-actionable first so the operator's eye lands on
    # the rows that need action. Headers expanded with plain-English
    # context so a fresh operator can read the Run Log without a glossary.
    threshold_dollars = f"${settings.REVIEW_LARGE_DELTA_DOLLARS:,.2f}".rstrip("0").rstrip(".")
    section_order = [
        ("decrease",
         "Decreases in Spent so far (likely Tableau correction credits)"),
        ("alarm_transition",
         "Alarms transitions (Clear <-> ALARM)"),
        ("crossed_100",
         "% Spent crossed 100% (over-budget threshold)"),
        ("large_swing",
         f"Large swings in Spent so far (absolute change >= {threshold_dollars})"),
        ("band_transition",
         "Spending Rate Alarm band changes (75%/90%/100%/Over)"),
    ]

    for cat, header in section_order:
        items = by_cat.get(cat) or []
        if not items:
            continue
        lines.append(f"{header} ({len(items)}):")
        for f in items:
            lines.append(f"  - {f.contract_name}: {f.detail}")
        lines.append("")  # blank line between sections

    return "\n".join(lines).rstrip()


def summarize_findings(findings: list[ChangeFinding]) -> dict[str, int]:
    """Counts per category — for the Run Log notes line."""
    counts = {
        "decrease": 0,
        "alarm_transition": 0,
        "crossed_100": 0,
        "large_swing": 0,
        "band_transition": 0,
        "first_run": 0,
    }
    for f in findings:
        if f.category in counts:
            counts[f.category] += 1
    return counts


__all__ = [
    "StatePrior",
    "ChangeFinding",
    "diff_against_prior",
    "build_review_flags",
    "summarize_findings",
]
