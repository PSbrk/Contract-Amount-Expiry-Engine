"""Step 5: write the five Asana custom-field values.

The ONLY module in the codebase that calls Asana's update API. Every call
is gated by:
1. The live gate (compute_dashboard already filtered) — caller passes only
   DashboardRow + Contract pairs for live contracts.
2. The idempotent diff — only fields whose computed value differs from the
   cached current value get written.
3. settings.DRY_RUN_ASANA — when True (default during build), writes are
   logged but never sent.
4. settings.WRITE_TEST_CONTRACT — when set to a task GID, only that one
   contract receives writes; everything else is skipped.

Writes touch ONLY the five spec §0 fields: Spent so far, % Spent, Spending
Rate, Spending Rate Alarm, Alarms. No task name change, no section move,
no other custom field. The Asana SDK's TasksApi.update_task body never
contains any key outside `custom_fields` keyed by the five GIDs.

Spec §0 idempotency: writes happen only when a value actually changed.
That keeps Asana's activity log clean and prevents the operator's
email-on-ALARM automation rule from re-firing on no-op rewrites.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import asana

from config import settings
from engine.asana_contracts import Contract
from engine.compute import DashboardRow


log = logging.getLogger(__name__)


# Number comparison tolerance. Both sides come from round(_, 2) so exact
# equality is the common case; 0.005 absorbs any float-drift fuzz when one
# side is e.g. 75.50000000001 from Asana storage.
_NUMBER_TOLERANCE: float = 0.005


# Field name → custom field GID. Pinned so a single source-of-truth update
# (config/settings.py) cascades here. Order matters for the report.
_WRITABLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("Spent so far", settings.ASANA_FIELD_SPENT_SO_FAR),
    ("% Spent", settings.ASANA_FIELD_PCT_SPENT),
    ("Spending Rate", settings.ASANA_FIELD_SPENDING_RATE),
    ("Spending Rate Alarm", settings.ASANA_FIELD_SPENDING_RATE_ALARM),
    ("Alarms", settings.ASANA_FIELD_ALARMS),
)

_FIELD_NAME_TO_GID: dict[str, str] = dict(_WRITABLE_FIELDS)

# Enum option-name → option-gid lookups, used to translate the human-readable
# band/alarm strings into the GIDs Asana's API expects.
_ENUM_OPTIONS: dict[str, dict[str, str]] = {
    "Spending Rate Alarm": dict(settings.ASANA_SPENDING_RATE_ALARM_OPTIONS),
    "Alarms": dict(settings.ASANA_ALARMS_OPTIONS),
}


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FieldDelta:
    """One field's old → new transition."""
    field_name: str
    old_value: Any
    new_value: Any


def _numbers_differ(a: float | None, b: float | None) -> bool:
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    return abs(a - b) >= _NUMBER_TOLERANCE


def _enums_differ(a: str | None, b: str | None) -> bool:
    return a != b


def diff_dashboard_vs_current(
    dash: DashboardRow,
    contract: Contract,
) -> list[FieldDelta]:
    """Return one FieldDelta per writable field whose computed value differs
    from the cached current Asana value. Empty list = nothing to write."""
    deltas: list[FieldDelta] = []

    if _numbers_differ(contract.current_spent_so_far, dash.spent_so_far):
        deltas.append(FieldDelta("Spent so far",
                                   contract.current_spent_so_far,
                                   dash.spent_so_far))
    if _numbers_differ(contract.current_pct_spent, dash.pct_spent):
        deltas.append(FieldDelta("% Spent",
                                   contract.current_pct_spent,
                                   dash.pct_spent))
    if _numbers_differ(contract.current_spending_rate, dash.spending_rate):
        deltas.append(FieldDelta("Spending Rate",
                                   contract.current_spending_rate,
                                   dash.spending_rate))
    if _enums_differ(contract.current_spending_rate_alarm, dash.spending_rate_alarm):
        deltas.append(FieldDelta("Spending Rate Alarm",
                                   contract.current_spending_rate_alarm,
                                   dash.spending_rate_alarm))
    if _enums_differ(contract.current_alarms, dash.alarms):
        deltas.append(FieldDelta("Alarms",
                                   contract.current_alarms,
                                   dash.alarms))
    return deltas


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------

def build_custom_fields_payload(deltas: list[FieldDelta]) -> dict[str, Any]:
    """Translate the list of deltas into Asana's custom_fields dict shape.

    Keys are custom field GIDs (NOT names). Number fields carry the raw
    number; enum fields carry the option GID (NOT the option name). None
    is preserved as null so a field can be cleared (e.g. Spending Rate
    blanked by the pace guard).
    """
    payload: dict[str, Any] = {}
    for d in deltas:
        gid = _FIELD_NAME_TO_GID[d.field_name]
        if d.field_name in _ENUM_OPTIONS:
            if d.new_value is None:
                payload[gid] = None
            else:
                option_lookup = _ENUM_OPTIONS[d.field_name]
                option_gid = option_lookup.get(d.new_value)
                if option_gid is None:
                    raise ValueError(
                        f"{d.field_name}: option {d.new_value!r} is not in "
                        f"the known option list {sorted(option_lookup)}"
                    )
                payload[gid] = option_gid
        else:
            payload[gid] = d.new_value
    return payload


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WriteResult:
    """What happened for one contract."""
    contract_gid: str
    contract_name: str
    deltas: tuple[FieldDelta, ...]
    dry_run: bool
    skipped_reason: str | None = None    # e.g. "test_contract_filter", "no_change"
    error: str | None = None


