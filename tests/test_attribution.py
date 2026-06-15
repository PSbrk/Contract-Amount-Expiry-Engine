"""Tests for engine.attribution — the spec §6 algorithm.

Each test builds the minimal synthetic DataFrame + contract list needed to
exercise one branch (auto / learned / ambiguous / unmatched / dropped).
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from engine import campus_map
from engine.asana_contracts import Contract
from engine.attribution import (
    AttributionResult,
    DEFAULT_FUZZY_THRESHOLD,
    attribute,
)


def _contract(name: str, campus_options: frozenset[str], gid: str | None = None) -> Contract:
    return Contract(
        gid=gid or f"gid:{name}",
        name=name,
        campus_options=campus_options,
        contract_amount=10000.0,
        target_start=date(2026, 1, 1),
        due_on=date(2026, 12, 31),
        status="Active",
        expire_countdown=None,
        pm_email=None,
        section_name="Active - Compliant",
    )


def _df(*rows: dict) -> pd.DataFrame:
    """Build a small in-scope-style DataFrame from kwarg dicts.

    Auto-injects a Date column when individual rows omit it; the attribution
    layer's date-bounds aggregation requires it, but the older tests don't
    care about dates so they just get a static default. Per-row Date keys
    in the input dict still win (kwarg-override semantics)."""
    rows = [{"Date": pd.Timestamp("2025-06-01"), **r} for r in rows]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Auto attribution — exactly one candidate
# ---------------------------------------------------------------------------

def test_auto_single_candidate_via_exact_vendor_and_campus():
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme SaaS", "Record Description": "subscription", "Amount": 1000.0},
    )
    contracts = [_contract("Acme SaaS", frozenset({"CEN", "OMH"}))]
    run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(), learned_mappings={})
    assert len(run.auto) == 1
    assert run.auto[0].contract_name == "Acme SaaS"
    assert run.auto[0].rows == 1
    assert run.auto[0].amount == pytest.approx(1000.0)


def test_auto_groups_aggregate_multiple_transactions_for_same_key():
    """The (Campus, Dept, Account No, Vendor) groupby must collapse rows
    that share the key. amount is the signed sum across the group."""
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme SaaS", "Record Description": "a", "Amount": 1000.0},
        {"Record No": "R2", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme SaaS", "Record Description": "b", "Amount": 2000.0},
        {"Record No": "R3", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme SaaS", "Record Description": "c", "Amount": -500.0},
    )
    contracts = [_contract("Acme SaaS", frozenset({"CEN"}))]
    run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(), learned_mappings={})
    assert len(run.results) == 1
    auto = run.auto[0]
    assert auto.rows == 3
    assert auto.amount == pytest.approx(2500.0)


# ---------------------------------------------------------------------------
# Auto via vendor alias
# ---------------------------------------------------------------------------

def test_auto_match_via_vendor_alias():
    """Even if Tableau Vendor doesn't fuzzy-match the contract name, an
    operator-curated alias should bring them together."""
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "ACMEXYZ CO", "Record Description": "x", "Amount": 100.0},
    )
    contracts = [_contract("Acme SaaS", frozenset({"CEN"}))]
    aliases = {"Acme SaaS": ["ACMEXYZ CO"]}
    run = attribute(df, contracts, aliases=aliases, crosswalk=campus_map.build(),
                    learned_mappings={})
    assert len(run.auto) == 1
    assert run.auto[0].contract_name == "Acme SaaS"


# ---------------------------------------------------------------------------
# Learned Mappings precedence
# ---------------------------------------------------------------------------

def test_learned_mapping_takes_precedence_over_fuzzy_match():
    """If a Learned Mapping exists for this group key, attribution uses it
    without checking fuzzy matches — operator's prior answer is sticky."""
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Beta Tools", "Record Description": "x", "Amount": 500.0},
    )
    # Fuzzy match would find Beta Tools; operator already said "Gamma Inc".
    contracts = [
        _contract("Beta Tools", frozenset({"CEN"})),
        _contract("Gamma Inc", frozenset({"CEN"})),
    ]
    learned = {("CEN", "000", "63015", "Beta Tools"): "Gamma Inc"}
    run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(),
                    learned_mappings=learned)
    assert len(run.learned) == 1
    assert run.learned[0].contract_name == "Gamma Inc"
    assert run.learned[0].status == "learned"


# ---------------------------------------------------------------------------
# Ambiguous — multiple candidates after campus narrow
# ---------------------------------------------------------------------------

def test_ambiguous_when_two_contracts_match_both_vendor_and_campus():
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Vendor X", "Record Description": "rent", "Amount": 100.0},
    )
    contracts = [
        _contract("Vendor X", frozenset({"CEN"}), gid="g1"),
        _contract("Vendor X", frozenset({"CEN", "OMH"}), gid="g2"),
    ]
    run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(), learned_mappings={})
    assert len(run.ambiguous) == 1
    result = run.ambiguous[0]
    assert result.status == "ambiguous"
    assert set(result.candidate_names) == {"Vendor X"}  # both contracts named "Vendor X"
    assert result.contract_name is None


