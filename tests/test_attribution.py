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
    in the input dict still win (kwarg-override semantics).

    Phase 14a moved the date check into the single-candidate fast paths,
    so the default Date MUST fall inside the default contract term
    (2026-01-01 -> 2026-12-31 from _contract); otherwise every legacy test
    fails ambiguously. Tests that exercise date-narrowing supply their own
    Date per row."""
    rows = [{"Date": pd.Timestamp("2026-06-01"), **r} for r in rows]
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


def test_strip_campus_suffix_unit():
    from engine.attribution import _strip_campus_suffix
    cc = frozenset({"LNX", "OPK", "WWK", "DRB"})
    # campus suffix stripped (all trailing tokens are real campus codes)
    assert _strip_campus_suffix("Corporate Cleaning Group Inc - LNX-OPK-WWK-DRB", cc) == "Corporate Cleaning Group Inc"
    assert _strip_campus_suffix("X - LNX", cc) == "X"
    # NOT stripped — trailing part isn't campus codes
    assert _strip_campus_suffix("Acme Co - Downtown Branch", cc) == "Acme Co - Downtown Branch"
    assert _strip_campus_suffix("Plain Vendor Inc", cc) == "Plain Vendor Inc"
    # no known campus codes → never strips (defensive)
    assert _strip_campus_suffix("Y - LNX", frozenset()) == "Y - LNX"


def test_coding_compatible_multi_value_dept():
    """Asana Dept may list multiple accepted codes ('000, 107'); a Tableau row
    coded to EITHER belongs to the contract. Single-value behaviour unchanged."""
    import dataclasses
    from engine.attribution import _coding_compatible, _dept_set

    assert _dept_set("000, 107") == frozenset({"000", "107"})
    assert _dept_set("000") == frozenset({"000"})
    assert _dept_set("") == frozenset()
    assert _dept_set(None) == frozenset()

    multi = dataclasses.replace(
        _contract("Vendor", frozenset({"CEN"})), dept="000, 107", acc="63080"
    )
    assert _coding_compatible(multi, "000", "63080") is True   # first code
    assert _coding_compatible(multi, "107", "63080") is True   # second code
    assert _coding_compatible(multi, "204", "63080") is False  # neither

    single = dataclasses.replace(
        _contract("Vendor", frozenset({"CEN"})), dept="000", acc="63080"
    )
    assert _coding_compatible(single, "000", "63080") is True
    assert _coding_compatible(single, "107", "63080") is False


def test_campus_suffix_in_vendor_name_still_matches():
    """Tableau bakes the campus list into the vendor name itself
    ('Corporate Cleaning Group Inc - LNX-OPK-WWK-DRB'). The suffix drops WRatio
    to 90 (<92), so without stripping the contract never matches. With the
    strip, the LNX row attributes; the other campuses stay unmatched (no
    contract covers them) — campus exact-match still holds."""
    df = _df(
        {"Record No": "R1", "Campus": "LNX", "Dept": "000", "Account No": "63090",
         "Vendor": "Corporate Cleaning Group Inc - LNX-OPK-WWK-DRB",
         "Record Description": "Janitorial", "Amount": 3130.0},
        {"Record No": "R2", "Campus": "OPK", "Dept": "000", "Account No": "63090",
         "Vendor": "Corporate Cleaning Group Inc - LNX-OPK-WWK-DRB",
         "Record Description": "Janitorial", "Amount": 3130.0},
        {"Record No": "R3", "Campus": "WWK", "Dept": "000", "Account No": "63090",
         "Vendor": "Corporate Cleaning Group Inc - LNX-OPK-WWK-DRB",
         "Record Description": "Janitorial", "Amount": 3130.0},
        {"Record No": "R4", "Campus": "DRB", "Dept": "000", "Account No": "63090",
         "Vendor": "Corporate Cleaning Group Inc - LNX-OPK-WWK-DRB",
         "Record Description": "Janitorial", "Amount": 3130.0},
    )
    contracts = [_contract("Corporate Cleaning Group", frozenset({"LNX"}))]
    run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(), learned_mappings={})
    assert len(run.auto) == 1
    assert run.auto[0].contract_name == "Corporate Cleaning Group"
    assert run.auto[0].campus == "LNX"
    assert run.auto[0].rows == 1
    assert run.auto[0].amount == pytest.approx(3130.0)


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
    learned = {("CEN", "000", "63015", "Beta Tools"): [("Gamma Inc", None, None)]}
    run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(),
                    learned_mappings=learned)
    assert len(run.learned) == 1
    assert run.learned[0].contract_name == "Gamma Inc"
    assert run.learned[0].status == "learned"


def test_learned_mapping_gid_pin_must_respect_campus():
    """A gid-pinned Learned Mapping must NOT attribute across campus. A pin
    keyed to EDM but pointing at a CEN-only contract (the real EDM->CEN
    Oklahoma Chiller leak) is ignored; with no EDM contract for the vendor the
    row falls through to unmatched instead of dumping spend on the CEN task."""
    df = _df(
        {"Record No": "R1", "Campus": "EDM", "Dept": "000", "Account No": "63040",
         "Vendor": "Oklahoma Chiller Corporation", "Record Description": "hvac",
         "Amount": 5000.0},
    )
    cen = _contract("Oklahoma Chiller Corporation", frozenset({"CEN"}), gid="cen1")
    learned = {("EDM", "000", "63040", "Oklahoma Chiller Corporation"):
               [("Oklahoma Chiller Corporation", "cen1", None)]}
    run = attribute(df, [cen], aliases={}, crosswalk=campus_map.build(),
                    learned_mappings=learned)
    assert len(run.learned) == 0            # cross-campus pin NOT honored
    assert run.row_gids == (None,)          # no spend leaked onto the CEN gid
    assert len(run.unmatched) == 1


def test_learned_mapping_gid_pin_honored_when_campus_matches():
    """The same pin, for a row whose campus the contract DOES serve, still
    attributes — the campus guard only blocks cross-campus pins."""
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63040",
         "Vendor": "Oklahoma Chiller Corporation", "Record Description": "hvac",
         "Amount": 5000.0},
    )
    cen = _contract("Oklahoma Chiller Corporation", frozenset({"CEN"}), gid="cen1")
    learned = {("CEN", "000", "63040", "Oklahoma Chiller Corporation"):
               [("Oklahoma Chiller Corporation", "cen1", None)]}
    run = attribute(df, [cen], aliases={}, crosswalk=campus_map.build(),
                    learned_mappings=learned)
    assert len(run.learned) == 1
    assert run.learned[0].contract_gid == "cen1"


# ---------------------------------------------------------------------------
# Cross-campus EXCEPTIONS — the flagged, operator-confirmed override
# ---------------------------------------------------------------------------

def _xc_learned(campus, vendor, name, gid):
    """A flagged (Cross-Campus Exception) gid-pinned Learned Mapping."""
    return {(campus, "000", "63040", vendor):
            [(name, gid, None, True)]}


def test_flagged_cross_campus_exception_is_honored():
    """A WAR-coded row billed to a CEN contract: with the Cross-Campus Exception
    flag set AND no WAR contract for the vendor, the pin attributes across
    campus (the deliberate exception the operator confirmed)."""
    df = _df(
        {"Record No": "R1", "Campus": "WAR", "Dept": "000", "Account No": "63040",
         "Vendor": "DH Pace", "Record Description": "doors", "Amount": 5000.0},
    )
    cen = _contract("DH Pace", frozenset({"CEN"}), gid="cen1")
    learned = _xc_learned("WAR", "DH Pace", "DH Pace", "cen1")
    run = attribute(df, [cen], aliases={}, crosswalk=campus_map.build(),
                    learned_mappings=learned)
    assert len(run.learned) == 1
    assert run.learned[0].contract_gid == "cen1"
    assert run.row_gids == ("cen1",)


def test_unflagged_cross_campus_pin_still_blocked():
    """The SAME pin without the flag is treated as an accidental leak and
    blocked — the regression guard for the Oklahoma Chiller incident stays."""
    df = _df(
        {"Record No": "R1", "Campus": "WAR", "Dept": "000", "Account No": "63040",
         "Vendor": "DH Pace", "Record Description": "doors", "Amount": 5000.0},
    )
    cen = _contract("DH Pace", frozenset({"CEN"}), gid="cen1")
    learned = {("WAR", "000", "63040", "DH Pace"):
               [("DH Pace", "cen1", None, False)]}
    run = attribute(df, [cen], aliases={}, crosswalk=campus_map.build(),
                    learned_mappings=learned)
    assert len(run.learned) == 0
    assert run.row_gids == (None,)


def test_flagged_exception_re_asks_when_same_campus_home_exists():
    """Play-it-safe rule: even with a flagged cross-campus exception, if the
    row's OWN campus now has a live contract for the vendor, don't silently
    apply the old exception — surface BOTH as ambiguous so the operator
    re-decides."""
    df = _df(
        {"Record No": "R1", "Campus": "WAR", "Dept": "000", "Account No": "63040",
         "Vendor": "DH Pace", "Record Description": "doors", "Amount": 5000.0},
    )
    cen = _contract("DH Pace", frozenset({"CEN"}), gid="cen1")
    war = _contract("DH Pace", frozenset({"WAR"}), gid="war1")  # new same-campus home
    learned = _xc_learned("WAR", "DH Pace", "DH Pace", "cen1")
    run = attribute(df, [cen, war], aliases={}, crosswalk=campus_map.build(),
                    learned_mappings=learned)
    assert len(run.learned) == 0
    assert len(run.ambiguous) == 1
    assert set(run.ambiguous[0].candidate_gids) == {"cen1", "war1"}
    assert run.row_gids == (None,)  # no spend lands until operator re-decides


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
# "All Campuses" wildcard — prefer specific, ask on wildcard-only (2026-07-02)
# ---------------------------------------------------------------------------

def test_wildcard_only_match_is_not_auto_attributed_but_surfaced():
    """A match that exists SOLELY via the All-Campuses wildcard is no longer
    auto-attributed — the operator can't control that Asana coding, so the
    engine asks once (ambiguous → Needs Tagging) rather than silently letting a
    magnet contract swallow the spend."""
    df = _df(
        {"Record No": "R1", "Campus": "DAL", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme SaaS", "Record Description": "x", "Amount": 100.0},
    )
    contracts = [_contract("Acme SaaS", frozenset({"All Campuses"}), gid="wild")]
    run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    assert len(run.auto) == 0
    assert len(run.ambiguous) == 1
    assert run.ambiguous[0].candidate_gids == ("wild",)  # surfaced for confirm
    assert run.row_gids == (None,)                        # nothing lands yet


def test_specific_campus_contract_beats_all_campuses_wildcard():
    """The DH Pace scenario: a campus-specific contract and an All-Campuses
    contract both vendor-match. The specific one wins automatically; the
    wildcard magnet never grabs the spend."""
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63040",
         "Vendor": "DH Pace", "Record Description": "doors", "Amount": 5000.0},
    )
    specific = _contract("DH Pace", frozenset({"CEN"}), gid="cen1")
    wildcard = _contract("DH Pace", frozenset({"All Campuses"}), gid="wild")
    run = attribute(df, [specific, wildcard], aliases={},
                    crosswalk=campus_map.build(), learned_mappings={})
    assert len(run.auto) == 1
    assert run.auto[0].contract_gid == "cen1"
    assert run.row_gids == ("cen1",)


def test_wildcard_learned_mapping_still_attributes_after_confirm():
    """Once the operator confirms a wildcard-only match (a Learned Mapping), it
    attributes directly — the 'ask' happens once, not every ingest."""
    df = _df(
        {"Record No": "R1", "Campus": "DAL", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme SaaS", "Record Description": "x", "Amount": 100.0},
    )
    wildcard = _contract("Acme SaaS", frozenset({"All Campuses"}), gid="wild")
    learned = {("DAL", "000", "63015", "Acme SaaS"):
               [("Acme SaaS", "wild", None, False)]}
    run = attribute(df, [wildcard], aliases={}, crosswalk=campus_map.build(),
                    learned_mappings=learned)
    assert len(run.learned) == 1
    assert run.row_gids == ("wild",)


# ---------------------------------------------------------------------------
# Crosswalk-driven matches
# ---------------------------------------------------------------------------

def test_omh_transaction_no_longer_auto_matches_retired_nor_override():
    """2026-07-02: the ***NOR→OMH blanket override is retired. A contract still
    tagged with the old '***NOR (contract is for OMH)' option name no longer
    auto-matches OMH transactions — the row falls to unmatched (Needs Tagging),
    where the operator confirms a cross-campus exception once."""
    df = _df(
        {"Record No": "R1", "Campus": "OMH", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme SaaS", "Record Description": "x", "Amount": 100.0},
    )
    contracts = [
        _contract("Acme SaaS", frozenset({"***NOR (contract is for OMH)"})),
    ]
    run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    assert len(run.auto) == 0
    assert len(run.unmatched) == 1


def test_yvn_transaction_needs_exact_campus_match():
    """Exact-match (2026-06-24): YVN no longer auto-maps to CEN/EDM. A YVN
    transaction does NOT match a contract tagged only CEN/EDM — it falls to
    unmatched for the operator. It matches once a contract carries YVN."""
    df = _df(
        {"Record No": "R1", "Campus": "YVN", "Dept": "000", "Account No": "63040",
         "Vendor": "Acme SaaS", "Record Description": "x", "Amount": 100.0},
    )
    cen_edm = [_contract("Acme SaaS", frozenset({"CEN/EDM"}))]
    run = attribute(df, cen_edm, aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    assert len(run.auto) == 0
    assert len(run.unmatched) == 1

    yvn = [_contract("Acme SaaS", frozenset({"YVN"}))]
    run2 = attribute(df, yvn, aliases={}, crosswalk=campus_map.build(),
                     learned_mappings={})
    assert len(run2.auto) == 1


# ---------------------------------------------------------------------------
# Opex coding-narrow (2026-06) — Dept/Acc filter on vendor candidates
# ---------------------------------------------------------------------------

def _coded(name, campus_options, *, dept, acc, gid=None):
    return Contract(
        gid=gid or f"gid:{name}:{acc}", name=name, campus_options=campus_options,
        contract_amount=10000.0, target_start=date(2026, 1, 1),
        due_on=date(2026, 12, 31), status="Active", expire_countdown=None,
        pm_email=None, section_name="Active - Compliant", dept=dept, acc=acc,
    )


def test_coding_narrow_excludes_mismatched_account():
    """A vendor that fuzzy-matches two contracts in different accounts
    attributes to the Acc that matches the row; the mismatched one is excluded."""
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63040",
         "Vendor": "Acme SaaS", "Record Description": "x", "Amount": 500.0},
    )
    right = _coded("Acme SaaS", frozenset({"CEN"}), dept="000", acc="63040", gid="g_right")
    wrong = _coded("Acme SaaS", frozenset({"CEN"}), dept="000", acc="63090", gid="g_wrong")
    run = attribute(df, [right, wrong], aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    assert len(run.auto) == 1
    assert run.auto[0].contract_gid == "g_right"


def test_coding_narrow_uncoded_contract_is_wildcard():
    """A contract with no Dept/Acc coded yet (mid-rollout) still matches —
    leniency prevents regressions while the operator codes Asana."""
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63040",
         "Vendor": "Acme SaaS", "Record Description": "x", "Amount": 500.0},
    )
    uncoded = _contract("Acme SaaS", frozenset({"CEN"}))  # dept/acc None
    run = attribute(df, [uncoded], aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    assert len(run.auto) == 1


def test_coding_only_mismatch_is_miscoded():
    """Vendor + campus + term align and ONLY the Dept/Acct coding differs →
    the group is 'miscoded' (routed to the Miscoded? tab), NOT auto-attributed
    and NOT plain unmatched. The campus+term-aligned contract is surfaced as
    the candidate to accept."""
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63040",
         "Vendor": "Acme SaaS", "Record Description": "x", "Amount": 500.0},
    )
    wrong = _coded("Acme SaaS", frozenset({"CEN"}), dept="000", acc="63090")
    run = attribute(df, [wrong], aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    assert len(run.auto) == 0
    assert len(run.unmatched) == 0
    assert len(run.miscoded) == 1
    assert len(run.miscoded[0].candidate_gids) == 1


def test_coding_mismatch_with_campus_mismatch_stays_unmatched():
    """If the only vendor match ALSO differs in campus (not just coding), it is
    NOT a miscoding — no contract covers this campus, so it stays unmatched."""
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63040",
         "Vendor": "Acme SaaS", "Record Description": "x", "Amount": 500.0},
    )
    wrong = _coded("Acme SaaS", frozenset({"OKC"}), dept="000", acc="63090")
    run = attribute(df, [wrong], aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    assert len(run.miscoded) == 0
    assert len(run.unmatched) == 1


def test_capex_account_charge_not_routed_to_miscoded():
    """A charge coded to the CapEx account (63015) whose vendor matches an
    OPEX contract is NOT 'miscoded' — CapEx is the other tier (matched by
    Project ID). It stays unmatched so accepting it could never pull
    CapEx-project dollars into an opex contract."""
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme SaaS", "Record Description": "x", "Amount": 500.0},
    )
    opex = _coded("Acme SaaS", frozenset({"CEN"}), dept="000", acc="63040")
    run = attribute(df, [opex], aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    assert len(run.miscoded) == 0
    assert len(run.unmatched) == 1


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
    learned = {("CEN", "000", "63015", "Beta Tools"): [("DeletedContract", None, None)]}

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
        "Date": [pd.Timestamp("2026-06-01"), pd.Timestamp("2025-06-15")],
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


# ---------------------------------------------------------------------------
# Phase 7: GID-based attribution + date-window narrowing + crossover
# ---------------------------------------------------------------------------

def test_per_row_gid_map_is_populated_for_auto_attribution():
    """The AttributionRun.row_gids dict maps Record No -> contract gid for
    every auto/learned row. This is what compute.annotate_with_contract joins
    on to credit spend to specific Asana tasks (not by name)."""
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme SaaS", "Record Description": "x", "Amount": 100.0},
    )
    contracts = [_contract("Acme SaaS", frozenset({"CEN"}), gid="g_acme")]
    run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    # row_gids is POSITIONAL (one entry per df row, in df order).
    assert run.row_gids == ("g_acme",)
    assert run.auto[0].contract_gid == "g_acme"


def test_same_name_multi_task_disambiguates_by_campus():
    """Southern Botanical-style case: same vendor name, multiple open Asana
    tasks each tagged for a different campus. The campus narrow should pick
    exactly one. Without this, all same-name tasks would tie."""
    contracts = [
        _contract("Southern Botanical Inc", frozenset({"FTW"}), gid="g_ftw"),
        _contract("Southern Botanical Inc", frozenset({"KLR"}), gid="g_klr"),
        _contract("Southern Botanical Inc", frozenset({"MKY"}), gid="g_mky"),
    ]
    df = _df(
        {"Record No": "R1", "Campus": "FTW", "Dept": "000", "Account No": "63080",
         "Vendor": "Southern Botanical Inc", "Record Description": "x",
         "Amount": 1000.0},
        {"Record No": "R2", "Campus": "KLR", "Dept": "000", "Account No": "63080",
         "Vendor": "Southern Botanical Inc", "Record Description": "y",
         "Amount": 2000.0},
        {"Record No": "R3", "Campus": "MKY", "Dept": "000", "Account No": "63080",
         "Vendor": "Southern Botanical Inc", "Record Description": "z",
         "Amount": 3000.0},
    )
    run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    # Each row attributes to its campus's task — no ambiguity.
    assert run.row_gids == ("g_ftw", "g_klr", "g_mky")
    # Three SEPARATE groups (different campus), all auto.
    assert len(run.auto) == 3
    assert len(run.ambiguous) == 0


def test_same_name_same_campus_overlapping_terms_prefers_older():
    """Crossover case: PM renewed a contract before the old one expired.
    Both contracts are active in Asana, both match the same campus + vendor.
    Per spec: spend up the OLD contract first, NEW contract only takes
    over once OLD is past_due. For transactions during the overlap window
    the earlier-started (= older) contract wins."""
    old_contract = Contract(
        gid="g_old", name="Acme Service",
        campus_options=frozenset({"CEN"}),
        contract_amount=10000.0,
        target_start=date(2025, 9, 1), due_on=date(2026, 9, 1),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
    )
    new_contract = Contract(
        gid="g_new", name="Acme Service",
        campus_options=frozenset({"CEN"}),
        contract_amount=10000.0,
        target_start=date(2026, 7, 1), due_on=date(2027, 7, 1),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
    )
    # August 2026 falls in BOTH windows. Old contract wins.
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme Service", "Record Description": "aug bill",
         "Amount": 500.0, "Date": pd.Timestamp("2026-08-15")},
    )
    run = attribute(df, [old_contract, new_contract], aliases={},
                    crosswalk=campus_map.build(), learned_mappings={})
    assert run.row_gids == ("g_old",)


def test_same_name_same_campus_post_old_expiry_attributes_to_new():
    """After the OLD contract is past_due (date narrowing excludes it),
    spend goes to the NEW contract automatically — no operator action
    required."""
    old_contract = Contract(
        gid="g_old", name="Acme Service",
        campus_options=frozenset({"CEN"}),
        contract_amount=10000.0,
        target_start=date(2025, 9, 1), due_on=date(2026, 9, 1),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
    )
    new_contract = Contract(
        gid="g_new", name="Acme Service",
        campus_options=frozenset({"CEN"}),
        contract_amount=10000.0,
        target_start=date(2026, 7, 1), due_on=date(2027, 7, 1),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
    )
    # October 2026 falls AFTER old contract's due_on (2026-09-01).
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme Service", "Record Description": "oct bill",
         "Amount": 500.0, "Date": pd.Timestamp("2026-10-15")},
    )
    run = attribute(df, [old_contract, new_contract], aliases={},
                    crosswalk=campus_map.build(), learned_mappings={})
    assert run.row_gids == ("g_new",)


def test_crossover_group_with_dates_both_sides_splits():
    """A single (campus, vendor, dept, account) group with transactions
    BOTH in the crossover overlap (→ old) and after old expires (→ new)
    is fully attributed — to DIFFERENT contracts per row. Group status is
    'split'."""
    old_contract = Contract(
        gid="g_old", name="Acme Service",
        campus_options=frozenset({"CEN"}),
        contract_amount=10000.0,
        target_start=date(2025, 9, 1), due_on=date(2026, 9, 1),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
    )
    new_contract = Contract(
        gid="g_new", name="Acme Service",
        campus_options=frozenset({"CEN"}),
        contract_amount=10000.0,
        target_start=date(2026, 7, 1), due_on=date(2027, 7, 1),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
    )
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme Service", "Record Description": "aug bill",
         "Amount": 500.0, "Date": pd.Timestamp("2026-08-15")},
        {"Record No": "R2", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme Service", "Record Description": "oct bill",
         "Amount": 750.0, "Date": pd.Timestamp("2026-10-15")},
    )
    run = attribute(df, [old_contract, new_contract], aliases={},
                    crosswalk=campus_map.build(), learned_mappings={})
    assert run.row_gids == ("g_old", "g_new")
    # Group is fully attributed (no ambiguous / unmatched rows) so does not
    # surface to Needs Tagging — but it's not "auto" either, it's "split".
    assert len(run.split) == 1
    split = run.split[0]
    assert split.contract_name is None
    assert split.contract_gid is None
    # Splits tuple records the per-contract breakdown for diagnostics.
    by_gid = {s[0]: s for s in split.splits}
    assert by_gid["g_old"][2] == 1 and by_gid["g_old"][3] == pytest.approx(500.0)
    assert by_gid["g_new"][2] == 1 and by_gid["g_new"][3] == pytest.approx(750.0)
    assert not split.needs_tagging


def test_truly_ambiguous_same_name_same_campus_same_term_surfaces_to_operator():
    """If two open Asana tasks share name + campus + start date, neither the
    date-narrow nor the earliest-start tiebreaker can decide. Status is
    'ambiguous', candidate_gids carries BOTH so the Vendor Conflicts panel
    can show them side-by-side."""
    a = Contract(
        gid="g_a", name="DupVendor",
        campus_options=frozenset({"CEN"}),
        contract_amount=10000.0,
        target_start=date(2026, 1, 1), due_on=date(2026, 12, 31),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
    )
    b = Contract(
        gid="g_b", name="DupVendor",
        campus_options=frozenset({"CEN"}),
        contract_amount=5000.0,
        target_start=date(2026, 1, 1), due_on=date(2026, 12, 31),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
    )
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "DupVendor", "Record Description": "x",
         "Amount": 100.0, "Date": pd.Timestamp("2026-06-01")},
    )
    run = attribute(df, [a, b], aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    assert run.row_gids == (None,)
    assert len(run.ambiguous) == 1
    amb = run.ambiguous[0]
    assert set(amb.candidate_gids) == {"g_a", "g_b"}


def test_pattern_learned_mapping_routes_row_by_description_substring():
    """Operator splits a same-vendor group across two contracts by per-row
    description pattern (Phase 7c). Pattern-bearing LMs are checked first;
    the row whose description CONTAINS the pattern attributes to the
    pattern's gid. Pattern matching is case-insensitive substring."""
    landscaping = _contract("Bear Claw Landscaping", frozenset({"NCS"}), gid="g_lawn")
    snow = _contract("Bear Claw Landscaping Inc", frozenset({"NCS"}), gid="g_snow")
    df = _df(
        {"Record No": "R1", "Campus": "NCS", "Dept": "000", "Account No": "63080",
         "Vendor": "Bear Claw Landscaping, Inc", "Record Description": "Groundskeeping 03/2026",
         "Amount": 7000.0},
        {"Record No": "R2", "Campus": "NCS", "Dept": "000", "Account No": "63080",
         "Vendor": "Bear Claw Landscaping, Inc", "Record Description": "Snow Removal 02/2026",
         "Amount": 4000.0},
    )
    # Two pattern LMs under the SAME key — landscape rows go to g_lawn,
    # snow rows go to g_snow. No plain LM, so unmatched-pattern rows would
    # fall through to fuzzy (and would surface as ambiguous in this test).
    learned = {
        ("NCS", "000", "63080", "Bear Claw Landscaping, Inc"): [
            ("Bear Claw Landscaping", "g_lawn", "Groundskeeping"),
            ("Bear Claw Landscaping Inc", "g_snow", "Snow"),
        ],
    }
    run = attribute(df, [landscaping, snow], aliases={},
                    crosswalk=campus_map.build(), learned_mappings=learned)
    assert run.row_gids == ("g_lawn", "g_snow")


