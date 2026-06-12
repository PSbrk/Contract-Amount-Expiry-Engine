"""Parser tests against the synthesized UTF-16 + tab + parens fixture.

Each test pins one of the documented Tableau-export quirks Step 1 research
surfaced. If the production file's format changes, regenerate
tests/fixtures/transactions_sample.tsv first and these tests are the
breakage signal.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from engine.ingest import (
    EXPECTED_COLUMNS,
    _parse_amount,
    parse_tableau_export,
)


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "transactions_sample.tsv"


def test_fixture_exists():
    assert FIXTURE.exists(), (
        f"fixture missing at {FIXTURE}. Regenerate with: "
        f"python tests/fixtures/build_sample.py"
    )


def test_fixture_is_utf16_with_bom():
    head = FIXTURE.read_bytes()[:2]
    assert head == b"\xff\xfe", (
        f"fixture is not UTF-16 LE BOM; first two bytes were {head!r}. "
        f"Regenerate via tests/fixtures/build_sample.py."
    )


def test_parse_strips_trailing_spaces_in_headers():
    df = parse_tableau_export(FIXTURE)
    # 'Vendor ' and 'Program Name ' come from the production file with trailing
    # spaces; parser must strip them so downstream lookups by name work.
    assert "Vendor" in df.columns
    assert "Program Name" in df.columns
    assert "Vendor " not in df.columns
    assert "Program Name " not in df.columns


def test_parse_renames_unlabeled_amount_column():
    df = parse_tableau_export(FIXTURE)
    assert "Amount" in df.columns
    assert "" not in df.columns


def test_parse_returns_all_expected_columns_in_canonical_order():
    df = parse_tableau_export(FIXTURE)
    assert list(df.columns) == list(EXPECTED_COLUMNS)


def test_parse_drops_grand_total_row():
    df = parse_tableau_export(FIXTURE)
    # 15 data rows + 1 'Grand Total' row in the fixture → 15 after the
    # Campus == 'Total' drop.
    assert len(df) == 15
    assert (df["Campus"] == "Total").sum() == 0


def test_parse_preserves_dept_leading_zeros():
    df = parse_tableau_export(FIXTURE)
    # '000' is the most common in-scope Dept value in the production file.
    # If pandas inferred int the value would become '0' or 0 — engine then
    # silently drops every '000' row from in-scope.
    assert "000" in df["Dept"].values
    # All Dept values must be strings (preserved as such by dtype="string").
    assert all(isinstance(v, str) for v in df["Dept"].head(5).tolist())


def test_parse_amount_signs_match_type():
    df = parse_tableau_export(FIXTURE)
    charges = df.loc[df["Type"] == "Charge", "Amount"]
    credits = df.loc[df["Type"] == "Credit", "Amount"]
    assert (charges >= 0).all(), (
        "every Charge row's Amount should be >= 0 after parens cleanup; "
        f"violations:\n{df.loc[df['Type'] == 'Charge'].loc[charges < 0]}"
    )
    assert (credits < 0).all(), (
        "every Credit row's Amount should be < 0 after parens cleanup; "
        f"violations:\n{df.loc[df['Type'] == 'Credit'].loc[credits >= 0]}"
    )


def test_parse_handles_accounting_parens_amounts():
    df = parse_tableau_export(FIXTURE)
    # R004: '($250.00)' -> -250
    r004 = df.loc[df["Record No"] == "R004"].iloc[0]
    assert r004["Amount"] == pytest.approx(-250.00)
    # R014: '($1,000.00)' — thousands comma INSIDE parens
    r014 = df.loc[df["Record No"] == "R014"].iloc[0]
    assert r014["Amount"] == pytest.approx(-1000.00)


def test_parse_handles_dollar_and_commas():
    df = parse_tableau_export(FIXTURE)
    # R002: '$2,500.00' -> 2500
    r002 = df.loc[df["Record No"] == "R002"].iloc[0]
    assert r002["Amount"] == pytest.approx(2500.00)


def test_parse_handles_zero_amount():
    df = parse_tableau_export(FIXTURE)
    r015 = df.loc[df["Record No"] == "R015"].iloc[0]
    assert r015["Amount"] == 0.0


def test_parse_dates_are_pandas_timestamp():
    df = parse_tableau_export(FIXTURE)
    r001 = df.loc[df["Record No"] == "R001"].iloc[0]
    assert isinstance(r001["Date"], pd.Timestamp)
    assert r001["Date"].date() == datetime(2025, 1, 15).date()


def test_parse_amount_helper_known_cases():
    # The Amount cleaner is the most format-sensitive piece. Pin it directly.
    assert _parse_amount("$1,000.00") == 1000.0
    assert _parse_amount("($788.38)") == -788.38
    assert _parse_amount("($244,362.45)") == -244362.45
    assert _parse_amount("$0.00") == 0.0
    assert _parse_amount("") == 0.0
    assert _parse_amount(None) == 0.0
    assert _parse_amount("   $50.00 ") == 50.0


def test_parse_amount_helper_raises_on_garbage():
    """A bad Amount cell must surface loudly — silent fallback to 0 would
    corrupt downstream totals."""
    with pytest.raises(ValueError):
        _parse_amount("notamoney")


def test_parse_from_bytes_matches_parse_from_path():
    by_path = parse_tableau_export(FIXTURE)
    by_bytes = parse_tableau_export(FIXTURE.read_bytes(), filename=FIXTURE.name)
    pd.testing.assert_frame_equal(by_path, by_bytes)


# ---------------------------------------------------------------------------
# Hardening pins (added after Step 2 adversarial review)
# ---------------------------------------------------------------------------

_TSV_HEADER = (
    "Record No\tCampus\tDept\tAccount No\tAccount Name\tProject ID\t"
    "Vendor \tRecord Description\tProgram Name \tReference\tDate\tType\t"
)
_TSV_TOTAL = (
    "Grand Total\tTotal\tTotal\tTotal\tTotal\tTotal\tTotal\t"
    "Total\tTotal\tTotal\tTotal\tTotal\t$0.00"
)


def _write_tsv(path, *rows: str) -> None:
    """Write a UTF-16 LE BOM + tab + CRLF TSV with the production header."""
    body = "\r\n".join([_TSV_HEADER, _TSV_TOTAL, *rows]) + "\r\n"
    path.write_bytes(body.encode("utf-16"))


def test_parse_normalizes_sign_from_type_column(tmp_path):
    """Spec §4 says the sign is derived from the Type column. If a Credit row
    ever exports without accounting parens (Tableau workbook drift), the
    Type-based normalization must still produce a negative Amount."""
    path = tmp_path / "weird.tsv"
    _write_tsv(
        path,
        # Credit row WITHOUT parens — Type must still drive the sign
        "R001\tCEN\t000\t63015\tDesc\t\tVendor A\tDesc A\t\tref\t1/1/2025\tCredit\t$500.00",
        # Charge row WITH bizarre parens — Type must still drive (positive)
        "R002\tCEN\t000\t63015\tDesc\t\tVendor B\tDesc B\t\tref\t1/2/2025\tCharge\t($300.00)",
    )
    df = parse_tableau_export(path)
    credit = df.loc[df["Record No"] == "R001"].iloc[0]
    charge = df.loc[df["Record No"] == "R002"].iloc[0]
    assert credit["Amount"] == pytest.approx(-500.0), (
        "Credit must be negative even when parens are missing"
    )
    assert charge["Amount"] == pytest.approx(300.0), (
        "Charge must be positive even when value arrives in parens"
    )


def test_parse_strips_trailing_whitespace_in_data_cells(tmp_path):
    """A future export with a stray trailing space in Campus / Dept / Account
    No / Type must NOT silently shift rows between in_scope and out_of_scope.
    Defensive strip on every string column."""
    from engine.filters import in_scope

    path = tmp_path / "trailing.tsv"
    _write_tsv(
        path,
        # In-scope row with trailing whitespace on key filter columns.
        "R001\tCEN \t000 \t63015 \tDesc\t\tVendor\tDesc\t\tref\t1/1/2025\tCharge \t$1,000.00",
    )
    df = parse_tableau_export(path)
    row = df.iloc[0]
    assert row["Campus"] == "CEN"
    assert row["Dept"] == "000"
    assert row["Account No"] == "63015"
    assert row["Type"] == "Charge"
    # And the filter actually keeps it.
    assert len(in_scope(df)) == 1


def test_parse_drops_total_row_with_trailing_whitespace(tmp_path):
    """If a future export ever emits Campus='Total ' (trailing whitespace),
    the engine must still drop the summary row. Without the strip we'd leak
    its (large) Amount into in/out scope totals."""
    path = tmp_path / "total_ws.tsv"
    header = _TSV_HEADER
    bad_total = (
        "Grand Total\tTotal \tTotal\tTotal\tTotal\tTotal\t"
        "Total\tTotal\tTotal\tTotal\t1/1/2025\tCharge\t$99,999.99"
    )
    real_row = (
        "R001\tCEN\t000\t63015\tDesc\t\tVendor\tDesc\t\tref\t"
        "2/1/2025\tCharge\t$1.00"
    )
    body = "\r\n".join([header, bad_total, real_row]) + "\r\n"
    path.write_bytes(body.encode("utf-16"))

    df = parse_tableau_export(path)
    # Should be one real row, not two.
    assert len(df) == 1
    assert (df["Campus"] == "Total").sum() == 0


def test_parse_amount_handles_pd_na():
    """xlsx fallback path produces pd.NA for empty Amount cells — must not
    crash the cleaner."""
    assert _parse_amount(pd.NA) == 0.0
    assert _parse_amount("<NA>") == 0.0
    # pandas NaT and float NaN both pass pd.isna and should round-trip to 0.0
    import math
    assert _parse_amount(math.nan) == 0.0


def test_parse_warns_on_extra_columns(tmp_path, caplog):
    """A 14th column in a future export must surface as a WARNING — silent
    drop has bitten too many ingestion pipelines."""
    import logging
    extra_header = _TSV_HEADER + "\tBonusColumn"
    total = _TSV_TOTAL + "\tTotal"
    row = (
        "R001\tCEN\t000\t63015\tDesc\t\tVendor\tDesc\t\tref\t"
        "1/1/2025\tCharge\t$1.00\textra-value"
    )
    body = "\r\n".join([extra_header, total, row]) + "\r\n"
    path = tmp_path / "extra.tsv"
    path.write_bytes(body.encode("utf-16"))

    with caplog.at_level(logging.WARNING, logger="engine.ingest"):
        df = parse_tableau_export(path)
    assert len(df) == 1
    assert "BonusColumn" in caplog.text
    assert "unexpected column" in caplog.text