def apply_writes(
    api_client: asana.ApiClient | None,
    dash: DashboardRow,
    contract: Contract,
    *,
    dry_run: bool,
    test_contract_gid: str | None = None,
) -> WriteResult:
    """Compute the diff and (if non-empty AND not dry-run AND scope allows)
    write the changed fields to the Asana task.

    api_client may be None in dry-run mode — the writer never touches it
    when dry_run=True, by construction.

    test_contract_gid, when non-empty, restricts writes to that one task
    GID. Other contracts are reported as skipped with reason
    'test_contract_filter' (no diff or write performed for them).
    """
    # Scope filter — applied BEFORE diff. A non-test contract is skipped
    # entirely; we don't even bother computing the diff so the operator's
    # test-contract dry-run output stays focused on the one task.
    if test_contract_gid and contract.gid != test_contract_gid:
        return WriteResult(
            contract_gid=contract.gid,
            contract_name=contract.name,
            deltas=(),
            dry_run=dry_run,
            skipped_reason="test_contract_filter",
        )

    deltas = diff_dashboard_vs_current(dash, contract)
    if not deltas:
        return WriteResult(
            contract_gid=contract.gid,
            contract_name=contract.name,
            deltas=(),
            dry_run=dry_run,
            skipped_reason="no_change",
        )

    if dry_run:
        log.info(
            "[DRY RUN] would write to %s (%s): %s",
            contract.gid, contract.name,
            ", ".join(f"{d.field_name} {d.old_value!r}->{d.new_value!r}"
                      for d in deltas),
        )
        return WriteResult(
            contract_gid=contract.gid,
            contract_name=contract.name,
            deltas=tuple(deltas),
            dry_run=True,
        )

    if api_client is None:
        raise RuntimeError(
            "apply_writes called with dry_run=False but no api_client"
        )

    # REAL WRITE.
    payload = build_custom_fields_payload(deltas)
    body = {"data": {"custom_fields": payload}}
    opts = {"opt_fields": "gid"}
    tasks_api = asana.TasksApi(api_client)
    try:
        tasks_api.update_task(body, contract.gid, opts)
    except Exception as exc:  # noqa: BLE001 — asana raises ApiException + its subclasses
        log.exception("Asana update_task failed for %s (%s)",
                       contract.gid, contract.name)
        return WriteResult(
            contract_gid=contract.gid,
            contract_name=contract.name,
            deltas=tuple(deltas),
            dry_run=False,
            error=f"{type(exc).__name__}: {exc}",
        )

    log.info(
        "wrote %d field(s) to %s (%s): %s",
        len(deltas), contract.gid, contract.name,
        ", ".join(d.field_name for d in deltas),
    )
    return WriteResult(
        contract_gid=contract.gid,
        contract_name=contract.name,
        deltas=tuple(deltas),
        dry_run=False,
    )


# ---------------------------------------------------------------------------
# Roll-up summary
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WriteRunSummary:
    """Aggregate of a Step 5 pass over many contracts."""
    contracts_evaluated: int           # total processed (any outcome)
    contracts_changed: int             # at least one field differed
    contracts_no_change: int           # diff empty
    contracts_filtered: int            # test_contract_filter
    contracts_errored: int             # API error during write
    fields_written: int                # sum of len(deltas) across non-dry, no-error
    fields_would_write: int            # sum of len(deltas) across dry_run
    dry_run: bool


def summarize(results: list[WriteResult], *, dry_run: bool) -> WriteRunSummary:
    changed = sum(1 for r in results if r.deltas and not r.skipped_reason and not r.error)
    no_change = sum(1 for r in results if r.skipped_reason == "no_change")
    filtered = sum(1 for r in results if r.skipped_reason == "test_contract_filter")
    errored = sum(1 for r in results if r.error)
    fields_written = sum(
        len(r.deltas) for r in results if not r.dry_run and not r.error and not r.skipped_reason
    )
    fields_would = sum(
        len(r.deltas) for r in results if r.dry_run and not r.skipped_reason
    )
    return WriteRunSummary(
        contracts_evaluated=len(results),
        contracts_changed=changed,
        contracts_no_change=no_change,
        contracts_filtered=filtered,
        contracts_errored=errored,
        fields_written=fields_written,
        fields_would_write=fields_would,
        dry_run=dry_run,
    )


__all__ = [
    "FieldDelta",
    "WriteResult",
    "WriteRunSummary",
    "diff_dashboard_vs_current",
    "build_custom_fields_payload",
    "apply_writes",
    "summarize",
]