def test_pattern_learned_mapping_longest_pattern_wins_when_multiple_match():
    """If multiple pattern LMs match a row's description, the LONGEST
    pattern wins (more specific)."""
    a = _contract("Contract A", frozenset({"CEN"}), gid="g_a")
    b = _contract("Contract B", frozenset({"CEN"}), gid="g_b")
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "DupVendor", "Record Description": "Snow plowing main lot",
         "Amount": 100.0},
    )
    learned = {
        ("CEN", "000", "63015", "DupVendor"): [
            ("Contract A", "g_a", "Snow"),                # length 4
            ("Contract B", "g_b", "Snow plowing main"),   # length 17, more specific
        ],
    }
    run = attribute(df, [a, b], aliases={},
                    crosswalk=campus_map.build(), learned_mappings=learned)
    assert run.row_gids == ("g_b",)


def test_pattern_learned_mapping_falls_back_to_plain_when_no_pattern_matches():
    """When a row's description doesn't match any pattern LM, the
    plain (no-pattern) LM acts as the group-level fallback."""
    a = _contract("Lawn Contract", frozenset({"CEN"}), gid="g_lawn")
    b = _contract("Snow Contract", frozenset({"CEN"}), gid="g_snow")
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "MultiVendor", "Record Description": "Generic landscape work",
         "Amount": 100.0},
    )
    learned = {
        ("CEN", "000", "63015", "MultiVendor"): [
            ("Snow Contract", "g_snow", "Snow Removal"),  # doesn't match "Generic..."
            ("Lawn Contract", "g_lawn", None),            # plain fallback
        ],
    }
    run = attribute(df, [a, b], aliases={},
                    crosswalk=campus_map.build(), learned_mappings=learned)
    assert run.row_gids == ("g_lawn",)


