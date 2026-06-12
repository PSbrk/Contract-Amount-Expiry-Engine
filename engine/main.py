"""Engine entry point.

CLI wires four modes (all read-only against Asana; Asana writes land in
Step 5):

  --audit                  verify Asana project schema matches expectations
  --provision              create/verify the 8 Airtable tables (idempotent)
  --ingest                 full pipeline: promote -> ingest -> attribute -> write
                           Airtable Needs Tagging + Run Log
  --ingest-file PATH       parse + filter a local export file; no attribution
                           and no Airtable writes (parser smoke-test only)

The --ingest pipeline (Steps 2 + 3 combined):
  1. Promote any operator-filled Needs Tagging rows into Learned Mappings.
  2. Pull the newest unprocessed Inbox attachment; dedup by SHA-256 content
     hash against prior Processed rows.
  3. Parse + filter (in-scope account/dept) + signed sums report.
  4. Load Asana contracts + Airtable lookups; run attribution on the
     in-scope DataFrame.
  5. Upsert Needs Tagging rows for ambiguous / unmatched groupings.
  6. Clean up stale Needs Tagging rows whose groups no longer need review.
  7. Mark Inbox Processed; append Run Log row with the attribution summary.

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
        help="Full pipeline: promote operator-filled Needs Tagging answers "
             "into Learned Mappings, pull the newest unprocessed Inbox "
             "attachment, parse + filter, load Asana contracts and run "
             "attribution, upsert ambiguous/unmatched groupings into Needs "
             "Tagging, clean up stale Needs Tagging rows, mark the Inbox "
             "row Processed, and write a Run Log entry.",
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


def _run_promotion_only(base, *, today_iso: str) -> int:
    """Drain the Needs Tagging promotion queue without doing ingestion.

    Called on the no-new-data and duplicate-file early exits so an operator
    who fills Assign Contract on Friday and runs --ingest with no new file
    still sees their answers get promoted. Returns the number promoted.
    """
    from engine import asana_client, asana_contracts
    from engine.airtable_client import promote_filled_needs_tagging

    # Best-effort: load contract names for validation; if Asana auth fails,
    # promote without validation (the operator will see the typo eventually).
    valid_names: frozenset[str] | None = None
    try:
        api = asana_client.get_api_client()
        contracts = asana_contracts.load_open_contracts(api)
        valid_names = frozenset(c.name for c in contracts if c.name)
    except Exception as exc:  # noqa: BLE001
        log = logging.getLogger(__name__)
        log.warning("Skipping contract-name validation during promotion: %s", exc)

    promotions = promote_filled_needs_tagging(
        base, learned_at_iso_date=today_iso,
        valid_contract_names=valid_names,
    )
    if promotions:
        print(f"Promoted {len(promotions)} Needs Tagging answer(s) to Learned Mappings:")
        for p in promotions:
            print(f"  {p.group_key}  ->  {p.contract_name}")
    return len(promotions)


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
        # Still drain the promotion queue — operator answers shouldn't sit
        # unpromoted just because no fresh file arrived.
        promoted = _run_promotion_only(base, today_iso=today_iso)
        append_run_log(
            base, run_id=run_id, mode="ingest", outcome="no_new_data",
            notes=f"{exc}. Promoted {promoted} Needs Tagging answer(s).",
        )
        return 0
    except DuplicateTransactionsError as exc:
        print(f"duplicate file detected: {exc}")
        # Still drain the promotion queue on duplicate-file early exit too.
        promoted = _run_promotion_only(base, today_iso=today_iso)
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
            notes=f"duplicate hash — skipped. Promoted {promoted} Needs Tagging answer(s).",
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

        # Step 3: attribution + Needs Tagging upsert. Runs read-only against
        # Asana and writes only to the Airtable Needs Tagging table. No Asana
        # writes anywhere.
        attribution_summary = _run_attribution_and_needs_tagging(
            base, kept, today_iso=today_iso,
        )

        if meta.inbox_record_id:
            mark_inbox_processed(
                base, meta.inbox_record_id,
                file_hash=meta.hash,
                rows_in_scope=len(kept),
                total_in_scope=in_total,
                processed_at_iso_date=today_iso,
                notes=(
                    f"parsed {len(df):,} rows; in-scope {len(kept):,}. "
                    f"Attribution: {attribution_summary['summary_line']}"
                ),
            )
        append_run_log(
            base, run_id=run_id, mode="ingest", outcome="ok",
            file_name=meta.name, file_hash=meta.hash,
            rows_in_scope=len(kept), rows_out_of_scope=len(rejected),
            total_in_scope=in_total, total_out_of_scope=out_total,
            notes=attribution_summary["notes"],
            review_flags=attribution_summary["review_flags"],
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


def _run_attribution_and_needs_tagging(base, kept_df, *, today_iso: str) -> dict:
    """Run Step 3 attribution against an in-scope DataFrame and upsert any
    needs-tagging groups into Airtable. Returns a small dict the caller uses
    to enrich the Inbox + Run Log notes.

    Promotion of operator-filled Needs Tagging → Learned Mappings happens
    BEFORE the attribution pass, so the new Learned Mappings are visible to
    the attribute() call within the same run.
    """
    from engine import asana_client, asana_contracts, attribution, campus_map
    from engine.airtable_client import (
        cleanup_stale_needs_tagging,
        load_campus_map_overrides,
        load_learned_mappings,
        load_vendor_aliases,
        promote_filled_needs_tagging,
        upsert_needs_tagging_group,
    )

    # Load Asana contracts FIRST so we can pass valid contract names to the
    # promotion validator AND so the Learned Mappings created by promotion
    # are visible to the attribute() call below.
    try:
        asana_api = asana_client.get_api_client()
    except RuntimeError as exc:
        # Asana PAT missing is a setup error, not a transient one — surface
        # loudly so the operator notices.
        raise RuntimeError(f"attribution requires ASANA_PAT: {exc}") from exc
    contracts = asana_contracts.load_open_contracts(asana_api)
    valid_contract_names = frozenset(c.name for c in contracts if c.name)

    # Operator-driven promotions first. With valid_contract_names plumbed in,
    # a typo or stale rename in Assign Contract is caught at promotion time
    # rather than baked into a permanent Learned Mappings row.
    promotions = promote_filled_needs_tagging(
        base, learned_at_iso_date=today_iso,
        valid_contract_names=valid_contract_names,
    )
    if promotions:
        print(f"Promoted {len(promotions)} Needs Tagging answer(s) to Learned Mappings:")
        for p in promotions:
            print(f"  {p.group_key}  ->  {p.contract_name}")

    aliases = load_vendor_aliases(base)
    forward_overrides, drop_override = load_campus_map_overrides(base)
    crosswalk = campus_map.build(forward_overrides, drop_override)
    learned = load_learned_mappings(base)

    # Attribute.
    run = attribution.attribute(kept_df, contracts, aliases, crosswalk, learned)

    summary = run.summary_dict()
    print()
    print("Attribution")
    print(f"  total groups:      {summary['total_groups']:>6}")
    print(f"  auto-attributed:   {summary['auto']:>6}")
    print(f"  learned (operator):{summary['learned']:>6}")
    print(f"  ambiguous:         {summary['ambiguous']:>6}  (need review)")
    print(f"  unmatched:         {summary['unmatched']:>6}  (need review)")
    print(f"  dropped (INT etc): {summary['dropped']:>6}")

    # Upsert Needs Tagging for ambiguous + unmatched groups.
    needs_tag = run.needs_tagging_groups
    upserted = 0
    for group in needs_tag:
        upsert_needs_tagging_group(
            base,
            group_key=group.group_key,
            campus=group.campus,
            dept=group.dept,
            account_no=group.account_no,
            vendor=group.vendor,
            sample_description=group.sample_description,
            amount=group.amount,
            candidate_names=list(group.candidate_names),
            created_at_iso_date=today_iso,
        )
        upserted += 1
    if upserted:
        print(f"  upserted {upserted} Needs Tagging row(s) for operator review.")

    # Sweep stale Needs Tagging rows whose group is no longer in the review
    # set — e.g. a group that was ambiguous last run but is now auto via a
    # newly-added Vendor Alias. Filled (operator-answered) rows are NEVER
    # touched; this only deletes rows the operator hasn't engaged with.
    live_keys = {g.group_key for g in needs_tag}
    stale_deleted = cleanup_stale_needs_tagging(base, live_group_keys=live_keys)
    if stale_deleted:
        print(f"  cleaned up {stale_deleted} stale Needs Tagging row(s).")

    # Compose the audit-trail strings.
    summary_line = (
        f"auto {summary['auto']}, learned {summary['learned']}, "
        f"ambiguous {summary['ambiguous']}, unmatched {summary['unmatched']}, "
        f"dropped {summary['dropped']}"
    )
    notes_lines = [f"Attribution summary: {summary_line}.", f"Open contracts loaded: {len(contracts)}."]
    if promotions:
        notes_lines.append(f"Promoted {len(promotions)} prior Needs Tagging answers.")
    if stale_deleted:
        notes_lines.append(f"Cleaned up {stale_deleted} stale Needs Tagging rows.")
    review_flags = []
    if needs_tag:
        review_flags.append(f"{len(needs_tag)} group(s) in Needs Tagging awaiting Assign Contract.")

    return {
        "summary_line": summary_line,
        "notes": "\n".join(notes_lines),
        "review_flags": "\n".join(review_flags),
        "run": run,
        "promotions": promotions,
    }


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
