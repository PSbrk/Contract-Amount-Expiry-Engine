"""One-shot exporter: dump the three operator-managed Airtable tables to a JSON
file that tools/import_to_sqlite.py can ingest.

Why this exists: when we migrated off Airtable in Phase 6b, the SQLite database
starts empty. Inbox / Dashboard / Run Log / State / Needs Tagging are all
re-derivable on the next --ingest, but the three operator-curated tables --
Vendor Aliases, Campus Map, Learned Mappings -- hold accumulated hand work
that we do NOT want to lose.

This script reads those three tables via the Airtable API and writes a single
JSON file in the shape import_to_sqlite.py expects.

Usage:

    pip install pyairtable
    export AIRTABLE_PAT=patABC...           # PAT scoped to the source base
    export AIRTABLE_BASE_ID=appXYZ...       # appId of the source base
    python -m tools.export_from_airtable --out airtable_export.json

    python -m tools.import_to_sqlite --in airtable_export.json --db data/engine.db

This script is intentionally NOT installed into the bundle -- it runs on the
dev machine, once, against the still-live Airtable base. Once the JSON is
generated, the bundle ships with a pre-seeded data/engine.db and never needs
Airtable again.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _split_multiline_list(raw) -> list[str]:
    """Airtable multilineText fields with comma OR newline separators.
    Mirrors legacy/airtable_client.py's helper so behavior matches the
    pre-migration parser."""
    if not raw:
        return []
    parts = []
    for line in str(raw).splitlines():
        for piece in line.split(","):
            piece = piece.strip()
            if piece:
                parts.append(piece)
    return parts


def _load_vendor_aliases(table) -> list[dict]:
    """Return [{Contract Name, Aliases, Notes}, ...] preserving newline-joined
    Aliases (so the import round-trips into the SQLite multilineText column
    with the exact same content)."""
    rows = []
    for record in table.all():
        fields = record.get("fields") or {}
        contract_name = (fields.get("Contract Name") or "").strip()
        if not contract_name:
            continue
        rows.append({
            "Contract Name": contract_name,
            # Preserve the raw cell so the SQLite copy is byte-identical to
            # what the operator typed; legacy code's split-then-rejoin would
            # discard intentional whitespace.
            "Aliases": fields.get("Aliases") or "",
            "Notes": fields.get("Notes") or "",
        })
    return rows


def _load_campus_map(table) -> list[dict]:
    rows = []
    for record in table.all():
        fields = record.get("fields") or {}
        code = (fields.get("Tableau Code") or "").strip()
        if not code:
            continue
        rows.append({
            "Tableau Code": code,
            "Asana Option Names": fields.get("Asana Option Names") or "",
            "Drop": bool(fields.get("Drop", False)),
            "Notes": fields.get("Notes") or "",
        })
    return rows


def _load_learned_mappings(table) -> list[dict]:
    rows = []
    for record in table.all():
        fields = record.get("fields") or {}
        campus = (fields.get("Campus") or "").strip()
        dept = (fields.get("Dept") or "").strip()
        account_no = (fields.get("Account No") or "").strip()
        vendor = (fields.get("Vendor") or "").strip()
        contract_name = (fields.get("Contract Name") or "").strip()
        if not contract_name or not all((campus, dept, account_no, vendor)):
            # Same skip semantics as the pre-migration loader -- a partial
            # row would produce a meaningless attribution rule.
            continue
        rows.append({
            "Campus": campus,
            "Dept": dept,
            "Account No": account_no,
            "Vendor": vendor,
            "Contract Name": contract_name,
            # Key is synthesized at import time from the four components
            # using the same separator convention the engine uses; we don't
            # need (or trust) any Key stored in Airtable.
            "Learned At": fields.get("Learned At") or "",
            "Notes": fields.get("Notes") or "",
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", required=True,
        help="Path to write the JSON dump (e.g. airtable_export.json).",
    )
    parser.add_argument(
        "--pat",
        help="Airtable PAT. Falls back to env AIRTABLE_PAT.",
    )
    parser.add_argument(
        "--base-id",
        help="Airtable base id. Falls back to env AIRTABLE_BASE_ID.",
    )
    args = parser.parse_args(argv)

    pat = args.pat or os.environ.get("AIRTABLE_PAT", "").strip()
    base_id = args.base_id or os.environ.get("AIRTABLE_BASE_ID", "").strip()
    if not pat or not base_id:
        print(
            "FATAL: AIRTABLE_PAT and AIRTABLE_BASE_ID must be set (env or --pat/--base-id).",
            file=sys.stderr,
        )
        return 2

    try:
        from pyairtable import Api
    except ImportError:
        print(
            "FATAL: pyairtable is not installed. Run `pip install pyairtable` "
            "in this venv to install it for this one-shot export.",
            file=sys.stderr,
        )
        return 2

    api = Api(pat)
    base = api.base(base_id)

    payload = {
        "vendor_aliases":   _load_vendor_aliases(base.table("Vendor Aliases")),
        "campus_map":       _load_campus_map(base.table("Campus Map")),
        "learned_mappings": _load_learned_mappings(base.table("Learned Mappings")),
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Wrote {out_path}")
    print(f"  Vendor Aliases:   {len(payload['vendor_aliases']):>5} row(s)")
    print(f"  Campus Map:       {len(payload['campus_map']):>5} row(s)")
    print(f"  Learned Mappings: {len(payload['learned_mappings']):>5} row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