def test_distinct_descriptions_aggregated_for_ambiguous_groups():
    """Ambiguous groups carry a per-distinct-description breakdown for
    the Vendor Conflicts UI per-description picker. Sorted by descending
    amount so the highest-impact descriptions appear first."""
    a = _contract("VendorA", frozenset({"CEN"}), gid="g_a")
    b = _contract("VendorA", frozenset({"CEN"}), gid="g_b")
    # Both candidates share name + campus + identical term; engine can't
    # tiebreak even with description narrowing (reasons left empty).
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "VendorA", "Record Description": "Big charge",
         "Amount": 5000.0},
        {"Record No": "R2", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "VendorA", "Record Description": "Big charge",
         "Amount": 3000.0},
        {"Record No": "R3", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "VendorA", "Record Description": "Small thing",
         "Amount": 100.0},
    )
    run = attribute(df, [a, b], aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    assert len(run.ambiguous) == 1
    dd = run.ambiguous[0].distinct_descriptions
    # Two distinct descriptions; "Big charge" comes first by descending amount.
    # Phase 13: tuple shape is (desc, rows, amount, min_date_iso, max_date_iso).
    # All rows use the test's default Date (2025-06-01), so min == max.
    assert len(dd) == 2
    assert dd[0] == ("Big charge", 2, 8000.0, "2026-06-01", "2026-06-01")
    assert dd[1] == ("Small thing", 1, 100.0, "2026-06-01", "2026-06-01")


def test_distinct_descriptions_carry_min_max_date_per_bucket():
    """Phase 13: per-description bucket tracks min/max transaction date so
    the Vendor Conflicts auto-suggest can reject candidates whose term
    doesn't overlap. Two buckets here -- 'Snow' rows span Jan-Feb 2025,
    'Landscaping' rows span Aug 2025, and the engine must NOT collapse the
    date bounds across descriptions."""
    a = _contract("BearClaw", frozenset({"NCS"}), gid="g_a")
    b = _contract("BearClaw", frozenset({"NCS"}), gid="g_b")
    df = pd.DataFrame([
        {"Date": pd.Timestamp("2025-01-15"), "Record No": "R1",
         "Campus": "NCS", "Dept": "000", "Account No": "63015",
         "Vendor": "BearClaw", "Record Description": "Snow/ice management 1/2025",
         "Amount": 1000.0},
        {"Date": pd.Timestamp("2025-02-20"), "Record No": "R2",
         "Campus": "NCS", "Dept": "000", "Account No": "63015",
         "Vendor": "BearClaw", "Record Description": "Snow/ice management 1/2025",
         "Amount": 1500.0},
        {"Date": pd.Timestamp("2025-08-05"), "Record No": "R3",
         "Campus": "NCS", "Dept": "000", "Account No": "63015",
         "Vendor": "BearClaw", "Record Description": "Landscaping monthly",
         "Amount": 800.0},
    ])
    run = attribute(df, [a, b], aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    assert len(run.ambiguous) == 1
    dd = {d[0]: d for d in run.ambiguous[0].distinct_descriptions}
    # Snow bucket has the wider date range (Jan -> Feb).
    assert dd["Snow/ice management 1/2025"] == (
        "Snow/ice management 1/2025", 2, 2500.0, "2025-01-15", "2025-02-20",
    )
    # Landscaping bucket has its own single-day range; NOT contaminated
    # by the snow rows' dates -- proves the aggregator buckets correctly.
    assert dd["Landscaping monthly"] == (
        "Landscaping monthly", 1, 800.0, "2025-08-05", "2025-08-05",
    )


def test_description_narrowing_picks_snow_vs_landscaping():
    """One vendor often holds TWO contracts in different scopes (landscaping
    + snow removal). Same campus, same overlapping term. Tableau row's
    Record Description should pick the right one via fuzzy match against
    each candidate's Contract Reason Text."""
    snow = Contract(
        gid="g_snow", name="A-Team Landscape",
        campus_options=frozenset({"WPB"}),
        contract_amount=20000.0,
        target_start=date(2025, 11, 1), due_on=date(2026, 4, 30),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
        contract_reason_text="Snow plowing and salting services for the campus parking lots and sidewalks during winter months.",
    )
    landscaping = Contract(
        gid="g_lawn", name="A-Team Landscape",
        campus_options=frozenset({"WPB"}),
        contract_amount=80000.0,
        target_start=date(2025, 7, 19), due_on=date(2026, 7, 19),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
        contract_reason_text="Lawn care, mowing, irrigation, and landscape maintenance year-round.",
    )
    # Two transactions: one looks like snow, one looks like landscape.
    df = _df(
        {"Record No": "R1", "Campus": "WPB", "Dept": "000", "Account No": "63080",
         "Vendor": "A-Team Landscape", "Record Description": "Snow removal 02/2026",
         "Amount": 1500.0, "Date": pd.Timestamp("2026-02-10")},
        {"Record No": "R2", "Campus": "WPB", "Dept": "000", "Account No": "63080",
         "Vendor": "A-Team Landscape", "Record Description": "Groundskeeping 03/2026 - mowing",
         "Amount": 7499.0, "Date": pd.Timestamp("2026-03-01")},
    )
    run = attribute(df, [snow, landscaping], aliases={},
                    crosswalk=campus_map.build(), learned_mappings={})
    assert run.row_gids == ("g_snow", "g_lawn")


def test_description_narrowing_falls_through_when_neither_clearly_wins():
    """If the description matches both candidates' reason texts equally
    (e.g. both contracts overlap in scope), fall through to the
    earliest-start tiebreaker rather than picking arbitrarily."""
    a = Contract(
        gid="g_a", name="GenericVendor",
        campus_options=frozenset({"CEN"}),
        contract_amount=10000.0,
        target_start=date(2025, 6, 1), due_on=date(2026, 6, 1),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
        contract_reason_text="General services for the campus including maintenance and repairs",
    )
    b = Contract(
        gid="g_b", name="GenericVendor",
        campus_options=frozenset({"CEN"}),
        contract_amount=10000.0,
        target_start=date(2025, 9, 1), due_on=date(2026, 9, 1),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
        contract_reason_text="Campus maintenance and general repair services",
    )
    # Description scores roughly equal against both reasons -> no decisive
    # winner -> falls through to earliest-start (g_a, earlier start).
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "GenericVendor", "Record Description": "Maintenance and repairs",
         "Amount": 100.0, "Date": pd.Timestamp("2025-12-01")},
    )
    run = attribute(df, [a, b], aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    assert run.row_gids == ("g_a",)


def test_description_narrowing_skipped_when_reason_text_empty():
    """Older Asana tasks may have no Contract Reason Text set. The engine
    must not penalize them — narrowing falls through to the next step
    (earliest-start tiebreak) rather than declaring them unmatched."""
    a = Contract(
        gid="g_a", name="OldVendor",
        campus_options=frozenset({"CEN"}),
        contract_amount=10000.0,
        target_start=date(2025, 6, 1), due_on=date(2026, 6, 1),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
        contract_reason_text=None,
    )
    b = Contract(
        gid="g_b", name="OldVendor",
        campus_options=frozenset({"CEN"}),
        contract_amount=10000.0,
        target_start=date(2025, 9, 1), due_on=date(2026, 9, 1),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
        contract_reason_text=None,
    )
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "OldVendor", "Record Description": "any description here",
         "Amount": 100.0, "Date": pd.Timestamp("2025-12-01")},
    )
    run = attribute(df, [a, b], aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    # Falls through to earliest-start; g_a (Jun start) wins over g_b (Sep start).
    assert run.row_gids == ("g_a",)


def test_description_narrowing_handles_empty_row_description():
    """A Tableau row with no Record Description can't be matched by scope.
    Engine must fall through to the next narrowing step, not crash."""
    snow = Contract(
        gid="g_snow", name="A-Team Landscape",
        campus_options=frozenset({"WPB"}),
        contract_amount=20000.0,
        target_start=date(2025, 11, 1), due_on=date(2026, 4, 30),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
        contract_reason_text="Snow plowing and salting services.",
    )
    landscaping = Contract(
        gid="g_lawn", name="A-Team Landscape",
        campus_options=frozenset({"WPB"}),
        contract_amount=80000.0,
        target_start=date(2025, 7, 19), due_on=date(2026, 7, 19),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
        contract_reason_text="Lawn care and landscape maintenance.",
    )
    df = _df(
        {"Record No": "R1", "Campus": "WPB", "Dept": "000", "Account No": "63080",
         "Vendor": "A-Team Landscape", "Record Description": "",
         "Amount": 100.0, "Date": pd.Timestamp("2026-02-10")},
    )
    run = attribute(df, [snow, landscaping], aliases={},
                    crosswalk=campus_map.build(), learned_mappings={})
    # No description → can't narrow by scope → falls through. Both windows
    # contain Feb 10; earliest-start picks landscaping (Jul 19 < Nov 1).
    assert run.row_gids == ("g_lawn",)


# ---------------------------------------------------------------------------
# Phase 14a: date check in single-candidate short-circuit paths
# ---------------------------------------------------------------------------

def test_single_campus_candidate_with_out_of_term_date_is_ambiguous():
    """Pre-Phase-14, when only one contract matched by campus, attribution
    short-circuited to that contract WITHOUT checking the row's date. Audit
    showed $722k of pre-contract-start payments got swept into Dashboard
    'Spent so far' through this path. After Phase 14a, the row must surface
    as ambiguous with the single contract as the candidate so the operator
    sees it in Vendor Conflicts marked outside-term."""
    only = Contract(
        gid="g_solo", name="Office Express Janitorial Services",
        campus_options=frozenset({"CEN"}),
        contract_amount=100000.0,
        target_start=date(2025, 11, 9), due_on=date(2026, 11, 9),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
    )
    df = pd.DataFrame([{
        "Date": pd.Timestamp("2025-06-01"),  # 5 MONTHS before contract start
        "Record No": "R_pre", "Campus": "CEN", "Dept": "000", "Account No": "63015",
        "Vendor": "Office Express Janitorial Services",
        "Record Description": "Cleaning April", "Amount": 16000.0,
    }])
    run = attribute(df, [only], aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    # Row is NOT attributed.
    assert run.row_gids == (None,)
    # Group surfaces as ambiguous with the single candidate carried as
    # narrowed_union so the UI can render it in the picker with a marker.
    assert len(run.auto) == 0
    assert len(run.ambiguous) == 1
    amb = run.ambiguous[0]
    assert amb.candidate_gids == ("g_solo",)


def test_single_campus_candidate_with_in_term_date_still_auto():
    """The Phase 14a date check should NOT regress the in-term happy path:
    a row whose date is inside the single candidate's term still attributes
    normally without operator intervention."""
    only = _contract("Acme", frozenset({"CEN"}), gid="g_acme")
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme", "Record Description": "x", "Amount": 100.0},
    )
    # Default Date in _df() is 2026-06-01; _contract's term is 2026-01-01 -> 2026-12-31.
    run = attribute(df, [only], aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    assert run.row_gids == ("g_acme",)
    assert len(run.auto) == 1


def test_pinned_learned_mapping_drops_out_of_term_row():
    """current-term-only: a pinned-gid Learned Mapping does NOT override the
    date check, but an out-of-term row under that pin is treated as PRE-TERM
    spend and DROPPED (excluded), not flagged ambiguous. The operator already
    declared the contract; spend before its term belongs to a prior contract,
    and re-surfacing it as ambiguous every ingest is pure toil."""
    contract = Contract(
        gid="g_pinned", name="Bear Claw Landscaping",
        campus_options=frozenset({"NCS"}),
        contract_amount=50000.0,
        target_start=date(2026, 3, 31), due_on=date(2027, 3, 31),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
    )
    # Operator pinned the Bear Claw Landscaping gid via Vendor Conflicts.
    learned = {
        ("NCS", "000", "63080", "Bear Claw Landscaping, Inc"): [
            ("Bear Claw Landscaping", "g_pinned", None),
        ],
    }
    df = pd.DataFrame([{
        "Date": pd.Timestamp("2026-01-15"),  # BEFORE contract starts 2026-03-31
        "Record No": "R1", "Campus": "NCS", "Dept": "000", "Account No": "63080",
        "Vendor": "Bear Claw Landscaping, Inc",
        "Record Description": "Landscaping 12/2025", "Amount": 8000.0,
    }])
    run = attribute(df, [contract], aliases={}, crosswalk=campus_map.build(),
                    learned_mappings=learned)
    assert run.row_gids == (None,)            # excluded from spend
    assert len(run.learned) == 0
    assert len(run.ambiguous) == 0            # NOT surfaced for review
    assert len(run.dropped) == 1              # cleanly dropped as pre-term


def test_pinned_learned_mapping_keeps_in_term_drops_out_of_term_in_same_group():
    """The whole point of the drop: in-term spend in a pinned group still
    posts while the out-of-term rows are excluded — the out-of-term row no
    longer poisons the group into ambiguous, so the operator's pin actually
    attributes the current-term spend hands-free."""
    contract = Contract(
        gid="g_pinned", name="Bear Claw Landscaping",
        campus_options=frozenset({"NCS"}),
        contract_amount=50000.0,
        target_start=date(2026, 3, 31), due_on=date(2027, 3, 31),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
    )
    learned = {
        ("NCS", "000", "63080", "Bear Claw Landscaping, Inc"): [
            ("Bear Claw Landscaping", "g_pinned", None),
        ],
    }
    df = pd.DataFrame([
        {"Date": pd.Timestamp("2026-06-01"),   # IN term
         "Record No": "R_in", "Campus": "NCS", "Dept": "000", "Account No": "63080",
         "Vendor": "Bear Claw Landscaping, Inc",
         "Record Description": "Landscaping 05/2026", "Amount": 5000.0},
        {"Date": pd.Timestamp("2026-01-15"),   # BEFORE term start 2026-03-31
         "Record No": "R_pre", "Campus": "NCS", "Dept": "000", "Account No": "63080",
         "Vendor": "Bear Claw Landscaping, Inc",
         "Record Description": "Landscaping 12/2025", "Amount": 8000.0},
    ])
    run = attribute(df, [contract], aliases={}, crosswalk=campus_map.build(),
                    learned_mappings=learned)
    # In-term row posts to the pinned gid; out-of-term row contributes nothing.
    assert run.row_gids == ("g_pinned", None)
    assert len(run.ambiguous) == 0
    assert len(run.unmatched) == 0
    # One clean group, labeled "learned" — the dropped row doesn't demote it.
    assert len(run.learned) == 1
    assert len(run.auto) == 0


def test_single_same_name_learned_mapping_drops_out_of_term_row():
    """When a learned name resolves to exactly one open contract, an
    out-of-term row is DROPPED (current-term-only), consistent with the
    pinned-gid path. The LM is operator intent for grouping, not a license to
    attribute outside the term — and pre-term spend is excluded, not surfaced."""
    contract = _contract("Acme", frozenset({"CEN"}), gid="g_only")
    learned = {("CEN", "000", "63015", "Acme"): [("Acme", None, None)]}
    df = pd.DataFrame([{
        "Date": pd.Timestamp("2024-12-15"),  # well before _contract default term
        "Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
        "Vendor": "Acme", "Record Description": "x", "Amount": 100.0,
    }])
    run = attribute(df, [contract], aliases={}, crosswalk=campus_map.build(),
                    learned_mappings=learned)
    assert run.row_gids == (None,)
    assert len(run.dropped) == 1
    assert len(run.ambiguous) == 0


def test_multi_same_name_learned_mapping_drops_fully_out_of_term_row():
    """When a learned name maps to >1 open contract and the row is out of term
    for EVERY one of them, it's pre-term spend → dropped (not ambiguous),
    consistent with the single-name and pinned paths."""
    contracts = [
        _contract("Acme", frozenset({"CEN"}), gid="g_cen"),
        _contract("Acme", frozenset({"OMH"}), gid="g_omh"),
    ]
    # _contract default term is 2026-01-01 -> 2026-12-31; this row pre-dates both.
    learned = {("CEN", "000", "63015", "Acme"): [("Acme", None, None)]}
    df = pd.DataFrame([{
        "Date": pd.Timestamp("2024-12-15"),
        "Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
        "Vendor": "Acme", "Record Description": "x", "Amount": 100.0,
    }])
    run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(),
                    learned_mappings=learned)
    assert run.row_gids == (None,)
    assert len(run.dropped) == 1
    assert len(run.ambiguous) == 0


def test_learned_mapping_with_multi_task_name_falls_through_to_narrowing():
    """An older learned mapping that points to a name with multiple open
    contracts must NOT auto-resolve — the engine still has to pick the
    right specific task. Campus + date narrowing apply normally."""
    contracts = [
        _contract("Acme", frozenset({"CEN"}), gid="g_cen"),
        _contract("Acme", frozenset({"OMH"}), gid="g_omh"),
    ]
    learned = {("CEN", "000", "63015", "Acme"): [("Acme", None, None)]}
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme", "Record Description": "x", "Amount": 100.0},
    )
    run = attribute(df, contracts, aliases={}, crosswalk=campus_map.build(),
                    learned_mappings=learned)
    # Learned name "Acme" had 2 matching contracts; campus narrow picks g_cen.
    assert run.row_gids == ("g_cen",)
    assert len(run.learned) == 1


