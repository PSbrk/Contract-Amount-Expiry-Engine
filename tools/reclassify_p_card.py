"""One-shot reclassifier for the Is P-Card flag on Needs Tagging.

The engine sets Is P-Card during every ingest upsert based on the
is_p_card_row predicate, so steady-state operation needs no manual
intervention. But when Phase 11 first lands, the existing Needs Tagging
rows in data/engine.db have Is P-Card = 0 (the column's default for
rows inserted before the column existed) -- so blank-vendor non-Bill
rows that SHOULD live on /p-card-spend are still invisible there.

This script scans every non-dismissed Needs Tagging row, applies the
predicate, and writes Is P-Card to whatever the predicate currently
returns. Idempotent: re-running it is fine; only rows whose flag differs
from the predicted value are updated. Safe to run while the engine is
NOT serving (close EngineApp.exe first to release the SQLite handle).

Usage:
    python tools/reclassify_p_card.py [--db PATH]

Default --db is dist/ContractEngine/data/engine.db (the bundle's
production database). Pass --db data/engine.db (or any path) to target
a different copy.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# Allow running as `python tools/reclassify_p_card.py` from the project
# root without setting PYTHONPATH -- prepend the project root to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default="dist/ContractEngine/data/engine.db",
        help="Path to engine.db (default: dist/ContractEngine/data/engine.db)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing.",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: db not found at {db_path}", file=sys.stderr)
        return 1

    # Import after argparse so --help works without engine dependencies.
    from engine.ingest import is_p_card_row

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    cols = [r[1] for r in conn.execute(
        'PRAGMA table_info("Needs Tagging")'
    ).fetchall()]
    if "Is P-Card" not in cols:
        print("ERROR: 'Is P-Card' column not found on Needs Tagging. Run "
              "the engine once with --provision to migrate the schema.",
              file=sys.stderr)
        return 1

    rows = conn.execute(
        '''SELECT id, "Vendor", "Sample Record Description",
                  COALESCE("Is P-Card", 0) AS current_flag,
                  "$ in group"
           FROM "Needs Tagging"
           WHERE COALESCE("Dismissed", 0) = 0'''
    ).fetchall()

    updates: list[tuple[int, int]] = []  # (id, new_flag)
    flipped_on_amount = 0.0
    flipped_off_amount = 0.0
    for r in rows:
        predicted = 1 if is_p_card_row(r["Vendor"], r["Sample Record Description"]) else 0
        if predicted != r["current_flag"]:
            updates.append((r["id"], predicted))
            amt = r["$ in group"] or 0
            if predicted == 1:
                flipped_on_amount += amt
            else:
                flipped_off_amount += amt

    flipped_on = sum(1 for _, v in updates if v == 1)
    flipped_off = sum(1 for _, v in updates if v == 0)

    print(f"Scanned {len(rows)} non-dismissed Needs Tagging rows in {db_path}")
    print(f"  Will flag ON  (route to P-Card Spend): {flipped_on:>4} rows "
          f"(${flipped_on_amount:,.2f})")
    print(f"  Will flag OFF (route back to Needs Tagging): "
          f"{flipped_off:>4} rows (${flipped_off_amount:,.2f})")
    print(f"  No change: {len(rows) - len(updates):>4} rows")

    if args.dry_run:
        print("\n--dry-run: no writes performed.")
        return 0

    if not updates:
        print("\nNothing to update.")
        return 0

    with conn:
        conn.executemany(
            'UPDATE "Needs Tagging" SET "Is P-Card" = ? WHERE id = ?',
            [(v, i) for i, v in updates],
        )
    print(f"\nUpdated {len(updates)} rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
