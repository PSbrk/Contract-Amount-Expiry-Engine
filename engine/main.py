"""Engine entry point.

Step 2 wires four modes (all read-only against Asana — Asana writes land in
Step 5):

  --audit                  verify Asana project schema matches expectations
  --provision              create/verify the 8 Airtable tables (idempotent)
  --ingest                 pull newest Inbox attachment, parse, filter, report
  --ingest-file PATH       parse a local export file (no Airtable round-trip)

Pair --provision with --dry-run to see the schema plan without applying it.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone


def _configure_logging() -> None:
    """Sets a safe default. Critically, caps urllib3 / asana / pyairtable
    loggers at INFO so a future DEBUG toggle on the engine logger cannot
    cascade into the SDKs and leak Bearer PATs via request-header logging.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.INFO)
    logging.getLogger("asana").setLevel(logging.INFO)
    logging.getLogger("pyairtable").setLevel(logging.INFO)


def _force_utf8_stdio() -> None:
    """Make sure non-ASCII characters (em-dashes, ellipses) in user-facing
    output don't get mangled to U+FFFD on Windows' cp1252 default. Best-effort
    — capsys-replaced or non-reconfigurable streams (e.g. in tests) just stay
    as-is.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Contract Amount Expiry Engine")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--audit", action="store_true",
        help="Read-only: verify Asana project schema matches the engine's "
             "expectations. Exits 0 on pass, 1 on fail.",
    )
    mode.add_argument(
        "--provision", action="store_true",
        help="Create or verify the 8 Airtable tables and their fields. "
             "Idempotent. Pair with --dry-run to print the plan only.",
    )
    mode.add_argument(
        "--ingest", action="store_true",
        help="Pull the newest unprocessed Inbox attachment from Airtable, "
             "parse it, apply scope filters, write a Run Log entry, and "
             "mark the Inbox row Processed.",
    )
    mode.add_argument(
        "--ingest-file", metavar="PATH",
        help="Parse a local Tableau export file and print the report. "
             "Skips Airtable entirely — useful for dev smoke-testing.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="With --provision: print the schema plan but make no API writes. "
             "Asana writes are gated separately by DRY_RUN_ASANA (Step 5+).",
    )
    args = parser.parse_args(argv)

    _load_dotenv()
    _configure_logging()
    _force_utf8_stdio()
    log = logging.getLogger(__name__)

    if args.audit:
        from engine.audit import main as audit_main
        return audit_main([])

    if args.provision:
        return _run_provision(dry_run=args.dry_run)

    if args.ingest:
        return _run_ingest_airtable()

    if args.ingest_file:
        return _run_ingest_file(args.ingest_file)

    log.info("no mode selected. Try `python -m engine.main --audit` or "
             "`--provision` or `--ingest` or `--ingest-file PATH`.")
    return 0


# ---------------------------------------------------------------------------
# Provision
# ---------------------------------------------------------------------------

def _run_provision(*, dry_run: bool) -> int:
    from engine.airtable_client import ensure_schema, get_api_and_base
    try:
        _, base = get_api_and_base()
    except RuntimeError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    plan = ensure_schema(base, dry_run=dry_run)
    verb = "WOULD" if dry_run else "did"

    print(f"Airtable provisioning {'plan (dry run)' if dry_run else 'complete'}.")
    if plan.tables_created:
        print(f"  {verb} create tables ({len(plan.tables_created)}):")
        for name in plan.tables_created:
            print(f"    + {name}")
    if plan.fields_added:
        print(f"  {verb} add fields ({len(plan.fields_added)}):")
        for table, field in plan.fields_added:
            print(f"    + {table}.{field}")
    if not plan.tables_created and not plan.fields_added:
        print(f"  no changes — all {len(plan.tables_already_present)} tables and "
              f"{len(plan.fields_already_present)} fields already in place.")
    return 0


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def _print_ingest_report(df, meta, kept, rejected) -> tuple[float, float]:
    from engine.filters import signed_sum
    in_total = signed_sum(kept)
    out_total = signed_sum(rejected)
    grand_total = signed_sum(df)

    print(f"Ingest — {meta.name}")
    print(f"  sha256:      {meta.hash}")
    print(f"  rows:        total {len(df):,}")
    print(f"               in-scope {len(kept):,}    out-of-scope {len(rejected):,}")
    if len(df):
        print(f"  date range:  {df['Date'].min().date()} .. {df['Date'].max().date()}")
    print(f"  signed sums:")
    print(f"      in-scope     ${in_total:>18,.2f}")
    print(f"      out-of-scope ${out_total:>18,.2f}")
    print(f"      total        ${grand_total:>18,.2f}")
    if len(kept):
        print()
        print(f"  Account No (in scope):")
        for acc, n in kept["Account No"].value_counts().sort_index().items():
            print(f"      {acc:>6s}   {n:>6,}")
        print(f"  Dept (in scope):")
        for dept, n in kept["Dept"].value_counts().sort_index().items():
            print(f"      {dept:>6s}   {n:>6,}")
    return in_total, out_total


def _run_ingest_airtable() -> int:
    import traceback

    from engine.airtable_client import (
        append_run_log,
        get_api_and_base,
        mark_inbox_processed,
    )
    from engine.filters import in_scope, out_of_scope
    from engine.ingest import (
        AirtableInboxSource,
        DuplicateTransactionsError,
        NoNewTransactionsError,
        UnusableInboxRecordError,
    )

    try:
        _, base = get_api_and_base()
    except RuntimeError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    source = AirtableInboxSource(base)
    run_id = datetime.now(timezone.utc).isoformat(timespec="seconds")
    today_iso = datetime.now(timezone.utc).date().isoformat()

    try:
        df, meta = source.get_latest_transactions()
    except NoNewTransactionsError as exc:
        print(f"no new data: {exc}")
        append_run_log(
            base, run_id=run_id, mode="ingest", outcome="no_new_data",
            notes=str(exc),
        )
        return 0
    except DuplicateTransactionsError as exc:
        print(f"duplicate file detected: {exc}")
        if exc.inbox_record_id:
            mark_inbox_processed(
                base, exc.inbox_record_id,
                file_hash=exc.hash,
                rows_in_scope=0,
                total_in_scope=0.0,
                processed_at_iso_date=today_iso,
                notes=(
                    f"duplicate of a previously processed file (hash "
                    f"{exc.hash[:12]}…); marked processed without re-parsing."
                ),
            )
        append_run_log(
            base, run_id=run_id, mode="ingest", outcome="no_new_data",
            file_name=exc.filename, file_hash=exc.hash,
            notes="duplicate hash — skipped",
        )
        return 0
    except UnusableInboxRecordError as exc:
        # Distinct from NoNewTransactionsError: the record exists and is
        # malformed. Mark it Processed so subsequent runs don't re-detect it
        # as "newest unprocessed" forever.
        print(f"unusable Inbox record: {exc}", file=sys.stderr)
        mark_inbox_processed(
            base, exc.inbox_record_id,
            file_hash="",
            rows_in_scope=0,
            total_in_scope=0.0,
            processed_at_iso_date=today_iso,
            notes=f"could not process: {exc.reason}. Review and replace.",
        )
        append_run_log(
            base, run_id=run_id, mode="ingest", outcome="error",
            notes=f"unusable Inbox record: {exc.reason}",
        )
        return 1

    # Broad try/except so an unexpected failure (parser ValueError, pandas
    # error, Airtable 5xx on mark) still writes a Run Log row with outcome
    # 'error' — the Run Log is the authoritative audit trail and missing
    # rows are worse than verbose ones.
    try:
        kept = in_scope(df)
        rejected = out_of_scope(df)
        in_total, out_total = _print_ingest_report(df, meta, kept, rejected)

        if meta.inbox_record_id:
            mark_inbox_processed(
                base, meta.inbox_record_id,
                file_hash=meta.hash,
                rows_in_scope=len(kept),
                total_in_scope=in_total,
                processed_at_iso_date=today_iso,
                notes=f"parsed {len(df):,} rows; in-scope {len(kept):,}.",
            )
        append_run_log(
            base, run_id=run_id, mode="ingest", outcome="ok",
            file_name=meta.name, file_hash=meta.hash,
            rows_in_scope=len(kept), rows_out_of_scope=len(rejected),
            total_in_scope=in_total, total_out_of_scope=out_total,
        )
        return 0
    except Exception as exc:
        tb = traceback.format_exc()
        print(f"FATAL during ingest: {exc}", file=sys.stderr)
        print(tb, file=sys.stderr)
        # Best-effort Run Log row. Don't swallow the audit-write failure if it
        # also fails — caller sees the original traceback above.
        try:
            append_run_log(
                base, run_id=run_id, mode="ingest", outcome="error",
                file_name=meta.name, file_hash=meta.hash,
                notes=f"{type(exc).__name__}: {exc}",
            )
        except Exception:  # noqa: BLE001 — secondary failure logged via tb above
            pass
        # Inbox record left Processed=false so a retry picks it up.
        return 1


def _run_ingest_file(path: str) -> int:
    from engine.filters import in_scope, out_of_scope
    from engine.ingest import LocalFileSource

    print("NOTE: --ingest-file bypasses Airtable — no dedup check, no "
          "Run Log row, no Inbox mark. Use --ingest for a real run.")
    print()

    try:
        source = LocalFileSource(path)
        df, meta = source.get_latest_transactions()
    except FileNotFoundError:
        print(f"FATAL: file not found at {path}", file=sys.stderr)
        return 2
    except (UnicodeDecodeError, ValueError) as exc:
        print(f"FATAL: parse error on {path}: {exc}", file=sys.stderr)
        return 1

    kept = in_scope(df)
    rejected = out_of_scope(df)
    _print_ingest_report(df, meta, kept, rejected)
    return 0


if __name__ == "__main__":
    sys.exit(main())
