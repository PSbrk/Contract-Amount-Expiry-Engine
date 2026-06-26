"""CapEx tier — cross-tier Miscoded? redirect (opex charge → CapEx project).

Money path: an opex-coded row the operator accepts onto a CapEx contract must
recode into the CapEx tier and be counted ONCE under that project — never
double-counted, never left in opex.
"""
import sqlite3
from datetime import date

import pandas as pd
import pytest

from engine.capex import apply_capex_redirects, compute_capex
from engine.asana_contracts import Contract, normalize_capex_id
from engine.sqlite_client import (
    ensure_schema,
    load_capex_redirect_pins,
    upsert_plain_learned_mapping,
)


def _contract(gid, name, cid):
    return Contract(
        gid=gid, name=name, campus_options=frozenset(), contract_amount=None,
        target_start=None, due_on=None, status="Active", expire_countdown=None,
        pm_email=None, section_name="Active - Compliant",
        capex_id=normalize_capex_id(cid),
    )


def _df():
    return pd.DataFrame({
        "Campus":     ["OMH", "OMH", "AAA"],
        "Dept":       ["000", "000", "000"],
        "Account No": ["63040", "63040", "63040"],
        "Vendor":     ["JBP Concrete", "JBP Concrete", "Other Vendor"],
        "Project ID": ["", "", ""],
        "Amount":     [3_400.0, 100.0, 50.0],
        "Date":       pd.to_datetime(["2026-03-10", "2026-03-11", "2026-03-12"]),
    })


def test_redirect_recodes_matching_rows_into_capex_tier():
    redirects = {("OMH", "000", "63040", "JBP Concrete"): "FFE001428"}
    recoded, n = apply_capex_redirects(_df(), redirects, "63015")
    assert n == 2
    assert (recoded["Account No"] == "63015").sum() == 2
    assert set(recoded.loc[recoded["Account No"] == "63015", "Project ID"]) == {"FFE001428"}
    # The non-matching vendor is untouched — stays opex (no spurious recode).
    assert (recoded["Account No"] == "63040").sum() == 1


def test_redirect_no_double_count_and_lands_on_project():
    redirects = {("OMH", "000", "63040", "JBP Concrete"): "FFE001428"}
    recoded, _ = apply_capex_redirects(_df(), redirects, "63015")
    run = compute_capex(recoded, [_contract("gj", "JBP", "FFE001428")],
                        {"FFE001428": 100_000.0}, date(2026, 6, 24))
    # Counted ONCE under the project: 3400 + 100. The opex tier (Account No !=
    # 63015) would no longer see these rows, so there is no double-count.
    assert {r.asana_task_gid for r in run.rows} == {"gj"}
    assert run.rows[0].spent_so_far == 3_500.0


def test_empty_redirects_is_identity():
    df = _df()
    out, n = apply_capex_redirects(df, {}, "63015")
    assert n == 0 and out.equals(df)


def test_load_capex_redirect_pins_returns_only_ignore_coding_gid_pins():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    # An Ignore-Coding pin (a Miscoded? Accept) → returned.
    upsert_plain_learned_mapping(
        c, key="OMH|000|63040|JBP Concrete", campus="OMH", dept="000",
        account_no="63040", vendor="JBP Concrete",
        contract_name="JBP Concrete & Construction, LLC", contract_gid="g_capex",
        ignore_coding=True, learned_at="2026-06-26",
    )
    # A plain name-only LM (no gid, not ignore-coding) → excluded.
    upsert_plain_learned_mapping(
        c, key="CEN|000|63080|Acme", campus="CEN", dept="000",
        account_no="63080", vendor="Acme", contract_name="Acme",
        learned_at="2026-06-26",
    )
    pins = load_capex_redirect_pins(c)
    assert pins == {("OMH", "000", "63040", "JBP Concrete"): "g_capex"}
    c.close()
