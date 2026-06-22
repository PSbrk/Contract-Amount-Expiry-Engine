"""Audit: are any Dashboard rows attributing transactions whose Date is
outside the contract's [Start, Due] term?

Background: Phase 13 fixed the Vendor Conflicts AUTO-SUGGEST to respect
dates, but attribution itself has three paths that bypass the date check
when narrowing produces a single candidate:

  1. Learned-Mapping pin-by-gid       (attribution.py ~L484)
  2. Learned-Mapping single same-name (attribution.py ~L487)
  3. Campus narrow with one survivor  (attribution.py ~L359)

If any of those paths fire on a row whose Date is outside the surviving
contract's term, the row gets attributed anyway and the Dashboard's
"Spent so far" includes out-of-term spend.

This script re-runs attribution against the source Tableau export using
the engine's Dashboard rows as the contract list, then for every row
attributed to a contract (status auto/learned) flags whether the row's
Date is inside the contract's [Start, Due] window. Outputs a per-contract
breakdown of dollars-in-term vs dollars-out-of-term so the operator can
see the blast radius before deciding to fix attribution + backfill.

Usage:
    python tools/audit_dashboard_out_of_term.py \\
        --source "C:\\Users\\philip.seabrook\\Downloads\\Transactions.csv" \\
        [--db PATH] [--show-rows N]

Default --db is dist/ContractEngine/data/engine.db (bundle's prod DB).
--show-rows N: print up to N example out-of-term rows per contract.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from engine import attribution, campus_map  # noqa: E402
from engine.asana_contracts import Contract  # noqa: E402
from engine.ingest import parse_tableau_export  # noqa: E402
from engine.sqlite_client import (  # noqa: E402
    load_campus_map_overrides,
    load_learned_mappings,
    load_vendor_aliases,
)


def _date_or_none(v) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _contracts_from_dashboard(conn: sqlite3.Connection) -> list[Contract]:
    rows = conn.execute('SELECT * FROM "Dashboard"').fetchall()
    contracts: list[Contract] = []
    for r in rows:
        d = dict(r)
        # Campus Set is a comma-separated string of Asana option names.
        cset = (d.get("Campus Set") or "").strip()
        options = frozenset(s.strip() for s in cset.split(",") if s.strip())
        contracts.append(Contract(
            gid=d["Asana Task GID"],
            name=d.get("Contract") or "",
            campus_options=options,
            contract_amount=float(d["Contract Amount"]) if d.get("Contract Amount") is not None else None,
            target_start=_date_or_none(d.get("Start")),
            due_on=_date_or_none(d.get("Due")),
            status=d.get("Status"),
            expire_countdown=None,
            pm_email=d.get("PM Email"),
            section_name="Active - Compliant",
            contract_reason_text=d.get("Contract Reason Text"),
        ))
    return contracts


def _row_date_iso(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    ts = pd.to_datetime(v, errors="coerce")
    if pd.isna(ts):
        return ""
    return ts.date().isoformat()


def _within_term(row_iso: str, c: Contract) -> bool | None:
    """Returns True if row date is inside [start, due], False if outside,
    None if either side is unknown (can't judge)."""
    if not row_iso:
        return None
    if c.target_start is None and c.due_on is None:
        return None
    rd = _date_or_none(row_iso)
    if rd is None:
        return None
    if c.target_start is not None and rd < c.target_start:
        return False
    if c.due_on is not None and rd > c.due_on:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--db", default="dist/ContractEngine/data/engine.db")
    parser.add_argument("--show-rows", type=int, default=3,
                        help="Print up to N example out-of-term rows per contract.")
    args = parser.parse_args()

    source_path = Path(args.source)
    if not source_path.is_file():
        print(f"ERROR: source not found: {source_path}", file=sys.stderr)
        return 1
    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"ERROR: db not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    print(f"Loading Dashboard contracts from {db_path}...")
    contracts = _contracts_from_dashboard(conn)
    contracts_by_gid = {c.gid: c for c in contracts}
    print(f"  {len(contracts)} contracts")

    print("Loading aliases / campus map / learned mappings from DB...")
    aliases = load_vendor_aliases(conn)
    forward_overrides, drop_override = load_campus_map_overrides(conn)
    learned = load_learned_mappings(conn)
    print(f"  {len(aliases)} aliases, "
          f"{len(forward_overrides)} crosswalk overrides, "
          f"drop_override={drop_override is not None}, "
          f"{len(learned)} learned-mapping keys")

    print(f"Parsing source: {source_path}")
    df = parse_tableau_export(source_path, filename=source_path.name)
    # Apply the same in-scope filter the engine uses, so the attribution
    # is comparable to what wrote the Dashboard. We replicate the engine's
    # in-scope logic by importing the function.
    from engine.filters import in_scope
    df = in_scope(df)
    print(f"  in-scope rows: {len(df)}")

    print("Re-running attribution...")
    run = attribution.attribute(
        df, contracts, aliases=aliases,
        crosswalk=campus_map.build(forward_overrides, drop_override),
        learned_mappings=learned,
    )
    # run.row_gids is now a POSITIONAL tuple aligned to the in-scope df rows
    # (one gid per row, None for unattributed). Iterate by position rather
    # than by Record No, which is not unique in the export.
    row_gids = run.row_gids
    print(f"  attributed rows: {sum(1 for g in row_gids if g)}")

    # Cross-check each attributed row's date against its contract's term.
    in_term_amt: dict[str, float] = defaultdict(float)
    out_term_amt: dict[str, float] = defaultdict(float)
    in_term_n: dict[str, int] = defaultdict(int)
    out_term_n: dict[str, int] = defaultdict(int)
    examples: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    no_term_amt: dict[str, float] = defaultdict(float)
    no_term_n: dict[str, int] = defaultdict(int)

    for i, gid in enumerate(row_gids):
        if not gid:
            continue
        c = contracts_by_gid.get(gid)
        if c is None:
            continue
        row = df.iloc[i]
        rec_no = str(row.get("Record No") or "")
        row_iso = _row_date_iso(row.get("Date"))
        try:
            amt = float(row.get("Amount") or 0.0)
        except (TypeError, ValueError):
            amt = 0.0

        verdict = _within_term(row_iso, c)
        if verdict is True:
            in_term_amt[gid] += amt
            in_term_n[gid] += 1
        elif verdict is False:
            out_term_amt[gid] += amt
            out_term_n[gid] += 1
            if len(examples[gid]) < args.show_rows:
                examples[gid].append((rec_no, row_iso, amt))
        else:
            no_term_amt[gid] += amt
            no_term_n[gid] += 1

    affected = sorted(
        ((g, out_term_amt[g], out_term_n[g], in_term_amt[g], in_term_n[g])
         for g in out_term_amt
         if abs(out_term_amt[g]) > 0.005),
        key=lambda t: abs(t[1]), reverse=True,
    )

    print()
    print("=" * 90)
    print(f"AUDIT RESULTS: {len(affected)} contract(s) have attributed transactions "
          f"with Date OUTSIDE [Start, Due]")
    print("=" * 90)
    if not affected:
        print("Clean — every attributed transaction's date is inside its contract's term.")
        return 0

    grand_out_amt = sum(o for _, o, _, _, _ in affected)
    grand_out_n = sum(n for _, _, n, _, _ in affected)
    print(f"Total out-of-term dollars attributed: ${grand_out_amt:,.2f} across {grand_out_n} rows")
    print()
    for gid, out_amt, out_n, in_amt, in_n in affected:
        c = contracts_by_gid[gid]
        print(f"  {c.name}  [gid {gid}]")
        print(f"    term      : {c.target_start} -> {c.due_on}")
        print(f"    in-term   : ${in_amt:>12,.2f}  ({in_n} rows)")
        print(f"    OUT of term: ${out_amt:>12,.2f}  ({out_n} rows)   <-- INFLATES Spent so far")
        if examples[gid]:
            print(f"    examples  :")
            for rn, dt, amt in examples[gid]:
                print(f"      record {rn}  date {dt}  ${amt:,.2f}")
        print()

    # Optional: also surface "unknown term" attributions where we can't judge.
    no_term_total_amt = sum(no_term_amt.values())
    if no_term_total_amt:
        print(f"(Indeterminate: ${no_term_total_amt:,.2f} attributed to contracts with no "
              f"Start AND no Due, so we can't judge in/out of term.)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
