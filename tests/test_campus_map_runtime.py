"""Tests for engine.campus_map runtime crosswalk.

Pins every special case from spec §5: identity fallback, CEN/YVN bundle,
INT drop, ***NOR/***TUL reverse-overrides, All Campuses wildcard, and the
Airtable-overrides-replace-config semantics.
"""

from __future__ import annotations

import pytest

from engine import campus_map


def test_default_crosswalk_handles_identity():
    """A code with no explicit mapping falls back to identity."""
    cw = campus_map.build()
    assert cw.lookup("DAL") == frozenset({"DAL"})


def test_default_crosswalk_cen_is_identity():
    # Exact-match (2026-06-24): CEN matches only Asana "CEN" — the old
    # CEN→{CEN,CEN/EDM} forward guess is retired.
    cw = campus_map.build()
    assert cw.lookup("CEN") == frozenset({"CEN"})


def test_default_crosswalk_yvn_is_identity_not_cen():
    # YVN is its own campus now — it falls back to identity and does NOT
    # auto-map to CEN/EDM. With no Asana "YVN" option yet, YVN rows go to
    # Needs-Tagging for the operator.
    cw = campus_map.build()
    assert cw.lookup("YVN") == frozenset({"YVN"})
    assert not cw.contract_matches_tableau_campus(frozenset({"CEN"}), "YVN")


def test_nor_tul_blanket_overrides_are_retired():
    """2026-07-02: the ***NOR→OMH / ***TUL→SBA blanket crosswalk overrides are
    GONE (they were exceptions wrongly encoded as always-on rules). OMH/SBA now
    resolve to plain identity; cross-campus routing is a per-case flagged
    Learned Mapping, not a global map entry."""
    cw = campus_map.build()
    assert cw.lookup("OMH") == frozenset({"OMH"})
    assert cw.lookup("SBA") == frozenset({"SBA"})
    assert "***NOR (contract is for OMH)" not in cw.lookup("OMH")
    assert "***TUL (contract is for SBA)" not in cw.lookup("SBA")


def test_default_crosswalk_int_is_drop_code():
    cw = campus_map.build()
    assert cw.is_drop_code("INT") is True
    assert cw.is_drop_code("CEN") is False
    assert cw.lookup("INT") == frozenset()  # empty set, not identity fallback


def test_all_campuses_is_wildcard_for_any_code():
    cw = campus_map.build()
    contract_opts = frozenset({"All Campuses"})
    assert cw.contract_matches_tableau_campus(contract_opts, "CEN")
    assert cw.contract_matches_tableau_campus(contract_opts, "OMH")
    assert cw.contract_matches_tableau_campus(contract_opts, "DAL")  # identity-matched code
    # Even wildcards don't rescue drop codes — INT is drop semantically.
    assert not cw.contract_matches_tableau_campus(contract_opts, "INT")


def test_contract_matches_on_exact_campus():
    cw = campus_map.build()
    # Exact-match: a contract tagged "CEN" matches Tableau CEN, not YVN/OMH.
    assert cw.contract_matches_tableau_campus(frozenset({"CEN"}), "CEN")
    assert not cw.contract_matches_tableau_campus(frozenset({"CEN"}), "YVN")
    assert not cw.contract_matches_tableau_campus(frozenset({"CEN"}), "OMH")
    # Once a contract is coded with the new campus, identity matches it.
    assert cw.contract_matches_tableau_campus(frozenset({"YVN"}), "YVN")


def test_nor_tagged_contract_no_longer_matches_omh():
    """With the blanket override retired, a contract still tagged with the old
    '***NOR (contract is for OMH)' option name matches NOTHING by identity — its
    OMH spend falls to Needs Tagging, where the operator confirms a cross-campus
    exception once (the migration path)."""
    cw = campus_map.build()
    contract = frozenset({"***NOR (contract is for OMH)"})
    assert not cw.contract_matches_tableau_campus(contract, "OMH")
    assert not cw.contract_matches_tableau_campus(contract, "CEN")


def test_forward_overrides_replace_config_per_code():
    """Operator overrides on a Tableau code in the Campus Map table replace
    the config default for that code; codes not present keep defaults."""
    cw = campus_map.build(
        forward_overrides={"CEN": frozenset({"CEN", "EDM_NEW"})},
    )
    assert cw.lookup("CEN") == frozenset({"CEN", "EDM_NEW"})
    # Codes not in overrides fall back to identity (no reverse-encoded specials
    # left to preserve now that the blanket overrides are retired).
    assert cw.lookup("OMH") == frozenset({"OMH"})


def test_drop_override_replaces_config_drops_when_provided():
    """When the Campus Map table has any Drop=1 row, that set REPLACES the
    config drop set entirely. None means 'use config defaults'; empty set
    means 'operator deliberately turned off all drops'."""
    # No operator drops provided -> config default (INT) applies.
    default_cw = campus_map.build()
    assert default_cw.is_drop_code("INT")

    # Operator provides an explicit empty set -> INT is no longer dropped.
    cw_no_drops = campus_map.build(drop_override=frozenset())
    assert not cw_no_drops.is_drop_code("INT")

    # Operator provides ZZZ as a drop -> INT no longer dropped, ZZZ now is.
    cw_new_drops = campus_map.build(drop_override=frozenset({"ZZZ"}))
    assert cw_new_drops.is_drop_code("ZZZ")
    assert not cw_new_drops.is_drop_code("INT")


def test_drop_code_never_matches_even_wildcard():
    cw = campus_map.build()
    # All Campuses wildcard, drop code INT → no match.
    contract = frozenset({"All Campuses", "CEN"})
    assert not cw.contract_matches_tableau_campus(contract, "INT")
