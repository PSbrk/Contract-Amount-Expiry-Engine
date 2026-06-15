"""One-shot importer: read the JSON produced by tools/export_from_airtable.py
and insert the three operator-managed tables into a SQLite engine.db.

Idempotent by default: rows that are already present in the destination DB
are SKIPPED, not re-inserted. Use --truncate to wipe the three tables first
if you want a clean reset.

Touches ONLY the three operator-curated tables:
  - Vendor Aliases    (no UNIQUE -- dedup by (Contract Name, Aliases) tuple
                       so the operator's "multiple rows per contract" pattern
                       is preserved on re-import)
  - Campus Map        (unique on "Tableau Code")
  - Learned Mappings  (unique on "Key" = "Campus|Dept|Account No|Vendor")

NEVER touches: Inbox, Dashboard, Needs Tagging, State, Run Log. Those are
re-derived on the next --ingest and would just be noise / outdated rows.

Usage:

    python -m engine.main --provision        # create data/engine.db schema
    python -m tools.import_to_sqlite --in airtable_export.json
    # Optional clean reset:
    python -m tools.import_to_sqlite --in airtable_export.json --truncate

The result data/engine.db is then ready to ship inside the dist/ContractEngine
bundle (or stays in dev where the operator keeps working).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from engine import sqlite_client


_OPERATOR_TABLES = ("Vendor Aliases", "Campus Map", "Learned Mappings")


def _truncate_operator_tables(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {}
    for tname in _OPERATOR_TABLES:
        cur = conn.execute(f'SELECT COUNT(*) FROM "{tname}"')
        before = cur.fetchone()[0]
        conn.execute(f'DELETE FROM "{tname}"')
        counts[tname] = before
    conn.commit()
    return counts


def _import_vendor_aliases(conn, rows: list[dict]) -> dict:
    """Vendor Aliases has NO UNIQUE constraint (multi-row per contract is a
    legitimate operator pattern), so we cannot rely on IntegrityError for
    dedup. Build the set of existing (Contract Name, Aliases) tuples once,
    skip any incoming row whose tuple already exists."""
    inserted = skipped_dup = skipped_blank = 0
    # sqlite3.Row does not compare equal to a plain tuple, so explicit
    # tuple() conversion is required for the `in existing` check to work.
    existing: set[tuple[str, str]] = {
        (row["Contract Name"], row["Aliases"])
        for row in conn.execute(
            'SELECT "Contract Name", "Aliases" FROM "Vendor Aliases"'
        ).fetchall()
    }
    for row in rows:
        name = (row.get("Contract Name") or "").strip()
        if not name:
            # Vendor Aliases without an owning contract is meaningless
            # and would also collide on empty-string Contract Name if
            # we ever add a UNIQUE constraint later. Skip + count.
            skipped_blank += 1
            continue
        aliases = row.get("Aliases", "") or ""
        if (name, aliases) in existing:
            skipped_dup += 1
            continue
        sqlite_client.insert_vendor_alias(
            conn,
            contract_name=name,
            aliases=aliases,
            notes=row.get("Notes", "") or "",
        )
        existing.add((name, aliases))
        inserted += 1
    return {"inserted": inserted, "skipped_dup": skipped_dup, "skipped_blank": skipped_blank}


def _import_campus_map(conn, rows: list[dict]) -> dict:
    inserted = skipped_dup = skipped_blank = 0
    for row in rows:
        code = (row.get("Tableau Code") or "").strip()
        if not code:
            skipped_blank += 1
            continue
        try:
            sqlite_client.insert_campus_map(
                conn,
                tableau_code=code,
                asana_option_names=row.get("Asana Option Names", "") or "",
                drop=bool(row.get("Drop", False)),
                notes=row.get("Notes", "") or "",
            )
            inserted += 1
        except sqlite3.IntegrityError:
            skipped_dup += 1
    return {"inserted": inserted, "skipped_dup": skipped_dup, "skipped_blank": skipped_blank}


def _import_learned_mappings(conn, rows: list[dict]) -> dict:
    inserted = skipped_dup = skipped_blank = 0
    for row in rows:
        campus = (row.get("Campus") or "").strip()
        dept = (row.get("Dept") or "").strip()
        account_no = (row.get("Account No") or "").strip()
        vendor = (row.get("Vendor") or "").strip()
        contract_name = (row.get("Contract Name") or "").strip()
        if not contract_name or not all((campus, dept, account_no, vendor)):
            # Same skip semantics as the pre-migration loader (legacy
            # airtable_client.load_learned_mappings) -- a partial row would
            # produce a meaningless attribution rule.
            skipped_blank += 1
            continue
        # Synthesize Key the same way the engine does (engine.attribution
        # group_key + engine.sqlite_client._UNIQUE_FIELDS). Any Key column
        # from the Airtable export is intentionally ignored so the format
        # is guaranteed canonical.
        key = f"{campus}|{dept}|{account_no}|{vendor}"
        try:
            sqlite_client.insert_learned_mapping(
                conn,
                key=key,
                campus=campus,
                dept=dept,
                account_no=account_no,
                vendor=vendor,
                contract_name=contract_name,
                learned_at=row.get("Learned At", "") or "",
                notes=row.get("Notes", "") or "",
            )
            inserted += 1
        except sqlite3.IntegrityError:
            skipped_dup += 1
    return {"inserted": inserted, "skipped_dup": skipped_dup, "skipped_blank": skipped_blank}


def run_import(payload: dict, *, db_path: Path, truncate: bool) -> dict:
    """Apply the import to db_path. Returns a stats dict.

    db_path must point at an existing engine.db whose schema is already in
    place (run --provision first). This function does NOT call ensure_schema
    itself because the operator may have an in-progress SQLite database
    they want to import INTO -- silently re-running ensure_schema there
    would be a no-op but still feels like overreach.
    """
    if not Path(db_path).exists():
        raise FileNotFoundError(
            f"SQLite database not found at {db_path}. Run "
            "`python -m engine.main --provision` first to create it."
        )

    conn = sqlite_client.get_db_connection(db_path)
    try:
        stats: dict = {}
        if truncate:
            stats["truncated"] = _truncate_operator_tables(conn)

        stats["vendor_aliases"]   = _import_vendor_aliases(conn, payload.get("vendor_aliases", []))
        stats["campus_map"]       = _import_campus_map(conn, payload.get("campus_map", []))
        stats["learned_mappings"] = _import_learned_mappings(conn, payload.get("learned_mappings", []))
        return stats
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--in", dest="input_path", required=True,
        help="Path to the JSON file produced by tools/export_from_airtable.py.",
    )
    parser.add_argument(
        "--db", default=str(sqlite_client.DEFAULT_DB_PATH),
        help=f"SQLite database to insert into (default: {sqlite_client.DEFAULT_DB_PATH}).",
    )
    parser.add_argument(
        "--truncate", action="store_true",
        help=(
            "Wipe the three operator tables before inserting. Use this for a "
            "clean reset; default is skip-existing (safe to re-run)."
        ),
    )
    args = parser.parse_args(argv)

    in_path = Path(args.input_path)
    try:
        payload = json.loads(in_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"FATAL: input file not found at {in_path}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"FATAL: input file at {in_path} is not valid JSON: {exc}", file=sys.stderr)
        return 2

    try:
        stats = run_import(payload, db_path=Path(args.db), truncate=args.truncate)
    except FileNotFoundError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    print(f"Imported into {args.db}:")
    if "truncated" in stats:
        print("  Pre-import truncate:")
        for tname, n in stats["truncated"].items():
            print(f"    {tname:<20s} deleted {n} row(s)")
    for tname_key in ("vendor_aliases", "campus_map", "learned_mappings"):
        s = stats[tname_key]
        label = tname_key.replace("_", " ").title()
        line = f"  {label:<20s} inserted {s['inserted']:>5}"
        skips = []
        if s.get("skipped_dup"):
            skips.append(f"{s['skipped_dup']} duplicate")
        if s.get("skipped_blank"):
            skips.append(f"{s['skipped_blank']} blank required")
        if skips:
            line += f"  (skipped: {', '.join(skips)})"
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
