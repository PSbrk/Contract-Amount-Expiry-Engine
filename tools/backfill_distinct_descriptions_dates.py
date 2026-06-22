"""One-shot backfill for Distinct Descriptions JSON date fields (Phase 13).

Phase 13 added min_date / max_date to each per-description bucket so the
Vendor Conflicts auto-suggest can reject candidates whose contract Term
doesn't overlap the description's transaction dates. The fields populate
naturally on every subsequent ingest. But the EXISTING rows in
data/engine.db were written by the pre-Phase-13 code path and have no
date fields -- so the date filter silently degrades to text-only for
them and the operator sees stale auto-suggestions until the next ingest.

This script re-reads a Tableau export and patches min_date / max_date
into the existing Distinct Descriptions JSON IN PLACE. No other column
is touched -- $ in group, candidate gids, operator decisions
(Assign Contract / Dismissed / Once Off / Conflict Other) all stay.

Idempotent: re-running on the same input rewrites the same dates.
Safe to run while the engine UI is up (only one row at a time is held
in a transaction); safer still with the engine closed.

Usage:
    python tools/backfill_distinct_descriptions_dates.py \\
        --source "C:\\Users\\philip.seabrook\\Downloads\\Transactions.csv" \\
        [--db PATH]

Default --db is dist/ContractEngine/data/engine.db (the bundle's prod DB).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from engine.ingest import parse_tableau_export  # noqa: E402


def _row_date_to_iso(v) -> str | None:
    """Best-effort to coerce a row Date cell into ISO YYYY-MM-DD.
    Returns None on unparseable / missing dates.
    """
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        ts = pd.to_datetime(v, errors="coerce")
    except Exception:
        return None
    if pd.isna(ts):
        return None
    return ts.date().isoformat()


def _build_date_index(df: pd.DataFrame) -> dict[tuple[str, str, str, str, str], tuple[str, str]]:
    """Group source rows by (campus, dept, account_no, vendor, description)
    and compute (min_iso_date, max_iso_date) per bucket. The 5-key matches
    how attribution.py keys distinct_descriptions, so a Needs Tagging row's
    JSON can be looked up directly.
    """
    out: dict[tuple[str, str, str, str, str], list[str]] = {}
    for _, row in df.iterrows():
        campus = str(row.get("Campus") or "").strip()
        dept = str(row.get("Dept") or "").strip()
        account_no = str(row.get("Account No") or "").strip()
        vendor = str(row.get("Vendor") or "").strip()
        desc = str(row.get("Record Description") or "").strip()
        iso = _row_date_to_iso(row.get("Date"))
        if iso is None:
            continue
        key = (campus, dept, account_no, vendor, desc)
        out.setdefault(key, []).append(iso)
    # Collapse each list into (min, max).
    return {k: (min(v), max(v)) for k, v in out.items()}


def _patch_json(
    raw_json: str,
    group_key: str,
    date_index: dict[tuple[str, str, str, str, str], tuple[str, str]],
) -> tuple[str, int, int]:
    """Patch min_date / max_date into the Distinct Descriptions JSON for
    one Needs Tagging row. Returns (new_json, patched_count, total_count).
    Buckets whose key doesn't appear in date_index keep their existing
    (possibly empty) min_date / max_date.

    The Needs Tagging Group Key is "Campus|Dept|Account No|Vendor".
    """
    try:
        items = json.loads(raw_json or "[]")
    except (TypeError, ValueError):
        return raw_json, 0, 0
    if not isinstance(items, list):
        return raw_json, 0, 0

    # Group_key splits into 4 fields; Tableau Record Description is the
    # 5th (per-bucket) lookup key.
    parts = (group_key or "").split("|")
    if len(parts) != 4:
        return raw_json, 0, 0
    campus, dept, account_no, vendor = parts

    patched = 0
    new_items = []
    for it in items:
        if not isinstance(it, dict):
            new_items.append(it)
            continue
        desc = (it.get("description") or "").strip()
        lookup = (campus, dept, account_no, vendor, desc)
        mn, mx = date_index.get(lookup, (None, None))
        new_it = dict(it)
        # Only overwrite when we found dates -- preserve any pre-existing
        # (perhaps post-Phase-13-written) dates if the source file no
        # longer covers that description.
        if mn is not None and mx is not None:
            if new_it.get("min_date") != mn or new_it.get("max_date") != mx:
                patched += 1
            new_it["min_date"] = mn
            new_it["max_date"] = mx
        else:
            # Ensure keys are at least present (empty) so downstream code
            # has a predictable shape.
            new_it.setdefault("min_date", "")
            new_it.setdefault("max_date", "")
        new_items.append(new_it)
    return json.dumps(new_items), patched, len(items)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", required=True,
        help="Path to the Tableau export (.csv or .xlsx) whose dates "
             "should be backfilled into Distinct Descriptions JSON.",
    )
    parser.add_argument(
        "--db", default="dist/ContractEngine/data/engine.db",
        help="SQLite engine.db to patch. Default: bundle's prod DB.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would change without writing.",
    )
    args = parser.parse_args()

    source_path = Path(args.source)
    if not source_path.is_file():
        print(f"ERROR: source file not found: {source_path}", file=sys.stderr)
        return 1
    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"ERROR: db not found: {db_path}", file=sys.stderr)
        return 1

    print(f"Reading source: {source_path}")
    df = parse_tableau_export(source_path, filename=source_path.name)
    print(f"  parsed rows: {len(df)}")

    print("Building (campus, dept, acct, vendor, desc) -> (min_date, max_date) index...")
    date_index = _build_date_index(df)
    print(f"  distinct buckets: {len(date_index)}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        '''SELECT id, "Group Key", "Distinct Descriptions JSON"
           FROM "Needs Tagging"
           WHERE COALESCE("Distinct Descriptions JSON", '') NOT IN ('', '[]')'''
    ).fetchall()
    print(f"Needs Tagging rows with JSON to inspect: {len(rows)}")

    rows_touched = 0
    buckets_patched = 0
    buckets_total = 0
    for r in rows:
        new_json, patched, total = _patch_json(
            r["Distinct Descriptions JSON"], r["Group Key"], date_index,
        )
        buckets_total += total
        if patched == 0:
            continue
        buckets_patched += patched
        rows_touched += 1
        if args.dry_run:
            print(f"  [dry] would patch {patched}/{total} buckets in row id={r['id']}  ({r['Group Key']})")
        else:
            conn.execute(
                'UPDATE "Needs Tagging" SET "Distinct Descriptions JSON" = ? WHERE id = ?',
                (new_json, r["id"]),
            )

    if not args.dry_run:
        conn.commit()
    conn.close()

    print()
    print(f"{'[dry-run] ' if args.dry_run else ''}Done.")
    print(f"  rows touched:    {rows_touched}")
    print(f"  buckets patched: {buckets_patched} / {buckets_total} total")
    print(f"  source: {source_path.name}")
    print(f"  db:     {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
