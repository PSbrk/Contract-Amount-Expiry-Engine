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
        # Using 63080 (still in scope) — 63015 was retired on 2026-06-16.
        "R001\tCEN \t000 \t63080 \tDesc\t\tVendor\tDesc\t\tref\t1/1/2025\tCharge \t$1,000.00",
    )
    df = parse_tableau_export(path)
    row = df.iloc[0]
    assert row["Campus"] == "CEN"
    assert row["Dept"] == "000"
    assert row["Account No"] == "63080"
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


# ---------------------------------------------------------------------------
# Step 7: TableauRestSource stub
# ---------------------------------------------------------------------------

def _make_stub_source(**overrides):
    """Construct a TableauRestSource with sensible test defaults; tests can
    override individual params without re-spelling the whole kwargs block."""
    from engine.ingest import TableauRestSource

    params: dict = dict(
        server_url="https://us-west-2b.online.tableau.com",
        site_name="lifechurch",
        view_id="view-abc-123",
        pat_name="ci-token",
        pat_secret="not-a-real-secret",
        api_version="3.22",
    )
    params.update(overrides)
    return TableauRestSource(**params)


def test_tableau_rest_source_conforms_to_transaction_source_protocol():
    """Stub must satisfy the runtime-checkable TransactionSource Protocol so
    main.py can hold the same variable type regardless of which source is
    selected. If the Protocol drifts (new methods added), this test breaks
    loudly instead of silently exempting the stub."""
    from engine.ingest import TransactionSource

    src = _make_stub_source()
    assert isinstance(src, TransactionSource)


def test_tableau_rest_source_raises_not_implemented_on_pull():
    """Calling the stub MUST raise NotImplementedError — if a future refactor
    accidentally turns it into a no-op (returning an empty DataFrame, say),
    the engine would silently mark Inbox rows Processed with 0 results and
    erase a day's transactions. NotImplementedError is the loud crash that
    keeps the operator's data safe until the REST pull actually works."""
    src = _make_stub_source()
    with pytest.raises(NotImplementedError, match="TableauRestSource is a stub"):
        src.get_latest_transactions()


def test_tableau_rest_source_strips_trailing_slash_on_server_url():
    """Tiny ergonomics fix for the eventual REST joiner — accept both
    'https://server.example.com' and 'https://server.example.com/' from the
    operator's env config."""
    src = _make_stub_source(server_url="https://us-west-2b.online.tableau.com/")
    assert src.server_url == "https://us-west-2b.online.tableau.com"


def test_tableau_rest_source_holds_passed_params_verbatim():
    """The stub MUST round-trip every param it was given so the future
    implementation can read them off `self.*` without re-plumbing the
    constructor. If a param is silently dropped, the eventual REST signin
    would fail mysteriously when the real code lands."""
    src = _make_stub_source(
        view_id="custom-view",
        pat_name="custom-token-name",
        pat_secret="custom-secret",
        api_version="3.99",
    )
    assert src.view_id == "custom-view"
    assert src.pat_name == "custom-token-name"
    assert src.pat_secret == "custom-secret"
    assert src.api_version == "3.99"
    assert src.site_name == "lifechurch"


def test_tableau_rest_source_accepts_none_view_id_for_unconfigured_install():
    """The default install runs on `local_inbox` and may never set
    TABLEAU_VIEW_ID; settings will pass None. The stub must accept that
    without crashing in its constructor (so audit/provision modes still
    run even when the env is half-configured)."""
    src = _make_stub_source(view_id=None, pat_name=None, pat_secret=None)
    assert src.view_id is None
    assert src.pat_name is None
    assert src.pat_secret is None


# ---------------------------------------------------------------------------
# Step 7: TRANSACTION_SOURCE config switch
# ---------------------------------------------------------------------------

def test_transaction_source_default_is_local_inbox():
    """Phase 2 of the local-first migration: with no TRANSACTION_SOURCE
    env set, the engine scans data/inbox/ rather than calling Airtable.
    `airtable_inbox` and `tableau_rest` remain available but are opt-in."""
    from config import settings
    assert settings.TRANSACTION_SOURCE == "local_inbox"


def test_vendor_backfill_from_bill_description(tmp_path):
    """Tableau emits AP bills with blank Vendor and 'Bill - <X>: <memo>' in
    Record Description. Engine must copy the vendor name out of the
    description so these rows merge with the real Vendor='<X>' groups."""
    path = tmp_path / "billbackfill.tsv"
    _write_tsv(
        path,
        # Blank vendor, 'Bill - X:' description → Vendor backfilled.
        "R001\tSTO\t000\t63090\tDesc\t\t\tBill - The Stewards Company: Window Cleaning 12/2024\t\tref\t1/1/2025\tCharge\t$1,050.00",
        # Blank vendor, no Bill - pattern → stays blank (p-card style).
        "R002\tCEN\t000\t63040\tDesc\t\t\tCarpet square glue, LOWES #01536*\t\tref\t1/15/2025\tCharge\t$120.00",
        # Already-filled vendor → unchanged even if description has Bill -.
        "R003\tBAO\t000\t63090\tDesc\t\tFoo Co\tBill - Bar Co: ignore me\t\tref\t1/20/2025\tCharge\t$500.00",
    )
    df = parse_tableau_export(path)
    r001 = df.loc[df["Record No"] == "R001"].iloc[0]
    r002 = df.loc[df["Record No"] == "R002"].iloc[0]
    r003 = df.loc[df["Record No"] == "R003"].iloc[0]
    assert r001["Vendor"] == "The Stewards Company"
    assert (r002["Vendor"] or "") == ""  # untouched
    assert r003["Vendor"] == "Foo Co"     # source-truth wins


