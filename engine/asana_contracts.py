"""Contract dataclass + loader.

Pulls every open task from the Contractor Database project and turns each
task's Asana custom-field payload into a Contract — a stable, typed
representation the rest of the engine consumes. The Asana SDK leaks dict
shapes everywhere; isolating that translation here means Step 3 / Step 4 /
Step 5 logic never has to know about enum_value vs multi_enum_values vs
date_value vs number_value.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from config import settings
from engine import asana_client


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Contract:
    """One open task in the Contractor Database project.

    name is the engine's vendor identity (matched against Tableau `Vendor`).
    campus_options is the Asana Campus multi_enum option names that are
    SELECTED on this task — may include the literal "All Campuses" wildcard
    that matches every Tableau campus.
    section_name is whichever section in the Contractor Database project
    this task belongs to (e.g. "Active - Compliant", "Pending Onboarding").

    current_* fields hold the CURRENT VALUES of the five writable fields,
    snapshotted at load time. Step 5's writer diffs the computed values
    against these and only writes the deltas — that keeps Asana's activity
    log clean and prevents the operator's email-on-ALARM automation rule
    from re-firing on no-op rewrites.
    """
    gid: str
    name: str
    campus_options: frozenset[str]
    contract_amount: float | None
    target_start: date | None
    due_on: date | None
    status: str | None
    expire_countdown: str | None
    pm_email: str | None
    section_name: str | None
    # Operator-authored description of what the contract covers. Used by
    # attribution as a tie-breaker for same-vendor multi-task ambiguity
    # (e.g. landscaping vs snow-removal at the same vendor) — matched
    # against the Tableau row's Record Description. Empty for older tasks
    # that pre-date the field.
    contract_reason_text: str | None = None
    # Coding mirrored from Tableau (added to Asana 2026-06). dept/acc narrow the
    # opex candidate set by (Campus, Dept, Acc); capex_id is the exact key for
    # 63015 contracts (normalized via normalize_capex_id). All optional — rollout
    # is gradual, so an uncoded contract is treated as a wildcard, never excluded.
    dept: str | None = None
    acc: str | None = None
    capex_id: str | None = None
    # Current values of the five writable fields (Step 5 idempotent diff).
    current_spent_so_far: float | None = None
    current_pct_spent: float | None = None
    current_spending_rate: float | None = None
    current_spending_rate_alarm: str | None = None   # option name e.g. "75%"
    current_alarms: str | None = None                # option name "Clear"/"ALARM"


def _parse_iso_date(s: str | None) -> date | None:
    if not s:
        return None
    return date.fromisoformat(s[:10])


def normalize_capex_id(raw: object) -> str | None:
    """Canonical CapEx ID for joining Asana ↔ Tableau. Strip + upper-case.

    Operator-entered Asana CapEx IDs carry stray leading spaces (' FFE001428')
    while Tableau Project IDs are clean ('FFE001428'); both MUST normalize the
    same way or the deterministic CapEx join silently fragments. None when empty.
    """
    if raw is None:
        return None
    s = str(raw).strip().upper()
    return s or None


def _acc_to_str(n: object) -> str | None:
    """Asana stores Acc as a number (63015.0); Tableau Account No is text
    ("63015"). Normalize the Asana side to a plain integer string so the
    (Dept, Acc) narrow compares like-for-like. None when missing."""
    if n is None:
        return None
    try:
        return str(int(round(float(n))))
    except (TypeError, ValueError):
        s = str(n).strip()
        return s or None


def _custom_fields_by_gid(task: dict) -> dict[str, dict]:
    return {cf["gid"]: cf for cf in (task.get("custom_fields") or [])}


def _enum_name(cf: dict | None) -> str | None:
    """Return the selected enum option's name, or None.

    Intentionally does NOT filter by enabled — for the Contract Status and
    Expire countdown fields specifically, the operator may temporarily
    disable an option on the field while leaving it selected on a task; we
    still want the signal. (This differs from _multi_enum_names which DOES
    filter by enabled — multi_enum selections are operator-curated tags,
    and a disabled tag is reasonably treated as retired. Document the
    asymmetry; reconcile if it becomes a problem.)
    """
    if cf is None:
        return None
    ev = cf.get("enum_value")
    return ev["name"] if ev else None


def _multi_enum_names(cf: dict | None) -> frozenset[str]:
    if cf is None:
        return frozenset()
    values = cf.get("multi_enum_values") or []
    # enabled refers to whether the option is currently enabled on the field;
    # selections of a now-disabled option still carry over in multi_enum_values
    # but we ignore them — the operator clearly intended to retire that option.
    return frozenset(o["name"] for o in values if o.get("enabled") and o.get("name"))


def _section_name_for_project(task: dict, project_gid: str) -> str | None:
    """A task can belong to multiple projects; return the section name of the
    membership in OUR project."""
    for m in (task.get("memberships") or []):
        proj = m.get("project") or {}
        if proj.get("gid") == project_gid:
            section = m.get("section") or {}
            return section.get("name")
    return None


def task_to_contract(task: dict, project_gid: str = settings.ASANA_PROJECT_GID) -> Contract:
    """Convert one Asana task dict into a Contract."""
    cf = _custom_fields_by_gid(task)

    contract_amount: float | None = None
    cam = cf.get(settings.ASANA_FIELD_CONTRACT_AMOUNT)
    if cam is not None:
        contract_amount = cam.get("number_value")

    target_start = None
    tsd = cf.get(settings.ASANA_FIELD_TARGET_START)
    if tsd is not None:
        dv = tsd.get("date_value")
        if dv:
            target_start = _parse_iso_date(dv.get("date"))

    pm_email = None
    pm = cf.get(settings.ASANA_FIELD_PM_EMAIL)
    if pm is not None:
        pm_email = pm.get("text_value") or None

    contract_reason_text = None
    crt = cf.get(settings.ASANA_FIELD_CONTRACT_REASON_TEXT)
    if crt is not None:
        contract_reason_text = crt.get("text_value") or None

    dept = None
    d = cf.get(settings.ASANA_FIELD_DEPT)
    if d is not None:
        dept = (d.get("text_value") or "").strip() or None

    acc = None
    a = cf.get(settings.ASANA_FIELD_ACC)
    if a is not None:
        acc = _acc_to_str(a.get("number_value"))

    capex_id = None
    cap = cf.get(settings.ASANA_FIELD_CAPEX_ID)
    if cap is not None:
        capex_id = normalize_capex_id(cap.get("text_value"))

    # Current values of the writable fields — Step 5 diffs against these.
    def _number(field_gid: str) -> float | None:
        f = cf.get(field_gid)
        return f.get("number_value") if f else None

    return Contract(
        gid=task["gid"],
        name=task.get("name") or "",
        campus_options=_multi_enum_names(cf.get(settings.ASANA_FIELD_CAMPUS)),
        contract_amount=contract_amount,
        target_start=target_start,
        due_on=_parse_iso_date(task.get("due_on")),
        status=_enum_name(cf.get(settings.ASANA_FIELD_CONTRACT_STATUS)),
        expire_countdown=_enum_name(cf.get(settings.ASANA_FIELD_EXPIRE_COUNTDOWN)),
        pm_email=pm_email,
        section_name=_section_name_for_project(task, project_gid),
        contract_reason_text=contract_reason_text,
        dept=dept,
        acc=acc,
        capex_id=capex_id,
        current_spent_so_far=_number(settings.ASANA_FIELD_SPENT_SO_FAR),
        current_pct_spent=_number(settings.ASANA_FIELD_PCT_SPENT),
        current_spending_rate=_number(settings.ASANA_FIELD_SPENDING_RATE),
        current_spending_rate_alarm=_enum_name(cf.get(settings.ASANA_FIELD_SPENDING_RATE_ALARM)),
        current_alarms=_enum_name(cf.get(settings.ASANA_FIELD_ALARMS)),
    )


def load_open_contracts(api_client) -> list[Contract]:
    """Fetch every open task in the Contractor Database project as Contracts.

    No section filter — Step 3 wants attribution to surface even Pending
    Onboarding matches (operator review purposes). The live-gate filter
    (Active - Compliant + start <= today) lives in Step 5's writer.
    """
    contracts: list[Contract] = []
    for task in asana_client.iter_open_tasks(api_client):
        contracts.append(task_to_contract(task))
    log.info("loaded %d open contracts from Asana", len(contracts))
    return contracts


__all__ = [
    "Contract",
    "normalize_capex_id",
    "task_to_contract",
    "load_open_contracts",
]
