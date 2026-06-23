"""Run the Asana write for ONE contract -- preview by default, --execute to write.

For the controlled Asana go-live: validate the write path on a single contract
before flipping DRY_RUN_ASANA for the whole project. Pushes that contract's
CURRENT stored Dashboard values (the five custom fields: Spent so far, % Spent,
Spending Rate, Spending Rate Alarm, Alarms) to its Asana task, reusing
engine.asana_writer.apply_writes with test_contract_gid scoping so no other
contract and no non-custom-field can ever be touched.

  # list live contracts whose Asana values differ from the Dashboard:
  python tools/asana_write_one.py

  # preview the exact delta for one contract (no write):
  python tools/asana_write_one.py --gid 1234567890

  # perform the single live write, then re-read to confirm:
  python tools/asana_write_one.py --gid 1234567890 --execute

Defaults read the live bundle's data/engine.db and config/secrets.env (the real
operator data + PAT). This never flips the global DRY_RUN_ASANA; the write is a
scoped one-off gated by --execute.
"""

from __future__ import annotations

# Inject the OS trust store BEFORE any import can build an SSLContext from
# certifi -- the corporate (life.church) TLS-inspecting proxy re-signs with a
# CA that lives only in the Windows cert store. Mirrors engine.main. Without
# this, every https call to Asana fails "self-signed certificate in chain".
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import argparse
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DEFAULT_BUNDLE = REPO / "dist" / "ContractEngine"


def _load_pat(secrets_path: Path) -> None:
    """Put ASANA_PAT into the environment from secrets.env if not already set.
    Only parses KEY=VALUE lines; never echoes the value."""
    if os.environ.get("ASANA_PAT", "").strip():
        return
    if not secrets_path.is_file():
        sys.exit(f"ASANA_PAT not in env and {secrets_path} not found.")
    for line in secrets_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())
    if not os.environ.get("ASANA_PAT", "").strip():
        sys.exit(f"ASANA_PAT not found in {secrets_path}.")