# ---------------------------------------------------------------------------
# Unmatched — no candidates at all OR vendor candidates but no campus narrow
# ---------------------------------------------------------------------------

def test_unmatched_when_no_vendor_matches():
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Completely Unknown Vendor Z", "Record Description": "x",
         "Amount": 100.0},
    )
    contracts = [_contract("Beta Tools", frozenset({"CEN"}))]
    run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    assert len(run.unmatched) == 1
    result = run.unmatched[0]
    assert result.contract_name is None
    assert result.candidate_names == ()  # no vendor hints


def test_unmatched_with_vendor_hints_when_campus_doesnt_narrow():
    """Vendor fuzzy-matches a contract, but the contract's campus set
    doesn't cover this transaction's campus. Surface the vendor candidate
    as a hint in candidate_names so the operator sees the engine almost
    got there."""
    df = _df(
        {"Record No": "R1", "Campus": "DEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme SaaS", "Record Description": "x", "Amount": 100.0},
    )
    contracts = [_contract("Acme SaaS", frozenset({"CEN", "OMH"}))]
    run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    assert len(run.unmatched) == 1
    result = run.unmatched[0]
    assert result.contract_name is None
    assert result.candidate_names == ("Acme SaaS",)


# ---------------------------------------------------------------------------
# Dropped — Tableau drop-code (INT)
# ---------------------------------------------------------------------------

def test_dropped_when_campus_is_int():
    """INT rows should be 'dropped', not 'unmatched' — they don't go to
    Needs Tagging."""
    df = _df(
        {"Record No": "R1", "Campus": "INT", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme SaaS", "Record Description": "x", "Amount": 100.0},
    )
    contracts = [_contract("Acme SaaS", frozenset({"CEN"}))]
    run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    assert len(run.dropped) == 1
    assert run.dropped[0].status == "dropped"
    assert run.needs_tagging_groups == []


# ---------------------------------------------------------------------------
# Wildcard contract matches any campus
# ---------------------------------------------------------------------------

def test_wildcard_contract_matches_any_tableau_campus():
    df = _df(
        {"Record No": "R1", "Campus": "DAL", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme SaaS", "Record Description": "x", "Amount": 100.0},
    )
    contracts = [_contract("Acme SaaS", frozenset({"All Campuses"}))]
    run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    assert len(run.auto) == 1


# ---------------------------------------------------------------------------
# Crosswalk-driven matches
# ---------------------------------------------------------------------------

def test_omh_transaction_matches_contract_with_nor_override():
    """A contract with Asana option '***NOR (contract is for OMH)' must
    match Tableau OMH transactions (spec §5 reverse override)."""
    df = _df(
        {"Record No": "R1", "Campus": "OMH", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme SaaS", "Record Description": "x", "Amount": 100.0},
    )
    contracts = [
        _contract("Acme SaaS", frozenset({"***NOR (contract is for OMH)"})),
    ]
    run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    assert len(run.auto) == 1


def test_yvn_transaction_matches_contract_with_cen_edm_option():
    """Tableau YVN crosswalks to {CEN, CEN/EDM}, so a CEN/EDM contract
    matches a YVN transaction."""
    df = _df(
        {"Record No": "R1", "Campus": "YVN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme SaaS", "Record Description": "x", "Amount": 100.0},
    )
    contracts = [_contract("Acme SaaS", frozenset({"CEN/EDM"}))]
    run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    assert len(run.auto) == 1


# ---------------------------------------------------------------------------
# Summary + helpers
# ---------------------------------------------------------------------------

def test_summary_dict_counts_each_branch():
    df = _df(
        # auto
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme SaaS", "Record Description": "a", "Amount": 100.0},
        # dropped (INT)
        {"Record No": "R2", "Campus": "INT", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme SaaS", "Record Description": "b", "Amount": 100.0},
        # unmatched
        {"Record No": "R3", "Campus": "DEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Mystery Vendor 999", "Record Description": "c", "Amount": 100.0},
    )
    contracts = [_contract("Acme SaaS", frozenset({"CEN"}))]
    run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    s = run.summary_dict()
    assert s["auto"] == 1
    assert s["dropped"] == 1
    assert s["unmatched"] == 1
    assert s["total_groups"] == 3


def test_fuzzy_threshold_blocks_loose_matches_by_default():
    """At the default threshold (90), unrelated vendors should not match."""
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Sports Equipment Co", "Record Description": "x", "Amount": 100.0},
    )
    # Different vendor — not a near-string match.
    contracts = [_contract("Tools Manufacturing LLC", frozenset({"CEN"}))]
    run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    assert len(run.unmatched) == 1


def test_empty_vendor_is_unmatched_with_no_candidates():
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "", "Record Description": "x", "Amount": 100.0},
    )
    contracts = [_contract("Acme SaaS", frozenset({"CEN"}))]
    run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    assert len(run.unmatched) == 1
    assert run.unmatched[0].candidate_names == ()


# ---------------------------------------------------------------------------
# Hardening pins (added after Step 3 adversarial review)
# ---------------------------------------------------------------------------

def test_fuzzy_match_is_case_insensitive_via_default_process():
    """Tableau exports often emit vendor names in ALL CAPS while Asana
    contracts are Title Case. Without processor=default_process the WRatio
    of 'VERIZON' vs 'Verizon' is ~14 — well below threshold — and the group
    would land in Needs Tagging instead of auto-attributing."""
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "VERIZON WIRELESS", "Record Description": "x", "Amount": 100.0},
    )
    contracts = [_contract("Verizon Wireless", frozenset({"CEN"}))]
    run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    assert len(run.auto) == 1
    assert run.auto[0].contract_name == "Verizon Wireless"