# ---------------------------------------------------------------------------
# Code-review fixes: positional row_gids (#2), no-leak gating (#3),
# per-bucket out-of-term (#5), stop-word narrowing guard (#12), and
# Learned-Mapping pattern normalization/recurrence (#7).
# ---------------------------------------------------------------------------

def test_duplicate_record_no_does_not_collapse_split_spend():
    """#2: two in-scope rows sharing a Record No that attribute to DIFFERENT
    contracts (crossover split) each keep their own gid. A Record-No-keyed
    dict used to collapse them (last-write-wins); the positional row_gids
    tuple preserves both."""
    old_contract = Contract(
        gid="g_old", name="Acme Service", campus_options=frozenset({"CEN"}),
        contract_amount=10000.0,
        target_start=date(2025, 9, 1), due_on=date(2026, 9, 1),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
    )
    new_contract = Contract(
        gid="g_new", name="Acme Service", campus_options=frozenset({"CEN"}),
        contract_amount=10000.0,
        target_start=date(2026, 7, 1), due_on=date(2027, 7, 1),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
    )
    # BOTH rows carry the SAME Record No "DUP-1" (a 2-line invoice) but fall
    # in different contract windows.
    df = _df(
        {"Record No": "DUP-1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme Service", "Record Description": "aug", "Amount": 500.0,
         "Date": pd.Timestamp("2026-08-15")},
        {"Record No": "DUP-1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "Acme Service", "Record Description": "oct", "Amount": 750.0,
         "Date": pd.Timestamp("2026-10-15")},
    )
    run = attribute(df, [old_contract, new_contract], aliases={},
                    crosswalk=campus_map.build(), learned_mappings={})
    # Positional: row 0 -> old, row 1 -> new. Neither overwrote the other.
    assert run.row_gids == ("g_old", "g_new")


