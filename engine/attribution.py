"""Group-based transaction attribution.

The spec §6 algorithm, condensed:
1. Drop transactions whose Campus is a configured drop-code (e.g. INT).
2. Consult Learned Mappings — operator's prior answer wins outright.
3. Vendor fuzzy match (rapidfuzz WRatio + Vendor Aliases) → candidate contracts.
4. Campus narrow via the crosswalk (All Campuses wildcard, identity, ***NOR /
   ***TUL overrides).
5. Exactly-one candidate → auto-attribute.
   Zero candidates after vendor match → "unmatched" → Needs Tagging.
   Zero candidates after campus narrow (had vendor candidates) → "unmatched"
   with the vendor-only candidates surfaced as hints.
   Multiple candidates → "ambiguous" → Needs Tagging with the candidate list.

This module is pure logic — no Asana, no Airtable. The caller fetches contracts /
aliases / crosswalk / learned mappings and passes them in. That keeps the unit
tests fast and the dependency graph one-way.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Iterable

import pandas as pd
from rapidfuzz import fuzz, utils as fuzz_utils

from config import settings
from engine.asana_contracts import Contract
from engine.campus_map import CampusCrosswalk


log = logging.getLogger(__name__)


# Vendor names are operator-curated in Asana, so the fuzz threshold is for
# format variations (case, punctuation, "Inc." vs ", Inc.") — NOT for finding
# semantically-similar vendors. A tight threshold reduces false positives.
#
# 92 is chosen empirically over 90 to filter out WRatio's partial-ratio
# cliff: "Acme" vs "Acme Sports Inc" scores exactly 90 via partial-ratio
# (one is a prefix of the other), which would falsely auto-attribute any
# vendor starting with the contract name. 92 still passes the canonical
# format variations ("Acme, Inc." vs "Acme Inc" scores 94 after
# default_process normalization).
DEFAULT_FUZZY_THRESHOLD: int = 92


@dataclass(frozen=True)
class AttributionResult:
    """Per-group attribution outcome.

    rows + amount are aggregated over every transaction in the group; engines
    downstream of attribution (Step 4 compute) consume these aggregates
    directly without re-grouping the raw DataFrame.
    """
    group_key: str           # "Campus|Dept|Account No|Vendor"
    campus: str
    dept: str
    account_no: str
    vendor: str
    status: str              # "auto" | "learned" | "ambiguous" | "unmatched" | "dropped"
    contract_name: str | None    # populated for auto / learned
    candidate_names: tuple[str, ...]  # narrowed list for ambiguous; vendor-only hints for unmatched
    rows: int
    amount: float
    sample_description: str

    @property
    def needs_tagging(self) -> bool:
        return self.status in ("ambiguous", "unmatched")


@dataclass(frozen=True)
class AttributionRun:
    results: tuple[AttributionResult, ...]

    def by_status(self, status: str) -> list[AttributionResult]:
        return [r for r in self.results if r.status == status]

    @property
    def auto(self) -> list[AttributionResult]: return self.by_status("auto")
    @property
    def learned(self) -> list[AttributionResult]: return self.by_status("learned")
    @property
    def ambiguous(self) -> list[AttributionResult]: return self.by_status("ambiguous")
    @property
    def unmatched(self) -> list[AttributionResult]: return self.by_status("unmatched")
    @property
    def dropped(self) -> list[AttributionResult]: return self.by_status("dropped")

    @property
    def needs_tagging_groups(self) -> list[AttributionResult]:
        return [r for r in self.results if r.needs_tagging]

    def summary_dict(self) -> dict[str, int]:
        return {
            "total_groups": len(self.results),
            "auto": len(self.auto),
            "learned": len(self.learned),
            "ambiguous": len(self.ambiguous),
            "unmatched": len(self.unmatched),
            "dropped": len(self.dropped),
        }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def attribute(
    df: pd.DataFrame,
    contracts: list[Contract],
    aliases: Mapping[str, list[str]],
    crosswalk: CampusCrosswalk,
    learned_mappings: Mapping[tuple[str, str, str, str], str],
    *,
    fuzzy_threshold: int = DEFAULT_FUZZY_THRESHOLD,
) -> AttributionRun:
    """Run attribution against an in-scope DataFrame.

    df must already be in-scope (ACCOUNTS_IN_SCOPE × DEPTS_IN_SCOPE filtered)
    and have Amount as a signed float (Type-normalized).
    """
    # Build the search corpus once: (string, contract) pairs covering contract
    # names AND every alias. Same contract may appear multiple times so we
    # dedup by contract.gid when collecting matches.
    searchable: list[tuple[str, Contract]] = []
    for c in contracts:
        if c.name:
            searchable.append((c.name, c))
        for alias in aliases.get(c.name, []):
            if alias:
                searchable.append((alias, c))

    # Index of valid contract names for stale-Learned-Mapping detection.
    # Two open contracts can share a name (rare data-quality smell in Asana);
    # we use the set of names because Learned Mappings stores by name.
    contract_names = frozenset(c.name for c in contracts if c.name)
    # Plus a name→Contract lookup for surfacing section_name on auto attrs.
    contracts_by_name: dict[str, Contract] = {}
    for c in contracts:
        if c.name:
            contracts_by_name.setdefault(c.name, c)

    # Group by the spec's natural key. The custom sample_description
    # aggregator picks the first NON-EMPTY description so the operator-facing
    # Needs Tagging row doesn't show a blank when later rows had useful text.
    def _first_non_empty(s: pd.Series) -> str:
        for x in s:
            if isinstance(x, str) and x.strip():
                return x
        return ""

    groups = (
        df.groupby(["Campus", "Dept", "Account No", "Vendor"], dropna=False, as_index=False)
        .agg(
            rows=("Record No", "count"),
            amount=("Amount", "sum"),
            sample_description=("Record Description", _first_non_empty),
        )
    )

    def _safe_str(v: object) -> str:
        """Coerce a pandas cell to a clean str; pd.NA and NaN become ''."""
        if v is None or (not isinstance(v, str) and pd.isna(v)):
            return ""
        return str(v)

    results: list[AttributionResult] = []
    for _, g in groups.iterrows():
        results.append(_attribute_group(
            campus=_safe_str(g["Campus"]),
            dept=_safe_str(g["Dept"]),
            account_no=_safe_str(g["Account No"]),
            vendor=_safe_str(g["Vendor"]),
            rows=int(g["rows"]),
            amount=float(g["amount"]),
            sample_description=_safe_str(g["sample_description"]),
            searchable=searchable,
            crosswalk=crosswalk,
            learned_mappings=learned_mappings,
            contract_names=contract_names,
            contracts_by_name=contracts_by_name,
            fuzzy_threshold=fuzzy_threshold,
        ))

    run = AttributionRun(results=tuple(results))
    log.info("attribution: %s", run.summary_dict())
    return run


# ---------------------------------------------------------------------------
# Single-group attribution
# ---------------------------------------------------------------------------

def _attribute_group(
    *,
    campus: str,
    dept: str,
    account_no: str,
    vendor: str,
    rows: int,
    amount: float,
    sample_description: str,
    searchable: list[tuple[str, Contract]],
    crosswalk: CampusCrosswalk,
    learned_mappings: Mapping[tuple[str, str, str, str], str],
    contract_names: frozenset[str],
    contracts_by_name: Mapping[str, Contract],
    fuzzy_threshold: int,
) -> AttributionResult:
    key = (campus, dept, account_no, vendor)
    group_key_str = "|".join(key)

    base = dict(
        group_key=group_key_str,
        campus=campus, dept=dept, account_no=account_no, vendor=vendor,
        rows=rows, amount=amount,
        sample_description=sample_description,
    )

    # 1. Drop-code short-circuit. The row is not a Needs Tagging concern —
    # it's not "unmatched", it's intentionally not matched.
    if crosswalk.is_drop_code(campus):
        return AttributionResult(**base, status="dropped",
                                  contract_name=None, candidate_names=())

    # 2. Learned Mappings take precedence over fuzzy matching. Operator's
    # prior answer is authoritative — but verify the contract still exists
    # in the loaded set, so a stale Learned Mapping for a renamed/archived
    # contract degrades gracefully instead of attributing spend to a ghost.
    if key in learned_mappings:
        cn = learned_mappings[key]
        if cn in contract_names:
            _warn_if_non_live(contracts_by_name.get(cn), group_key_str, source="learned")
            return AttributionResult(**base, status="learned",
                                      contract_name=cn, candidate_names=(cn,))
        log.warning(
            "stale Learned Mapping for group %s -> %r: that contract is no "
            "longer in the open Asana contracts list. Falling through to "
            "fuzzy match. Edit or delete the Learned Mappings row to clean up.",
            group_key_str, cn,
        )

    # 3. Vendor fuzzy match (with aliases). Candidates may be empty.
    vendor_candidates = _match_vendor(vendor, searchable, fuzzy_threshold)

    # 4. Campus-narrow. Wildcard ("All Campuses") matches every code; the
    # crosswalk handles the YVN/CEN, OMH-with-***NOR, etc. routing.
    narrowed = [
        c for c in vendor_candidates
        if crosswalk.contract_matches_tableau_campus(c.campus_options, campus)
    ]

    if len(narrowed) == 1:
        _warn_if_non_live(narrowed[0], group_key_str, source="auto")
        return AttributionResult(**base, status="auto",
                                  contract_name=narrowed[0].name,
                                  candidate_names=(narrowed[0].name,))

    if not narrowed:
        # Surface the vendor-only matches as hints in candidate_names so the
        # operator can see "we know your vendor but no Asana contract covers
        # this campus" vs the "we don't recognize this vendor at all" case.
        return AttributionResult(**base, status="unmatched",
                                  contract_name=None,
                                  candidate_names=tuple(c.name for c in vendor_candidates))

    return AttributionResult(**base, status="ambiguous",
                              contract_name=None,
                              candidate_names=tuple(c.name for c in narrowed))


def _warn_if_non_live(contract: Contract | None, group_key: str, *, source: str) -> None:
    """Log a warning when attribution lands on a non-write-gate contract.

    The Step 5 writer will skip these (Pending Onboarding contracts are
    excluded from writes per spec section 7). Without this signal an
    operator inspecting the attribution summary wouldn't know that the
    auto-attribution to a Pending Onboarding contract has no downstream
    effect.
    """
    if contract is None or contract.section_name is None:
        return
    if contract.section_name == settings.ASANA_WRITE_GATE_SECTION:
        return
    log.warning(
        "%s attribution lands on a non-write-gate contract: group %s -> %r "
        "in section %r. Step 5 writer will skip this contract until the "
        "section is changed to %r.",
        source, group_key, contract.name, contract.section_name,
        settings.ASANA_WRITE_GATE_SECTION,
    )


def _match_vendor(
    vendor: str,
    searchable: list[tuple[str, Contract]],
    fuzzy_threshold: int,
) -> list[Contract]:
    """Return unique contracts whose name or any alias scores ≥ threshold
    against the Tableau Vendor string.

    Dedup by contract.gid — a contract that matches via its name AND via an
    alias appears once in the result.
    """
    if not vendor or not vendor.strip():
        return []

    matches: dict[str, Contract] = {}
    for name_or_alias, contract in searchable:
        # processor=default_process normalizes case + strips non-alphanumeric
        # before comparison. Without it, "VERIZON" vs "Verizon" scores ~14
        # (case-sensitive) and Tableau's all-caps GL exports never match
        # title-case Asana contract names.
        score = fuzz.WRatio(vendor, name_or_alias, processor=fuzz_utils.default_process)
        if score >= fuzzy_threshold:
            matches.setdefault(contract.gid, contract)
    return list(matches.values())


__all__ = [
    "DEFAULT_FUZZY_THRESHOLD",
    "AttributionResult",
    "AttributionRun",
    "attribute",
]