def test_stale_learned_mapping_falls_through_to_fuzzy_with_warning(caplog):
    """If a Learned Mappings row points to a contract that's been renamed
    or archived in Asana, attribution must NOT silently route spend to a
    non-existent contract. It falls through to fuzzy match instead."""
    import logging
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Beta Tools", "Record Description": "x", "Amount": 100.0},
    )
    contracts = [_contract("Beta Tools", frozenset({"CEN"}))]  # no "DeletedContract"
    learned = {("CEN", "000", "63015", "Beta Tools"): "DeletedContract"}

    with caplog.at_level(logging.WARNING, logger="engine.attribution"):
        run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(),
                        learned_mappings=learned)

    assert len(run.auto) == 1
    assert run.auto[0].contract_name == "Beta Tools"  # found via fuzzy
    assert "stale Learned Mapping" in caplog.text


def test_attribute_handles_pd_na_in_string_columns():
    """If a future ingest path admits pd.NA into string columns (xlsx
    fallback, Tableau REST source), attribute() must coerce safely instead
    of crashing on `pd.NA or ''` or producing literal '<NA>' Group Keys."""
    df = pd.DataFrame({
        "Record No": ["R1", "R2"],
        "Campus": pd.array(["CEN", "CEN"], dtype="string"),
        "Dept": pd.array(["000", "000"], dtype="string"),
        "Account No": pd.array(["63015", "63015"], dtype="string"),
        "Vendor": pd.array([pd.NA, "Acme"], dtype="string"),
        "Record Description": pd.array([pd.NA, "real desc"], dtype="string"),
        "Amount": [100.0, 200.0],
        "Date": [pd.Timestamp("2025-06-01"), pd.Timestamp("2025-06-15")],
    })
    contracts = [_contract("Acme", frozenset({"CEN"}))]
    run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    # Two distinct groups (Vendor differs).
    assert len(run.results) == 2
    # The NA-vendor group has vendor="", not "<NA>".
    na_group = next(r for r in run.results if r.amount == pytest.approx(100.0))
    assert na_group.vendor == ""
    assert "<NA>" not in na_group.group_key
    assert na_group.sample_description == ""


def test_sample_description_picks_first_non_empty_row():
    """When a group's first row has an empty Record Description but later
    rows carry useful text, the operator-facing sample must pick the
    useful one."""
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme", "Record Description": "", "Amount": 100.0},
        {"Record No": "R2", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme", "Record Description": "useful description here",
         "Amount": 200.0},
    )
    contracts = [_contract("Acme", frozenset({"CEN"}))]
    run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    assert len(run.results) == 1
    assert run.results[0].sample_description == "useful description here"


def test_attribution_to_non_live_contract_logs_warning(caplog):
    """Auto-attributing to a contract in a section other than the write-gate
    section (e.g. Pending Onboarding) must surface a warning — Step 5's
    writer will silently skip such contracts, and without the warning an
    operator can't tell why their attribution had no effect."""
    import logging
    pending = Contract(
        gid="g_pend", name="Pending Vendor",
        campus_options=frozenset({"CEN"}),
        contract_amount=10000.0, target_start=date(2026, 1, 1),
        due_on=date(2026, 12, 31),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Pending Onboarding",
    )
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Pending Vendor", "Record Description": "x", "Amount": 100.0},
    )
    with caplog.at_level(logging.WARNING, logger="engine.attribution"):
        run = attribute(df, [pending], aliases={}, crosswalk=campus_map.build(),
                        learned_mappings={})
    assert len(run.auto) == 1
    assert "Pending Onboarding" in caplog.text
    assert "Step 5" in caplog.text