def test_ambiguous_group_does_not_leak_attributed_row_gids():
    """#3: a MIXED group (one row auto-attributes, one row ambiguous) is
    reported as ambiguous, and NONE of its rows contribute a gid to the
    spend map — so the already-attributed row's spend can't land on the
    Dashboard before the operator resolves the conflict."""
    a = Contract(
        gid="g_a", name="DupVendor", campus_options=frozenset({"CEN"}),
        contract_amount=10000.0,
        target_start=date(2026, 1, 1), due_on=date(2026, 12, 31),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
    )
    b = Contract(
        gid="g_b", name="DupVendor", campus_options=frozenset({"CEN"}),
        contract_amount=5000.0,
        target_start=date(2026, 1, 1), due_on=date(2026, 12, 31),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
    )
    solo = _contract("SoloVendor", frozenset({"CEN"}), gid="g_solo")
    df = _df(
        # SoloVendor row auto-attributes to g_solo on its own.
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "SoloVendor", "Record Description": "x", "Amount": 100.0},
        # DupVendor row is ambiguous (two identical same-name candidates).
        {"Record No": "R2", "Campus": "CEN", "Dept": "000", "Account No": "63015",
         "Vendor": "DupVendor", "Record Description": "y", "Amount": 200.0},
    )
    run = attribute(df, [a, b, solo], aliases={},
                    crosswalk=campus_map.build(), learned_mappings={})
    # SoloVendor is its own group and still attributes; DupVendor is ambiguous.
    # Both groups are distinct keys, so row 0 (solo) keeps its gid, row 1 None.
    assert run.row_gids == ("g_solo", None)


