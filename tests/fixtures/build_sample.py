"""Generate tests/fixtures/transactions_sample.tsv in the production export's
exact byte format: UTF-16 LE BOM + tab-delimited + CRLF line endings.

The trailing-space conventions on 'Vendor ' and 'Program Name ' headers, the
unlabeled Amount column, the Grand Total summary row, and the
accounting-parens negatives all mirror what Step 1 research found in the
real C:/Users/philip.seabrook/Downloads/Transactions.csv.

Run manually whenever the fixture needs regenerating:

    python tests/fixtures/build_sample.py

Totals (verified by tests/test_filters.py):
    in-scope:  5 rows, signed sum  $7,000.00
    out-of-scope: 10 rows, signed sum  $3,500.00
"""

from __future__ import annotations

import pathlib


OUT = pathlib.Path(__file__).resolve().parent / "transactions_sample.tsv"


# Header line — note the trailing spaces on 'Vendor ' and 'Program Name ',
# and the empty final element for the unlabeled Amount column.
HEADER_COLUMNS: list[str] = [
    "Record No",
    "Campus",
    "Dept",
    "Account No",
    "Account Name",
    "Project ID",
    "Vendor ",
    "Record Description",
    "Program Name ",
    "Reference",
    "Date",
    "Type",
    "",
]


# Grand Total summary row — sits as the FIRST data row in the production export
# (filter is Campus == "Total"; position doesn't matter).
GRAND_TOTAL: list[str] = [
    "Grand Total", "Total", "Total", "Total", "Total", "Total",
    "Total", "Total", "Total", "Total", "Total", "Total",
    "$10,500.00",
]


# Each row matches the 13 columns of HEADER_COLUMNS.
#
# Account 63015 was REMOVED from ACCOUNTS_IN_SCOPE on 2026-06-16 (CapEx no
# longer tracked). R001's account stays as 63015 here so the fixture also
# proves that a previously-in-scope account now lands in the out-of-scope
# bucket. Effective totals after the change:
#   In-scope: 4 rows (R002–R005), signed sum $6,000.00
#   Out-of-scope: 11 rows (R001 + the original 10), signed sum $4,500.00
DATA_ROWS: list[tuple[str, ...]] = [
    # PREVIOUSLY in-scope; now out-of-scope (account 63015 / CapEx).
    ("R001", "CEN", "000", "63015", "SaaS Subscriptions", "",
     "Acme SaaS", "Annual subscription Q1", "",
     "ref-001", "1/15/2025", "Charge", "$1,000.00"),
    ("R002", "OMH", "107", "63020", "Software Licenses", "P-1",
     "Beta Tools", "Beta Tools license renewal", "Tech Ops",
     "ref-002", "2/20/2025", "Charge", "$2,500.00"),
    ("R003", "SBA", "000", "63040", "Maintenance", "",
     "Gamma Inc", "Equipment maintenance", "",
     "ref-003", "3/10/2025", "Charge", "$750.00"),
    ("R004", "CEN", "107", "63080", "Professional Services", "",
     "Delta LLC", "Service credit adjustment", "Programs",
     "ref-004", "4/5/2025", "Credit", "($250.00)"),
    ("R005", "YVN", "000", "63090", "SaaS Subscriptions", "P-2",
     "Epsilon Co", "Quarterly subscription", "",
     "ref-005", "5/12/2025", "Charge", "$3,000.00"),

    # OUT-OF-SCOPE (10 rows; signed sum $3,500.00)
    # Out by dept only:
    ("R006", "CEN", "100", "61000", "Other Expense", "",
     "Vendor F", "Misc supplies", "Operations",
     "ref-006", "1/8/2025", "Charge", "$500.00"),
    # Out by account and dept:
    ("R007", "OMH", "1000", "63010", "Software Support", "",
     "Vendor G", "Support contract", "",
     "ref-007", "1/9/2025", "Charge", "$800.00"),
    # Out by account:
    ("R008", "SBA", "000", "63001", "Other", "",
     "Vendor H", "Description H", "",
     "ref-008", "2/15/2025", "Charge", "$1,200.00"),
    # Out by account:
    ("R009", "INT", "107", "63050", "Other", "P-3",
     "Vendor I", "Description I", "",
     "ref-009", "2/25/2025", "Charge", "$400.00"),
    # Out by dept (account 63020 IS in scope) — proves AND not OR:
    ("R010", "CEN", "102", "63020", "Other", "",
     "Vendor J", "Description J", "",
     "ref-010", "3/3/2025", "Charge", "$600.00"),
    # Out by account, with Credit:
    ("R011", "OMH", "000", "63060", "Other", "",
     "Vendor K", "Refund K", "",
     "ref-011", "3/15/2025", "Credit", "($150.00)"),
    # Out by account (dept 107 IS in scope) — proves AND not OR:
    ("R012", "YVN", "107", "63061", "Other", "",
     "Vendor L", "Description L", "",
     "ref-012", "4/20/2025", "Charge", "$250.00"),
    # Out by dept (account 63040 IS in scope):
    ("R013", "CEN", "200", "63040", "Other", "",
     "Vendor M", "Description M", "",
     "ref-013", "5/1/2025", "Charge", "$900.00"),
    # Out by account, large Credit with thousands comma inside parens:
    ("R014", "SBA", "000", "61000", "Refund", "",
     "Vendor N", "Large refund", "",
     "ref-014", "5/30/2025", "Credit", "($1,000.00)"),
    # Out by account, zero amount edge case:
    ("R015", "CEN", "000", "63010", "Other", "",
     "Vendor O", "Description O", "",
     "ref-015", "6/2/2025", "Charge", "$0.00"),
]


def main() -> None:
    lines: list[str] = ["\t".join(HEADER_COLUMNS), "\t".join(GRAND_TOTAL)]
    for row in DATA_ROWS:
        if len(row) != len(HEADER_COLUMNS):
            raise AssertionError(
                f"row {row[0]} has {len(row)} cells; expected {len(HEADER_COLUMNS)}"
            )
        lines.append("\t".join(row))
    content = "\r\n".join(lines) + "\r\n"

    # 'utf-16' codec writes a UTF-16 LE BOM on little-endian platforms
    # (every modern Windows / x86-64), matching the production export's
    # byte format exactly.
    OUT.write_bytes(content.encode("utf-16"))

    head = OUT.read_bytes()[:2]
    assert head == b"\xff\xfe", (
        f"expected UTF-16 LE BOM; got {head!r}. Check the platform's "
        f"native byte order."
    )

    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    print(f"  header columns: {len(HEADER_COLUMNS)} "
          f"(trailing spaces preserved on 'Vendor ' and 'Program Name ')")
    print(f"  data rows: {len(DATA_ROWS)} + 1 grand-total")
    print(f"  expected in-scope: 5 rows, signed sum $7,000.00")
    print(f"  expected out-of-scope: 10 rows, signed sum $3,500.00")


if __name__ == "__main__":
    main()
