"""Restore Asana custom-field values from an ingest log's write summary.

Undoes a bad LIVE ingest: parses the `[ ok] Name (gid)` / `Field: old->new`
block an ingest writes when DRY_RUN_ASANA is off, and writes each field's OLD
value back to Asana -- returning the five spend custom fields to their exact
pre-ingest state. Preview by default; --execute to write.

  python tools/asana_restore_from_log.py                 # preview from today's ingest log
  python tools/asana_restore_from_log.py --execute        # write the old values back
  python tools/asana_restore_from_log.py --log PATH        # a specific log file

Only touches the gids in the log's most-recent write summary, and only the
five known fields -- nothing else.
"""

from __future__ import annotations

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import argparse
import re
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DEFAULT_BUNDLE = REPO / "dist" / "ContractEngine"

_OK_RE = re.compile(r"\[ ok\]\s+(.*?)\s+\((\d+)\)\s*$")


def _load_pat(secrets_path: Path) -> None:
    import os
    if os.environ.get("ASANA_PAT", "").strip():
        return
    if not secrets_path.is_file():
        sys.exit(f"ASANA_PAT not in env and {secrets_path} not found.")
    for line in secrets_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
    if not os.environ.get("ASANA_PAT", "").strip():
        sys.exit(f"ASANA_PAT not found in {secrets_path}.")


def _parse_old(token: str):
    """Parse the OLD side of 'old->new'. 'None' -> None; quoted -> enum option
    name; else -> float."""
    t = token.strip()
    if t == "None":
        return None
    if len(t) >= 2 and t[0] == "'" and t[-1] == "'":
        return t[1:-1]
    return float(t)


def _parse_latest_summary(log_text: str) -> dict[str, tuple[str, dict]]:
    """Return {gid: (name, {field_name: old_value})} from the LAST write
    summary block in the log."""
    # Restrict to the last run's summary so re-runs don't resurrect stale gids.
    marker = "Asana writes [LIVE]"
    idx = log_text.rfind(marker)
    block = log_text[idx:] if idx != -1 else log_text
    lines = block.splitlines()
    out: dict[str, tuple[str, dict]] = {}
    pending_gid = pending_name = None
    for line in lines:
        m = _OK_RE.search(line)
        if m:
            pending_name, pending_gid = m.group(1), m.group(2)
            continue
        if pending_gid and "->" in line:
            fields: dict = {}
            for chunk in line.strip().split(", "):
                if ": " not in chunk:
                    continue
                fname, _, change = chunk.partition(": ")
                old_str = change.split("->")[0]
                fields[fname.strip()] = _parse_old(old_str)
            if fields:
                out[pending_gid] = (pending_name, fields)
            pending_gid = pending_name = None
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", default=str(DEFAULT_BUNDLE / "logs" / f"ingest-{date.today().isoformat()}.log"),
                    help="Ingest log to restore from (default: today's bundle log).")
    ap.add_argument("--execute", action="store_true",
                    help="Write the old values back. Without this, preview only.")
    ap.add_argument("--secrets", default=str(DEFAULT_BUNDLE / "config" / "secrets.env"))
    args = ap.parse_args(argv)

    log_path = Path(args.log)
    if not log_path.is_file():
        sys.exit(f"log not found: {log_path}")
    restores = _parse_latest_summary(log_path.read_text(encoding="utf-8", errors="replace"))
    if not restores:
        print("No write summary found in the log; nothing to restore.")
        return 0

    _load_pat(Path(args.secrets))
    import asana
    from engine import asana_client
    from engine.asana_writer import FieldDelta, build_custom_fields_payload

    api = asana_client.get_api_client()
    tasks_api = asana.TasksApi(api)

    verb = "RESTORE" if args.execute else "DRY-RUN"
    print(f"[{verb}] {len(restores)} contract(s) from {log_path.name}\n")
    errors = 0
    for gid, (name, fields) in restores.items():
        deltas = [FieldDelta(f, None, old) for f, old in fields.items()]
        print(f"  {name} [{gid}]")
        for f, old in fields.items():
            print(f"      {f} <- {old!r}")
        if not args.execute:
            continue
        payload = build_custom_fields_payload(deltas)
        try:
            tasks_api.update_task({"data": {"custom_fields": payload}}, gid, {"opt_fields": "gid"})
        except Exception as exc:  # noqa: BLE001
            errors += 1
            print(f"      ERROR: {type(exc).__name__}: {exc}")
    print(f"\n{'Restored' if args.execute else 'Would restore'} {len(restores)} contract(s); {errors} error(s).")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