def test_mixed_date_group_is_flagged_out_of_term_per_bucket():
    """#5: a group with one IN-term bucket and one PRE-term bucket is flagged
    all_out_of_term=True (per-bucket), so the single-candidate row can be
    routed to Vendor Conflicts instead of being stranded. The old any()
    over the union would have left it False."""
    only = Contract(
        gid="g_solo", name="Office Express", campus_options=frozenset({"CEN"}),
        contract_amount=100000.0,
        target_start=date(2026, 1, 1), due_on=date(2026, 12, 31),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
    )
    df = pd.DataFrame([
        # In-term bucket.
        {"Date": pd.Timestamp("2026-06-01"), "Record No": "R1", "Campus": "CEN",
         "Dept": "000", "Account No": "63015", "Vendor": "Office Express",
         "Record Description": "June service", "Amount": 1000.0},
        # Pre-term bucket (no candidate covers 2025).
        {"Date": pd.Timestamp("2025-06-01"), "Record No": "R2", "Campus": "CEN",
         "Dept": "000", "Account No": "63015", "Vendor": "Office Express",
         "Record Description": "Prior-year service", "Amount": 2000.0},
    ])
    run = attribute(df, [only], aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    assert len(run.ambiguous) == 1
    assert run.ambiguous[0].all_out_of_term is True


def test_description_narrowing_rejects_single_shared_generic_token():
    """#12: a description that shares only a STOP-WORD ("services") with one
    candidate's reason text must NOT auto-attribute — it falls through to
    ambiguous rather than letting filler noise pick the wrong contract."""
    snow = Contract(
        gid="g_snow", name="Multi", campus_options=frozenset({"CEN"}),
        contract_amount=20000.0,
        target_start=date(2026, 1, 1), due_on=date(2026, 12, 31),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
        contract_reason_text="Snow plowing and salting services",
    )
    land = Contract(
        gid="g_land", name="Multi", campus_options=frozenset({"CEN"}),
        contract_amount=80000.0,
        target_start=date(2026, 1, 1), due_on=date(2026, 12, 31),
        status="Active", expire_countdown=None, pm_email=None,
        section_name="Active - Compliant",
        contract_reason_text="Lawn care and landscape maintenance",
    )
    # Description shares only the stop-word "services" with the snow reason.
    df = _df(
        {"Record No": "R1", "Campus": "CEN", "Dept": "000", "Account No": "63080",
         "Vendor": "Multi", "Record Description": "Miscellaneous services 2026",
         "Amount": 500.0, "Date": pd.Timestamp("2026-06-01")},
    )
    run = attribute(df, [snow, land], aliases={}, crosswalk=campus_map.build(),
                    learned_mappings={})
    # No decisive MEANINGFUL-token winner -> stays ambiguous, not auto.
    assert run.row_gids == (None,)
    assert len(run.ambiguous) == 1


def test_pattern_lm_survives_volatile_invoice_tokens():
    """#7: a pattern LM stored from one month's description ('Snow plowing
    INV-4471 2/2026') still matches next month's row ('Snow plowing INV-5093
    3/2026') because both normalize to the same meaningful-token stem."""
    from engine.attribution import normalize_lm_pattern
    snow = _contract("Bear Claw", frozenset({"NCS"}), gid="g_snow")
    pattern = normalize_lm_pattern("Snow plowing INV-4471 2/2026")
    # Numeric tokens (4471, 2, 2026) are stripped; the "INV" prefix survives
    # as a stable token, so the stem recurs across invoices.
    assert pattern == "snow plowing inv"
    learned = {
        ("NCS", "000", "63080", "Bear Claw"): [
            ("Bear Claw", "g_snow", pattern),
        ],
    }
    df = _df(
        {"Record No": "R1", "Campus": "NCS", "Dept": "000", "Account No": "63080",
         "Vendor": "Bear Claw", "Record Description": "Snow plowing INV-5093 3/2026",
         "Amount": 1000.0},
    )
    run = attribute(df, [snow], aliases={}, crosswalk=campus_map.build(),
                    learned_mappings=learned)
    # Different invoice number, same stem -> pattern still applies.
    assert run.row_gids == ("g_snow",)
    assert len(run.learned) == 1
