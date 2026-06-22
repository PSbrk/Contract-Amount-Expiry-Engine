"""Filter + signed-sum tests against the fixture.

Pins the spec §4 contract that filtering enforces account AND dept (not OR),
and that the signed sum honors the parens-cleanup performed during parsing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config import settings
from engine.filters import in_scope, is_in_scope_mask, out_of_scope, signed_sum
from engine.ingest import parse_tableau_export


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "transactions_sample.tsv"


def test_in_scope_count_matches_fixture():
    df = parse_tableau_export(FIXTURE)
    kept = in_scope(df)
    # 4 in-scope rows: R002 (63020), R003 (63040), R004 (63080), R005 (63090).
    # R001 (63015) MOVED to out-of-scope on 2026-06-16 when CapEx was dropped.
    assert len(kept) == 4
    # Every kept row must satisfy BOTH filters.
    assert kept["Account No"].isin(settings.ACCOUNTS_IN_SCOPE).all()
    assert kept["Dept"].isin(settings.DEPTS_IN_SCOPE).all()


def test_in_scope_signed_sum_matches_fixture():
    df = parse_tableau_export(FIXTURE)
    kept = in_scope(df)
    # 2500 + 750 - 250 + 3000 = 6000 (R001 dropped after CapEx removed).
    assert signed_sum(kept) == pytest.approx(6000.00)


def test_out_of_scope_count_matches_fixture():
    df = parse_tableau_export(FIXTURE)
    rejected = out_of_scope(df)
    # 10 originally + R001 (63015 / now out by account) = 11.
    assert len(rejected) == 11


def test_out_of_scope_signed_sum_matches_fixture():
    df = parse_tableau_export(FIXTURE)
    rejected = out_of_scope(df)
    # 500 + 800 + 1200 + 400 + 600 - 150 + 250 + 900 - 1000 + 0 + 1000 (R001) = 4500.
    assert signed_sum(rejected) == pytest.approx(4500.00)


def test_in_and_out_partition_perfectly():
    df = parse_tableau_export(FIXTURE)
    assert len(in_scope(df)) + len(out_of_scope(df)) == len(df), (
        "in_scope and out_of_scope must partition the input — every row "
        "falls in exactly one"
    )


def test_dept_out_with_in_scope_account_is_rejected():
    """Dept-only failure (account is in scope) — pins AND not OR."""
    df = parse_tableau_export(FIXTURE)
    rejected = out_of_scope(df)
    # R010 has account 63020 (in scope) but dept 102 (out). Must be rejected.
    assert "R010" in rejected["Record No"].values


def test_account_out_with_in_scope_dept_is_rejected():
    """Account-only failure (dept is in scope) — pins AND not OR."""
    df = parse_tableau_export(FIXTURE)
    rejected = out_of_scope(df)
    # R012 has dept 107 (in scope) but account 63061 (out). Must be rejected.
    assert "R012" in rejected["Record No"].values


def test_is_in_scope_mask_shape_and_count():
    df = parse_tableau_export(FIXTURE)
    mask = is_in_scope_mask(df)
    assert len(mask) == len(df)
    assert mask.dtype == bool
    # 4 in-scope after CapEx (63015) was removed from ACCOUNTS_IN_SCOPE.
    assert mask.sum() == 4


def test_credits_keep_negative_sign_through_filter():
    """The in-scope set in the fixture contains exactly one Credit row (R004,
    -250). Make sure filtering doesn't accidentally flip the sign."""
    df = parse_tableau_export(FIXTURE)
    kept = in_scope(df)
    credits = kept.loc[kept["Type"] == "Credit"]
    assert len(credits) == 1
    assert credits.iloc[0]["Amount"] == pytest.approx(-250.00)