def test_reversal_description_forces_negative_amount(tmp_path):
    """'Reversed -- ' rows are clawbacks (vendor didn't deliver) and must
    net out against the original charge. Tableau exports them as POSITIVE
    Charge rows; engine forces -abs(Amount) regardless of source sign."""
    path = tmp_path / "reversal.tsv"
    _write_tsv(
        path,
        # Positive Charge with Reversed prefix → must flip to negative.
        "R001\tSTO\t000\t63090\tDesc\t\t\tReversed -- Bill - The Stewards Company: Window Cleaning 12/2024\t\tref\t1/1/2025\tCharge\t$9,459.57",
        # Positive Charge, no Reversed prefix → stays positive.
        "R002\tSTO\t000\t63090\tDesc\t\tFoo Co\tWindow Cleaning 01/2025\t\tref\t1/15/2025\tCharge\t$1,000.00",
        # Reversed prefix with no Bill - pattern (sign flips, vendor stays blank).
        "R003\tMUS\t000\t63040\tDesc\t\t\tReversed -- MUS Lowe's CAM Charges Accrual\t\tref\t1/20/2025\tCharge\t$2,500.00",
    )
    df = parse_tableau_export(path)
    r001 = df.loc[df["Record No"] == "R001"].iloc[0]
    r002 = df.loc[df["Record No"] == "R002"].iloc[0]
    r003 = df.loc[df["Record No"] == "R003"].iloc[0]
    assert r001["Amount"] == pytest.approx(-9459.57)
    assert r001["Vendor"] == "The Stewards Company"
    assert r002["Amount"] == pytest.approx(1000.0)
    assert r003["Amount"] == pytest.approx(-2500.0)
    assert (r003["Vendor"] or "") == ""


def test_reversal_already_negative_stays_negative(tmp_path):
    """A reversal row that arrives with parens (already negative after
    parse) must stay negative — the rule is -abs, idempotent."""
    path = tmp_path / "reversal_neg.tsv"
    _write_tsv(
        path,
        "R001\tSTO\t000\t63090\tDesc\t\t\tReversed -- Bill - X Co: memo\t\tref\t1/1/2025\tCredit\t($500.00)",
    )
    df = parse_tableau_export(path)
    r001 = df.loc[df["Record No"] == "R001"].iloc[0]
    assert r001["Amount"] == pytest.approx(-500.0)


def test_vendor_backfill_and_sign_flip_compose(tmp_path):
    """A 'Reversed -- Bill - X:' row gets BOTH vendor backfill AND sign
    flip. The two operations are orthogonal."""
    path = tmp_path / "combo.tsv"
    _write_tsv(
        path,
        "R001\tSTO\t000\t63090\tDesc\t\t\tReversed -- Bill - The Stewards Company: Window Cleaning 12/2024\t\tref\t1/1/2025\tCharge\t$9,459.57",
    )
    df = parse_tableau_export(path)
    r001 = df.loc[df["Record No"] == "R001"].iloc[0]
    assert r001["Vendor"] == "The Stewards Company"
    assert r001["Amount"] == pytest.approx(-9459.57)


def test_is_p_card_row_predicate():
    """Blank-vendor non-Bill rows route to the P-card surface; the P-card-vs-
    contract call is made PER LINE ITEM (has_cardholder_signature), not here."""
    from engine.ingest import is_p_card_row
    assert is_p_card_row("", "Reflective markers, GRAINGER, Hunter, Tami, 01/03/2025")
    assert is_p_card_row(None, "Carpet square glue, LOWES #01536*, Sanders, Jesse, 01/15/2025")
    assert is_p_card_row("   ", "Office supplies, AMAZON MKTPL*ZP06Y5UV0, Mea, 01/19/2025")
    # Blank-vendor with no signature still lands on the P-card surface (the
    # operator links these contract line items from there).
    assert is_p_card_row("", "Gallivan Snow Contract")
    assert is_p_card_row("", "Reversed -- MUS Lowe's CAM Charges Accrual")
    # NOT p-card: vendor populated.
    assert not is_p_card_row("The Stewards Company", "Window Cleaning 12/2024")
    assert not is_p_card_row("Bear Claw Landscaping", "Groundskeeping 01/2025")
    # NOT p-card: Bill - X: pattern (AP bill row — Phase 10 would backfill).
    assert not is_p_card_row("", "Bill - The Stewards Company: Window Cleaning 12/2024")
    assert not is_p_card_row("", "Reversed -- Bill - Foo Co: bar memo")


def test_has_cardholder_signature():
    """Per-line-item P-card tell: '…, Name, MM/DD/YYYY' at the end."""
    from engine.ingest import has_cardholder_signature
    assert has_cardholder_signature("Reflective markers, GRAINGER, Hunter, Tami, 01/03/2025")
    assert has_cardholder_signature("Office supplies, AMAZON MKTPL*ZP06Y5UV0, Mea, 01/19/2025")
    assert has_cardholder_signature("Fuel, PHILLIPS 66 - ONCUE, Snyder, Andrew T, 01/13/2025")
    # Contract/invoice spend — no cardholder signature.
    assert not has_cardholder_signature("Gallivan Snow Contract")
    assert not has_cardholder_signature("Snow Removal 10/2024")            # MM/YYYY, no day
    assert not has_cardholder_signature("OWS Lighting Upgrades LYNTEC RPCR-16")
    assert not has_cardholder_signature("Snow/ice management 01/23, 01/26")  # no name before date


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
