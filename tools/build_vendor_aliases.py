"""Propose (and optionally apply) Vendor Aliases by fuzzy-matching the Tableau
vendor names in an export against open Asana contract names.

Attribution matches a Tableau vendor to a contract via rapidfuzz WRatio >= 92.
Many real contracts sit just below that (name variants: 'BeCleanOKC' vs
'Be Clean OKC', 'Summit Fire & Security' vs 'Summit Fire'). An alias bridges
the gap. This tool finds those near-misses.

SAFETY: only AUTO-APPLIES a vendor->contract alias when the best contract match
is a CLEAR winner -- score in [min-score, 92) AND ahead of the runner-up by
>= margin. Anything ambiguous (two contracts score close) is HELD for human
review, never guessed, because a wrong alias mis-attributes spend.

  python tools/build_vendor_aliases.py                 # preview only
  python tools/build_vendor_aliases.py --execute        # write the confident aliases to the DB
  python tools/build_vendor_aliases.py --export PATH     # vendors source (default: latest processed)
"""

from __future__ import annotations

try:
    import truststore; truststore.inject_into_ssl()
except ImportError:
    pass

import argparse
import csv
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
DEFAULT_BUNDLE = REPO / "dist" / "ContractEngine"


def _load_pat(secrets_path: Path) -> None:
    if os.environ.get("ASANA_PAT", "").strip():
        return
    for line in Path(secrets_path).read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("="); os.environ.setdefault(k.strip(), v.strip())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--export", default=None,
                    help="Tableau export whose vendors to alias (default: newest in data/processed).")
    ap.add_argument("--db", default=str(DEFAULT_BUNDLE / "data" / "engine.db"))
    ap.add_argument("--secrets", default=str(DEFAULT_BUNDLE / "config" / "secrets.env"))
    ap.add_argument("--min-score", type=int, default=82)
    ap.add_argument("--margin", type=int, default=6,
                    help="Best must beat the runner-up by this many points to auto-apply.")
    ap.add_argument("--execute", action="store_true",
                    help="Write the EXACT-tier aliases to the DB.")
    ap.add_argument("--review-csv", metavar="PATH",
                    help="Write the REVIEW + AMBIGUOUS candidates to a CSV with an "
                         "'approve' column for you to mark y/n.")
    ap.add_argument("--apply-csv", metavar="PATH",
                    help="Read a filled review CSV and insert aliases for rows where "
                         "approve is y/yes/x. (Ignores everything else; safe to re-run.)")
    args = ap.parse_args(argv)

    # Apply a filled review sheet: insert aliases for approved rows. No Asana
    # call needed -- pure CSV -> DB.
    if args.apply_csv:
        today = date.today().isoformat()
        conn = sqlite3.connect(args.db)
        conn.row_factory = sqlite3.Row
        existing = {r["Contract Name"] for r in conn.execute('SELECT "Contract Name" FROM "Vendor Aliases"')}
        by_contract: dict[str, list[str]] = {}
        with open(args.apply_csv, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                if (row.get("approve") or "").strip().lower() not in ("y", "yes", "x", "true", "1"):
                    continue
                vendor = (row.get("tableau_vendor") or "").strip()
                contract = (row.get("proposed_contract") or "").strip()
                if vendor and contract:
                    by_contract.setdefault(contract, []).append(vendor)
        applied = 0
        for contract, vs in by_contract.items():
            note = f"approved review alias {today}"
            if contract in existing:
                cur = conn.execute('SELECT "Aliases" FROM "Vendor Aliases" WHERE "Contract Name" = ?', (contract,)).fetchone()
                merged = [a for a in (cur["Aliases"] or "").splitlines() if a.strip()] + vs
                conn.execute('UPDATE "Vendor Aliases" SET "Aliases" = ? WHERE "Contract Name" = ?',
                             ("\n".join(dict.fromkeys(merged)), contract))
            else:
                conn.execute('INSERT INTO "Vendor Aliases" ("Contract Name","Aliases","Notes") VALUES (?,?,?)',
                             (contract, "\n".join(vs), note))
            applied += len(vs)
        conn.commit()
        print(f"Applied {applied} approved alias(es) across {len(by_contract)} contracts from {Path(args.apply_csv).name}.")
        return 0

    export = args.export
    if export is None:
        proc = DEFAULT_BUNDLE / "data" / "processed"
        cands = sorted(proc.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not cands:
            sys.exit("no processed export found; pass --export PATH")
        export = str(cands[0])

    _load_pat(Path(args.secrets))
    import re
    from engine.ingest import parse_tableau_export
    from engine.filters import in_scope
    from engine import asana_contracts, asana_client

    # Token-subset matcher. WRatio mis-pairs vendors that merely share filler
    # words (LLC/Plumbing/Fire); requiring one name's DISTINCTIVE tokens to be a
    # subset of the other's only keeps true expansions/abbreviations.
    _CORP = {"llc", "inc", "incorporated", "corp", "corporation", "co", "ltd",
             "company", "dba", "the", "and", "of", "an", "a"}
    _tok = re.compile(r"[a-z0-9]+")

    def toks(s: str) -> set[str]:
        return {t for t in _tok.findall(s.lower()) if t not in _CORP and len(t) >= 2}

    def distinctive_enough(small: set[str]) -> bool:
        # A single short token (e.g. {long}, {tlc}) is too generic to alias on.
        return len(small) >= 2 or any(len(t) >= 6 for t in small)

    api = asana_client.get_api_client()
    contracts = [c for c in asana_contracts.load_open_contracts(api) if c.name]
    ctoks = {c.name: toks(c.name) for c in contracts}
    df = in_scope(parse_tableau_export(Path(export)))
    col = "Vendor" if "Vendor" in df.columns else "Vendor "
    vendors = sorted({str(v).strip() for v in df[col].dropna().unique() if str(v).strip()})

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    existing = {r["Contract Name"] for r in conn.execute('SELECT "Contract Name" FROM "Vendor Aliases"')}

    confident: dict[str, list[str]] = {}             # EXACT after normalization -> auto-apply
    review: list[tuple[str, str]] = []               # (vendor, contract) subset, single match
    ambiguous: list[tuple[int, str, str, str]] = []  # (n_matches, vendor, c1, c2)
    for v in vendors:
        vt = toks(v)
        if not vt:
            continue
        exact = [n for n, ct in ctoks.items() if ct and ct == vt and v.casefold() != n.casefold()]
        if exact:
            confident.setdefault(exact[0], []).append(v)
            continue
        matches = []
        for name, ct in ctoks.items():
            if not ct or v.casefold() == name.casefold():
                continue
            small = ct if len(ct) <= len(vt) else vt
            big = vt if small is ct else ct
            if small < big and distinctive_enough(small):
                matches.append(name)
        if len(matches) == 1:
            review.append((v, matches[0]))
        elif len(matches) > 1:
            ambiguous.append((len(matches), v, matches[0], matches[1]))

    n_aliases = sum(len(a) for a in confident.values())
    print(f"export: {Path(export).name} | vendors: {len(vendors)} | contracts: {len(contracts)}")
    print(f"\nEXACT (identical after dropping punctuation + LLC/Inc/Corp) — SAFE to auto-apply: "
          f"{n_aliases} aliases / {len(confident)} contracts")
    for contract in sorted(confident):
        for v in confident[contract]:
            print(f"  {contract}  <-  {v}")
    print(f"\nREVIEW (one name contains the other — likely right, NEEDS YOUR OK): {len(review)}")
    for v, c in sorted(review):
        print(f"  {c}  <-  {v}")
    print(f"\nAMBIGUOUS (matches >1 contract — definitely review): {len(ambiguous)}")
    for n, v, c1, c2 in sorted(ambiguous, reverse=True):
        print(f"  {v}  ->  {n} contracts incl. {c1} / {c2}")

    if args.review_csv:
        with open(args.review_csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["approve", "tableau_vendor", "proposed_contract", "alternatives", "tier"])
            for v, c in sorted(review):
                w.writerow(["", v, c, "", "review"])
            for n, v, c1, c2 in sorted(ambiguous, reverse=True):
                # proposed_contract left for you to choose; alternatives lists the rest
                w.writerow(["", v, "", f"{c1} | {c2} | (+{n-2} more)" if n > 2 else f"{c1} | {c2}", "ambiguous"])
        print(f"\nWrote review sheet -> {args.review_csv}")
        print("  Mark 'y' in the approve column for correct rows; for 'ambiguous' rows also")
        print("  fill proposed_contract. Then: python tools/build_vendor_aliases.py --apply-csv <that file>")

    if not args.execute:
        print("\n(preview only; re-run with --execute to write the confident aliases)")
        return 0

    today = date.today().isoformat()
    applied = 0
    for contract, vs in confident.items():
        if contract in existing:
            print(f"  skip (already has a Vendor Aliases row): {contract}")
            continue
        conn.execute(
            'INSERT INTO "Vendor Aliases" ("Contract Name", "Aliases", "Notes") VALUES (?, ?, ?)',
            (contract, "\n".join(vs), f"auto fuzzy alias {today} (>= {args.min_score}, margin {args.margin})"),
        )
        applied += len(vs)
    conn.commit()
    print(f"\nApplied {applied} alias(es) across {len(confident)} contracts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