def _dashboard_row_for(conn: sqlite3.Connection, gid: str):
    """Reconstruct a DashboardRow from the stored Dashboard row. Only the five
    writable fields matter to the diff; the rest get harmless placeholders the
    writer ignores."""
    from engine.compute import DashboardRow

    conn.row_factory = sqlite3.Row
    r = conn.execute(
        '''SELECT "Contract", "Asana Task GID", "Campus Set", "Contract Amount",
                  "Spent so far", "% Spent", "Spending Rate",
                  "Spending Rate Alarm", "Alarms"
             FROM "Dashboard" WHERE "Asana Task GID" = ?''',
        (gid,),
    ).fetchone()
    if r is None:
        return None
    return DashboardRow(
        contract_name=r["Contract"] or "",
        asana_task_gid=r["Asana Task GID"],
        campus_set=r["Campus Set"] or "",
        contract_amount=r["Contract Amount"],
        spent_so_far=r["Spent so far"] if r["Spent so far"] is not None else 0.0,
        pct_spent=r["% Spent"],
        spending_rate=r["Spending Rate"],
        spending_rate_alarm=r["Spending Rate Alarm"],
        alarms=r["Alarms"] or "Clear",
        start=date.today(), due=None, status=None, pm_email=None,
        last_updated=date.today(),
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gid", help="Asana task GID of the one contract to write.")
    ap.add_argument("--all", action="store_true",
                    help="Write EVERY live-gate contract with a pending delta "
                         "(one contract-load, then a write per changed task). "
                         "Dry-run summary unless --execute.")
    ap.add_argument("--execute", action="store_true",
                    help="Perform the live write. Without this, dry-run preview only.")
    ap.add_argument("--db", default=str(DEFAULT_BUNDLE / "data" / "engine.db"),
                    help="engine.db to read Dashboard values from.")
    ap.add_argument("--secrets", default=str(DEFAULT_BUNDLE / "config" / "secrets.env"),
                    help="secrets.env to read ASANA_PAT from.")
    args = ap.parse_args(argv)

    _load_pat(Path(args.secrets))
    from engine import asana_client, asana_contracts, asana_writer

    api = asana_client.get_api_client()
    contracts = {c.gid: c for c in asana_contracts.load_open_contracts(api)}

    conn = sqlite3.connect(args.db)

    # --all: write (or preview) every live-gate contract with a pending delta.
    if args.all:
        conn.row_factory = sqlite3.Row
        gids = [r["Asana Task GID"]
                for r in conn.execute('SELECT "Asana Task GID" FROM "Dashboard"').fetchall()]
        wrote = nochange = skipped = 0
        errors: list[tuple[str, str, str]] = []
        verb = "WRITE" if args.execute else "DRY-RUN"
        print(f"[{verb}] scanning {len(gids)} Dashboard contracts...\n")
        for gid in gids:
            c = contracts.get(gid)
            dash = _dashboard_row_for(conn, gid)
            if c is None or dash is None:
                skipped += 1
                continue
            res = asana_writer.apply_writes(
                api, dash, c, dry_run=not args.execute, test_contract_gid=None,
            )
            if res.error:
                errors.append((c.name, gid, res.error))
                print(f"  ERROR {c.name} [{gid}]: {res.error}")
            elif res.deltas:
                wrote += 1
                tag = "wrote" if args.execute else "would write"
                print(f"  {tag} {len(res.deltas)} field(s): {c.name}")
            else:
                nochange += 1
        print(f"\nSummary: {wrote} {'written' if args.execute else 'to write'}, "
              f"{nochange} already current, {skipped} skipped (not open in Asana), "
              f"{len(errors)} error(s).")
        if errors:
            print("\nERRORS:")
            for name, gid, err in errors:
                print(f"  {name} [{gid}]: {err}")
            return 1
        return 0

    # No gid: list live contracts whose Asana values differ from the Dashboard.
    if not args.gid:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT "Asana Task GID" FROM "Dashboard"').fetchall()
        print(f"Dashboard rows: {len(rows)}. Contracts with a pending write (Asana != Dashboard):\n")
        n = 0
        for row in rows:
            gid = row["Asana Task GID"]
            c = contracts.get(gid)
            dash = _dashboard_row_for(conn, gid)
            if c is None or dash is None:
                continue
            deltas = asana_writer.diff_dashboard_vs_current(dash, c)
            if not deltas:
                continue
            n += 1
            print(f"  {c.name}  [gid {gid}]")
            for d in deltas:
                print(f"      {d.field_name}: {d.old_value!r} -> {d.new_value!r}")
        print(f"\n{n} contract(s) would change. Re-run with --gid <GID> to preview one,"
              f" then add --execute to write it.")
        return 0

    c = contracts.get(args.gid)
    if c is None:
        sys.exit(f"gid {args.gid} is not an open contract in Asana.")
    dash = _dashboard_row_for(conn, args.gid)
    if dash is None:
        sys.exit(f"gid {args.gid} has no Dashboard row in {args.db}.")

    res = asana_writer.apply_writes(
        api, dash, c, dry_run=not args.execute, test_contract_gid=args.gid,
    )
    label = "WROTE" if (args.execute and not res.error and res.deltas) else \
            "WOULD WRITE" if res.deltas else "NO CHANGE"
    print(f"[{label}] {c.name}  [gid {args.gid}]")
    for d in res.deltas:
        print(f"    {d.field_name}: {d.old_value!r} -> {d.new_value!r}")
    if res.error:
        print(f"    ERROR: {res.error}")
        return 1

    # Confirm by re-reading the task straight from Asana after a live write.
    if args.execute and res.deltas:
        fresh = {x.gid: x for x in asana_contracts.load_open_contracts(api)}.get(args.gid)
        if fresh is not None:
            print("  confirmed in Asana now:")
            print(f"    Spent so far={fresh.current_spent_so_far!r}, "
                  f"% Spent={fresh.current_pct_spent!r}, "
                  f"Spending Rate={fresh.current_spending_rate!r}, "
                  f"Spending Rate Alarm={fresh.current_spending_rate_alarm!r}, "
                  f"Alarms={fresh.current_alarms!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
