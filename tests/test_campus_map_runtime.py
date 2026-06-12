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


def test_default_crosswalk_cen_includes_cen_edm():
    cw = campus_map.build()
    assert cw.lookup("CEN") == frozenset({"CEN", "CEN/EDM"})


def test_default_crosswalk_yvn_maps_to_cen_bundle():
    cw = campus_map.build()
    assert cw.lookup("YVN") == frozenset({"CEN", "CEN/EDM"})


def test_default_crosswalk_omh_includes_nor_override():
    """Spec §5: Asana '***NOR (contract is for OMH)' → Tableau OMH. The
    forward crosswalk must reverse-encode this so a contract tagged with
    ***NOR matches Tableau OMH transactions."""
    cw = campus_map.build()
    assert "***NOR (contract is for OMH)" in cw.lookup("OMH")
    assert "OMH" in cw.lookup("OMH")


def test_default_crosswalk_sba_includes_tul_override():
    cw = campus_map.build()
    assert "***TUL (contract is for SBA)" in cw.lookup("SBA")
    assert "SBA" in cw.lookup("SBA")


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


def test_contract_matches_when_options_intersect_crosswalk():
    cw = campus_map.build()
    # Contract with Asana option "CEN" should match Tableau YVN (which
    # crosswalks to {CEN, CEN/EDM}).
    assert cw.contract_matches_tableau_campus(frozenset({"CEN"}), "YVN")
    # And not match Tableau OMH.
    assert not cw.contract_matches_tableau_campus(frozenset({"CEN"}), "OMH")


def test_contract_matches_via_nor_override():
    """A contract tagged with '***NOR (contract is for OMH)' must match
    Tableau OMH transactions (the override's whole purpose)."""
    cw = campus_map.build()
    contract = frozenset({"***NOR (contract is for OMH)"})
    assert cw.contract_matches_tableau_campus(contract, "OMH")
    assert not cw.contract_matches_tableau_campus(contract, "CEN")


def test_airtable_overrides_replace_config_per_code():
    """Operator overrides on a Tableau code in Airtable Campus Map replace
    the config default for that code; codes not present keep defaults."""
    cw = campus_map.build(
        airtable_overrides={"CEN": frozenset({"CEN", "EDM_NEW"})},
    )
    assert cw.lookup("CEN") == frozenset({"CEN", "EDM_NEW"})
    # Codes not in overrides still get config defaults.
    assert "CEN/EDM" not in cw.lookup("CEN")  # was in the default
    assert "***NOR (contract is for OMH)" in cw.lookup("OMH")  # default kept


def test_airtable_override_preserves_reverse_encoded_starred_options():
    """An operator override for OMH must NOT silently wipe the reverse-encoded
    '***NOR (contract is for OMH)' option — that would break attribution for
    every contract still tagged with the starred name. Same for SBA/***TUL."""
    # Operator added OMH_NEW to the OMH option set but didn't list the
    # starred name. The crosswalk must still include the starred name so
    # contracts tagged with it continue to match OMH transactions.
    cw = campus_map.build(
        airtable_overrides={"OMH": frozenset({"OMH", "OMH_NEW"})},
    )
    options = cw.lookup("OMH")
    assert "OMH" in options
    assert "OMH_NEW" in options  # operator's addition
    assert "***NOR (contract is for OMH)" in options  # reverse-encoded preserved

    # Same hazard for SBA / ***TUL.
    cw_sba = campus_map.build(
        airtable_overrides={"SBA": frozenset({"SBA"})},
    )
    assert "***TUL (contract is for SBA)" in cw_sba.lookup("SBA")


def test_airtable_drop_codes_replace_config_drops_when_provided():
    """When Airtable Campus Map has any Drop=true row, that set REPLACES the
    config drop set entirely. None means 'use config defaults'; empty set
    means 'operator deliberately turned off all drops'."""
    # No Airtable drops provided → config default (INT) applies.
    default_cw = campus_map.build()
    assert default_cw.is_drop_code("INT")

    # Airtable provides an explicit empty set → INT is no longer dropped.
    cw_no_drops = campus_map.build(airtable_drop_codes=frozenset())
    assert not cw_no_drops.is_drop_code("INT")

    # Airtable provides ZZZ as a drop → INT no longer dropped, ZZZ now is.
    cw_new_drops = campus_map.build(airtable_drop_codes=frozenset({"ZZZ"}))
    assert cw_new_drops.is_drop_code("ZZZ")
    assert not cw_new_drops.is_drop_code("INT")


def test_drop_code_never_matches_even_wildcard():
    cw = campus_map.build()
    # All Campuses wildcard, drop code INT → no match.
    contract = frozenset({"All Campuses", "CEN"})
    assert not cw.contract_matches_tableau_campus(contract, "INT")
