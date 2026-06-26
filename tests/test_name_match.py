"""Distinctive-token matcher for spotting blank-vendor / parked spend."""
from engine.name_match import (
    distinctive_tokens,
    name_in_description,
    match_unlinked,
)


def test_distinctive_tokens_drops_generic_words():
    # "P&E Building Services" -> punctuation gone, 'building'/'services' generic.
    assert distinctive_tokens("P&E Building Services, LLC") == set()
    assert distinctive_tokens("Andrews Electric") == {"andrews"}       # 'electric' generic
    assert distinctive_tokens("Rigdon Floor Coverings, Inc.") == {"rigdon", "floor", "coverings"}
    assert "today" in distinctive_tokens("Empire Today LLC")


def test_name_in_description_requires_all_tokens_in_one_description():
    empire = distinctive_tokens("Empire Today LLC")        # {empire, today}
    assert name_in_description(empire, "Deposit for HNV office carpet, EMPIRE TODAY HS, 01/2026")
    # 'today' alone in a different sentence must NOT satisfy the whole name.
    assert not name_in_description(empire, "today we installed carpet")


def test_generic_only_name_never_matches():
    pe = distinctive_tokens("P&E Building Services")        # empty set
    assert not name_in_description(pe, "Building automation work on the new building")


def test_single_distinctive_token_needs_length():
    assert name_in_description({"andrews"}, "Bill - Andrews Electric: wall remodel")
    assert not name_in_description({"abc"}, "abc widget")   # too short to match alone


def test_match_unlinked_confident_vs_cross_campus_and_multivendor():
    contracts = [
        ("Empire Today LLC", "g_emp", {"HNV"}),
        ("Rigdon Floor Coverings, Inc.", "g_rig", {"OPK"}),   # name matches, wrong campus
        ("Solitude Lake Management", "g_sol", {"HNV"}),
        ("P&E Building Services", "g_pe", {"HNV"}),            # generic-only, never matches
    ]
    descs = [
        "Deposit for HNV office carpet replacement, EMPIRE TODAY HS, Hunter",
        "50% Deposit for Fountain Install, SOLITUDE LAKE, Evraets",
        "carpet via RIGDON FLOOR COVERINGS for a different campus job",
    ]
    confident, cross = match_unlinked(descs, {"HNV"}, contracts)
    conf_gids = {g for _, g in confident}
    cross_gids = {g for _, g in cross}
    assert conf_gids == {"g_emp", "g_sol"}        # named + campus (HNV / multivendor)
    assert cross_gids == {"g_rig"}                # named but campus is OPK, not HNV
    assert "g_pe" not in conf_gids | cross_gids   # generic-only never nominated


def test_match_unlinked_dedupes_by_gid():
    contracts = [("Empire Today LLC", "g_emp", {"HNV"})]
    confident, cross = match_unlinked(
        ["EMPIRE TODAY job one", "EMPIRE TODAY job two"], {"HNV"}, contracts)
    assert [g for _, g in confident] == ["g_emp"]
