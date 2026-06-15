"""Engine entry point.

CLI wires four modes (all read-only against Asana; Asana writes land in
Step 5):

  --audit                  verify Asana project schema matches expectations
  --provision              create/verify the 8 Airtable tables (idempotent)
  --ingest                 full pipeline: promote -> ingest -> attribute ->
                           compute -> write Airtable Dashboard + Needs
                           Tagging + Run Log
  --ingest-file PATH       parse + filter a local export file; no attribution
                           and no Airtable writes (parser smoke-test only)

The --ingest pipeline (Steps 2 + 3 + 4 combined):
  1. Promote any operator-filled Needs Tagging rows into Learned Mappings.
  2. Pull the newest unprocessed Inbox attachment; dedup by SHA-256 content
     hash against prior Processed rows.
  3. Parse + filter (in-scope account/dept) + signed sums report.
  4. Load Asana contracts + Airtable lookups; run attribution on the
     in-scope DataFrame.
  5. Upsert Needs Tagging rows for ambiguous / unmatched groupings.
  6. Clean up stale Needs Tagging rows whose groups no longer need review.
  7. Compute per-contract Dashboard rows for every contract passing the
     live gate. Spent so far uses the term-window date filter against
     attributed transactions (predecessor-term spend excluded).
  8. Upsert Dashboard rows by Asana Task GID.
  9. Mark Inbox Processed; append Run Log row with the attribution +
     dashboard summaries.

Pair --provision with --dry-run to see the schema plan without applying it.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone


# Module-level logger — used by helpers like _run_attribution_and_needs_tagging
# that previously assumed `log` was available in their scope. Without this
# the orphan-contract warning path would raise NameError on the first
# unusual run, converting a graceful skip into a hard abort.
log = logging.getLogger(__name__)


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
        help="Full local-first pipeline: pick the oldest unprocessed file "
             "from data/inbox/ (or the configured TransactionSource), parse "
             "+ filter, promote operator-filled Needs Tagging answers, load "
             "Asana contracts and run attribution, upsert Needs Tagging / "
             "Dashboard / State rows into data/engine.db, move the file to "
             "data/processed/, and write a Run Log entry.",
    )
    mode.add_argument(
        "--ingest-file", metavar="PATH",
        help="Parse a local Tableau export file, dedup its hash against "
             "data/engine.db, and record an Inbox + Run Log row. Skips "
             "attribution / compute — use --ingest for the full pipeline.",
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

    if args.audit:
        from engine.audit import main as audit_main
        return audit_main([])

    if args.provision:
        return _run_provision(dry_run=args.dry_run)

    if args.ingest:
        return _run_ingest()

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


def _run_promotion_only(conn, *, today_iso: str) -> int:
    """Drain the Needs Tagging promotion queue without doing ingestion.

    Called on the no-new-data and duplicate-file early exits so an operator
    who fills Assign Contract on Friday and runs --ingest with no new file
    still sees their answers get promoted. Returns the number promoted.
    """
    from engine import asana_client, asana_contracts
    from engine.sqlite_client import promote_filled_needs_tagging

    # Best-effort: load contract names for validation; if Asana auth fails,
    # promote without validation (the operator will see the typo eventually).
    valid_names: frozenset[str] | None = None
    try:
        api = asana_client.get_api_client()
        contracts = asana_contracts.load_open_contracts(api)
        valid_names = frozenset(c.name for c in contracts if c.name)
    except Exception as exc:  # noqa: BLE001
        log.warning("Skipping contract-name validation during promotion: %s", exc)

    promotions = promote_filled_needs_tagging(
        conn, learned_at_iso_date=today_iso,
        valid_contract_names=valid_names,
    )
    if promotions:
        print(f"Promoted {len(promotions)} Needs Tagging answer(s) to Learned Mappings:")
        for p in promotions:
            print(f"  {p.group_key}  ->  {p.contract_name}")
    return len(promotions)


def _prune_run_log_safely(conn) -> None:
    """Best-effort Run Log rolling-window prune. Called at the end of every
    --ingest outcome (success, no-new-data, duplicate) so the table doesn't
    grow unbounded.

    Failure here NEVER fails the run — the Run Log already captured the
    primary outcome on the calling line above; a stale-row cleanup glitch
    is a transient I/O problem that the operator will see on the next
    run when it succeeds. Swallow + log so the exit code stays truthful
    about the actual ingest result.
    """
    from datetime import datetime, timezone

    from config import settings
    from engine.sqlite_client import prune_run_log_older_than

    try:
        prune_run_log_older_than(
            conn,
            retention_days=settings.RUN_LOG_RETENTION_DAYS,
            today=datetime.now(timezone.utc).date(),
        )
    except Exception as exc:  # noqa: BLE001
        log.info(
            "Run Log prune skipped this run (%s: %s). Will retry next run.",
            type(exc).__name__, exc,
        )


def _build_transaction_source(conn):
    """Pick the TransactionSource implementation based on settings.

    Phase 2 default: LocalInboxSource — scans data/inbox/ for the next
    Tableau export, dedups via SQLite, moves to data/processed/ on
    success. `airtable_inbox` is preserved as a legacy option (engine
    opens an Airtable base internally to pull from the Inbox table) so
    the GitHub Actions cron keeps working through the transition;
    Phase 6 cleanup deletes it. `tableau_rest` is a Step 7 stub that
    raises NotImplementedError on first pull.
    """
    from config import settings
    from engine.ingest import (
        AirtableInboxSource,
        LocalInboxSource,
        TableauRestSource,
    )

    choice = settings.TRANSACTION_SOURCE
    if choice == "tableau_rest":
        log.info(
            "TRANSACTION_SOURCE=tableau_rest (stubbed). "
            "Will raise NotImplementedError on get_latest_transactions; "
            "see engine.ingest.TableauRestSource for the planned shape."
        )
        return TableauRestSource(
            server_url=settings.TABLEAU_SERVER_URL,
            site_name=settings.TABLEAU_SITE_NAME,
            view_id=settings.TABLEAU_VIEW_ID,
            pat_name=settings.TABLEAU_PAT_NAME,
            pat_secret=settings.TABLEAU_PAT_SECRET,
            api_version=settings.TABLEAU_API_VERSION,
        )
    if choice == "airtable_inbox":
        # Legacy transition path: pull the inbox file from Airtable, but
        # write engine state (Dashboard / State / Run Log / SQLite Inbox
        # dedup) to SQLite. The Airtable Inbox row is also marked
        # Processed via airtable_client so subsequent pulls don't keep
        # selecting it.
        log.info(
            "TRANSACTION_SOURCE=airtable_inbox is a legacy path being "
            "phased out; default is now `local_inbox` (data/inbox/)."
        )
        from engine.airtable_client import get_api_and_base
        _, base = get_api_and_base()
        return AirtableInboxSource(base)
    # Default: local_inbox.
    return LocalInboxSource(conn)


def _run_ingest() -> int:
    """Phase-2 local-first --ingest path.

    Opens the SQLite engine database, ensures the schema, picks the
    configured TransactionSource (default: LocalInboxSource scanning
    data/inbox/), and runs the full pipeline against SQLite. All state
    writes — Inbox dedup, Needs Tagging, Dashboard, State, Run Log —
    land in data/engine.db. The Airtable era's mark_inbox_processed is
    replaced by insert_inbox_processed (a new audit-log row) plus a
    source-specific finalization (move file to data/processed/ for
    LocalInboxSource; legacy mark Airtable record Processed for the
    transition-period AirtableInboxSource).
    """
    import traceback

    from config import settings
    from engine import sqlite_client
    from engine.filters import in_scope, out_of_scope
    from engine.ingest import (
        AirtableInboxSource,
        DuplicateTransactionsError,
        LocalInboxSource,
        NoNewTransactionsError,
        UnusableInboxRecordError,
    )

    conn = sqlite_client.get_db_connection()
    try:
        # Idempotent — a fresh install gets the 8 tables created here,
        # an existing install is a no-op.
        sqlite_client.ensure_schema(conn)

        try:
            source = _build_transaction_source(conn)
        except RuntimeError as exc:
            # Misconfigured legacy airtable_inbox (missing AIRTABLE_PAT).
            print(f"FATAL: {exc}", file=sys.stderr)
            return 2

        run_id = datetime.now(timezone.utc).isoformat(timespec="seconds")
        today_iso = datetime.now(timezone.utc).date().isoformat()

        try:
            df, meta = source.get_latest_transactions()
        except NoNewTransactionsError as exc:
            print(f"no new data: {exc}")
            # Still drain the promotion queue — operator answers shouldn't
            # sit unpromoted just because no fresh file arrived.
            promoted = _run_promotion_only(conn, today_iso=today_iso)
            sqlite_client.append_run_log(
                conn, run_id=run_id, mode="ingest", outcome="no_new_data",
                notes=f"{exc}. Promoted {promoted} Needs Tagging answer(s).",
            )
            _prune_run_log_safely(conn)
            return 0
        except DuplicateTransactionsError as exc:
            print(f"duplicate file detected: {exc}")
            # Drain the promotion queue on duplicate-file early exit too.
            promoted = _run_promotion_only(conn, today_iso=today_iso)
            # If the source has an Airtable record handle, mark it
            # processed so we don't re-detect it on the next run.
            # LocalInboxSource already handled its own move-out-of-inbox
            # before raising — no follow-up needed here.
            if isinstance(source, AirtableInboxSource) and exc.inbox_record_id:
                from engine.airtable_client import mark_inbox_processed
                mark_inbox_processed(
                    source.base, exc.inbox_record_id,
                    file_hash=exc.hash,
                    rows_in_scope=0,
                    total_in_scope=0.0,
                    processed_at_iso_date=today_iso,
                    notes=(
                        f"duplicate of a previously processed file (hash "
                        f"{exc.hash[:12]}…); marked processed without re-parsing."
                    ),
                )
            sqlite_client.append_run_log(
                conn, run_id=run_id, mode="ingest", outcome="no_new_data",
                file_name=exc.filename, file_hash=exc.hash,
                notes=f"duplicate hash — skipped. Promoted {promoted} Needs Tagging answer(s).",
            )
            _prune_run_log_safely(conn)
            return 0
        except UnusableInboxRecordError as exc:
            # Distinct from NoNewTransactionsError: the record exists and is
            # malformed. Only AirtableInboxSource raises this (a record
            # with no attachment). Mark it Processed so subsequent runs
            # don't re-detect it as "newest unprocessed" forever.
            print(f"unusable Inbox record: {exc}", file=sys.stderr)
            if isinstance(source, AirtableInboxSource):
                from engine.airtable_client import mark_inbox_processed
                mark_inbox_processed(
                    source.base, exc.inbox_record_id,
                    file_hash="",
                    rows_in_scope=0,
                    total_in_scope=0.0,
                    processed_at_iso_date=today_iso,
                    notes=f"could not process: {exc.reason}. Review and replace.",
                )
            sqlite_client.append_run_log(
                conn, run_id=run_id, mode="ingest", outcome="error",
                notes=f"unusable Inbox record: {exc.reason}",
            )
            return 1

        # Broad try/except so an unexpected failure (parser ValueError,
        # pandas error, SQLite OperationalError on insert) still writes
        # a Run Log row with outcome 'error' — the Run Log is the
        # authoritative audit trail and missing rows are worse than
        # verbose ones.
        try:
            kept = in_scope(df)
            rejected = out_of_scope(df)
            in_total, out_total = _print_ingest_report(df, meta, kept, rejected)

            # Attribution + Needs Tagging + Dashboard + State + Asana
            # writes — all storage-side state goes to SQLite.
            attribution_summary = _run_attribution_and_needs_tagging(
                conn, kept, meta=meta, today_iso=today_iso,
            )

            # SQLite Inbox audit-log row: one per successfully-processed
            # file, keyed by File Hash (the dedup index).
            sqlite_client.insert_inbox_processed(
                conn,
                name=meta.name,
                file_hash=meta.hash,
                rows_in_scope=len(kept),
                total_in_scope=in_total,
                processed_at_iso_date=today_iso,
                notes=(
                    f"parsed {len(df):,} rows; in-scope {len(kept):,}. "
                    f"Attribution: {attribution_summary['summary_line']}. "
                    f"{attribution_summary['dashboard_line']}"
                ),
            )

            # Source-specific finalization. LocalInboxSource moves the
            # file out of data/inbox/; AirtableInboxSource (legacy)
            # marks the Airtable Inbox row Processed=true.
            if isinstance(source, LocalInboxSource) and meta.source_path:
                source.move_to_processed(meta.source_path, file_hash=meta.hash)
            elif isinstance(source, AirtableInboxSource) and meta.inbox_record_id:
                from engine.airtable_client import mark_inbox_processed
                mark_inbox_processed(
                    source.base, meta.inbox_record_id,
                    file_hash=meta.hash,
                    rows_in_scope=len(kept),
                    total_in_scope=in_total,
                    processed_at_iso_date=today_iso,
                    notes=(
                        f"parsed {len(df):,} rows; in-scope {len(kept):,}. "
                        f"Attribution: {attribution_summary['summary_line']}. "
                        f"{attribution_summary['dashboard_line']}"
                    ),
                )

            sqlite_client.append_run_log(
                conn, run_id=run_id, mode="ingest", outcome="ok",
                file_name=meta.name, file_hash=meta.hash,
                rows_in_scope=len(kept), rows_out_of_scope=len(rejected),
                total_in_scope=in_total, total_out_of_scope=out_total,
                notes=attribution_summary["notes"],
                review_flags=attribution_summary["review_flags"],
            )
            _prune_run_log_safely(conn)
            return 0
        except Exception as exc:
            tb = traceback.format_exc()
            print(f"FATAL during ingest: {exc}", file=sys.stderr)
            print(tb, file=sys.stderr)
            # Best-effort Run Log row. Don't swallow a secondary
            # audit-write failure — caller sees the original traceback
            # above.
            try:
                sqlite_client.append_run_log(
                    conn, run_id=run_id, mode="ingest", outcome="error",
                    file_name=meta.name, file_hash=meta.hash,
                    notes=f"{type(exc).__name__}: {exc}",
                )
            except Exception:  # noqa: BLE001 — secondary failure logged via tb above
                pass
            # File stays in data/inbox/ (or Airtable record stays
            # unmarked) so a retry picks it up.
            return 1
    finally:
        conn.close()


def _run_attribution_and_needs_tagging(
    conn, kept_df, *, meta, today_iso: str,
) -> dict:
    """Run attribution + Needs Tagging + Dashboard + State + Asana writes.

    Returns a small dict the caller uses to enrich the Inbox + Run Log
    notes. All storage writes land in SQLite (engine.sqlite_client);
    Asana is read- and write-target only.

    Promotion of operator-filled Needs Tagging → Learned Mappings
    happens BEFORE the attribution pass, so the new Learned Mappings
    are visible to the attribute() call within the same run.

    `meta` carries the file's hash (for State persistence's
    last_processed_hash field).
    """
    from config import settings
    from engine import asana_client, asana_contracts, attribution, campus_map
    from engine.sqlite_client import (
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
        conn, learned_at_iso_date=today_iso,
        valid_contract_names=valid_contract_names,
    )
    if promotions:
        print(f"Promoted {len(promotions)} Needs Tagging answer(s) to Learned Mappings:")
        for p in promotions:
            print(f"  {p.group_key}  ->  {p.contract_name}")

    aliases = load_vendor_aliases(conn)
    forward_overrides, drop_override = load_campus_map_overrides(conn)
    crosswalk = campus_map.build(forward_overrides, drop_override)
    learned = load_learned_mappings(conn)

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
            conn,
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
    stale_deleted = cleanup_stale_needs_tagging(conn, live_group_keys=live_keys)
    if stale_deleted:
        print(f"  cleaned up {stale_deleted} stale Needs Tagging row(s).")

    # Step 4: compute per-contract Dashboard rows for live contracts.
    from engine import compute
    from engine.sqlite_client import upsert_dashboard_row
    today_date = datetime.now(timezone.utc).date()
    dashboard_rows, skip_counts = compute.compute_dashboard(
        kept_df, run, contracts, today_date,
    )
    alarms_count = sum(1 for r in dashboard_rows if r.alarms == "ALARM")
    over_count = sum(1 for r in dashboard_rows if r.spending_rate_alarm == "Over")
    print()
    print("Dashboard compute")
    print(f"  live rows:              {len(dashboard_rows):>6}")
    print(f"  ALARM tripping:         {alarms_count:>6}")
    print(f"  Over budget (>100%):    {over_count:>6}")
    print(f"  skipped not-active:     {skip_counts['not_active']:>6}")
    print(f"  skipped expired:        {skip_counts['expired']:>6}")
    print(f"  skipped future-start:   {skip_counts['future_start']:>6}")
    print(f"  skipped past-due:       {skip_counts['past_due']:>6}")
    print(f"  skipped no-start-data:  {skip_counts['no_start_data']:>6}")
    # Step 6: change detection. Load prior State (BEFORE Dashboard upsert so
    # the diff is against the truly-prior snapshot, not what we're about to
    # write). State PERSIST happens after the Asana write loop so a hard
    # failure mid-pipeline leaves State un-advanced — next run's diff then
    # surfaces the lingering inconsistency rather than hiding it.
    from engine import state as state_mod
    from engine.sqlite_client import (
        cleanup_stale_state,
        load_state_priors,
        upsert_state_for_contract,
    )

    state_priors_by_gid = load_state_priors(conn)
    change_findings: list[state_mod.ChangeFinding] = []
    for dash_row in dashboard_rows:
        prior = state_priors_by_gid.get(dash_row.asana_task_gid)
        change_findings.extend(state_mod.diff_against_prior(dash_row, prior))
    change_counts = state_mod.summarize_findings(change_findings)
    review_block = state_mod.build_review_flags(change_findings)

    if any(v for k, v in change_counts.items() if k != "first_run"):
        print()
        print("Change detection (vs prior State)")
        print(f"  decreases:           {change_counts['decrease']:>6}")
        print(f"  alarm transitions:   {change_counts['alarm_transition']:>6}")
        print(f"  crossed 100%:        {change_counts['crossed_100']:>6}")
        print(f"  large swings:        {change_counts['large_swing']:>6}")
        print(f"  band transitions:    {change_counts['band_transition']:>6}")
        if change_counts["first_run"]:
            print(f"  first-run contracts: {change_counts['first_run']:>6}")
    elif change_counts["first_run"]:
        print()
        print(f"Change detection: {change_counts['first_run']} first-run contract(s); "
              f"no diffs against prior State.")
    else:
        print()
        print("Change detection: no diffs against prior State.")

    for dash_row in dashboard_rows:
        upsert_dashboard_row(conn, dash_row)
    if dashboard_rows:
        print(f"  upserted {len(dashboard_rows)} Dashboard row(s).")

    # Step 5: Asana writes. Strictly gated:
    #   - settings.DRY_RUN_ASANA defaults True (writes are logged, not sent).
    #   - settings.WRITE_TEST_CONTRACT, when set to a task GID, restricts
    #     writes to that one contract.
    #   - Each contract's writes are computed by diffing the DashboardRow
    #     values against the Contract's cached current Asana values; only
    #     fields that actually changed get written (idempotent).
    from engine import asana_writer
    contracts_by_gid = {c.gid: c for c in contracts}
    write_results: list[asana_writer.WriteResult] = []
    test_gid = settings.WRITE_TEST_CONTRACT or None
    for dash_row in dashboard_rows:
        current_contract = contracts_by_gid.get(dash_row.asana_task_gid)
        if current_contract is None:
            log.warning(
                "Skipping Asana write for gid %s (%s): no matching open "
                "Contract object (task may have been completed/archived "
                "between contract-load and write).",
                dash_row.asana_task_gid, dash_row.contract_name,
            )
            continue
        res = asana_writer.apply_writes(
            asana_api, dash_row, current_contract,
            dry_run=settings.DRY_RUN_ASANA,
            test_contract_gid=test_gid,
        )
        write_results.append(res)

    # State PERSIST — runs AFTER Dashboard upsert AND AFTER Asana writes so
    # State becomes the "high-water mark of a fully-successful run". If
    # Asana writes failed for some contracts, those contracts' State stays
    # un-advanced and the next run's diff re-surfaces the inconsistency.
    # Per-contract try/except so one bad Airtable PATCH doesn't leave a
    # half-written State (the rest of the loop still advances).
    state_persist_errors: list[tuple[str, str]] = []  # (contract_name, error)
    for dash_row in dashboard_rows:
        try:
            upsert_state_for_contract(
                conn,
                contract_name=dash_row.contract_name,
                asana_task_gid=dash_row.asana_task_gid,
                spent=dash_row.spent_so_far,
                pct_spent=dash_row.pct_spent,
                spending_rate=dash_row.spending_rate,
                spending_rate_alarm=dash_row.spending_rate_alarm,
                alarms=dash_row.alarms,
                last_processed_hash=meta.hash,
                last_updated_iso_date=today_iso,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "State upsert failed for %s (%s): %s",
                dash_row.contract_name, dash_row.asana_task_gid, exc,
            )
            state_persist_errors.append(
                (dash_row.contract_name, f"{type(exc).__name__}: {exc}")
            )
    # Sweep stale State rows (contract archived in Asana, or removed from
    # the open list) so the table doesn't grow monotonically.
    live_gids = {r.asana_task_gid for r in dashboard_rows}
    stale_state_deleted = cleanup_stale_state(conn, live_asana_task_gids=live_gids)
    if stale_state_deleted:
        print(f"  cleaned up {stale_state_deleted} stale State row(s).")
    if state_persist_errors:
        print(f"  WARN: {len(state_persist_errors)} State persist error(s) "
              f"(see Run Log review_flags for details)")

    write_summary = asana_writer.summarize(write_results, dry_run=settings.DRY_RUN_ASANA)
    mode_label = "DRY RUN" if settings.DRY_RUN_ASANA else "LIVE"
    if test_gid:
        mode_label += f" (test contract {test_gid} only)"
    print()
    print(f"Asana writes [{mode_label}]")
    print(f"  contracts evaluated:        {write_summary.contracts_evaluated:>6}")
    if test_gid:
        print(f"  contracts filtered out:     {write_summary.contracts_filtered:>6}")
    print(f"  contracts with changes:     {write_summary.contracts_changed:>6}")
    print(f"  contracts no-change:        {write_summary.contracts_no_change:>6}")
    if write_summary.contracts_errored:
        print(f"  contracts errored:          {write_summary.contracts_errored:>6}")
    if settings.DRY_RUN_ASANA:
        print(f"  fields that WOULD write:    {write_summary.fields_would_write:>6}")
    else:
        print(f"  fields written:             {write_summary.fields_written:>6}")
    for r in write_results:
        if r.deltas and not r.skipped_reason:
            marker = "[DRY]" if r.dry_run else "[ ok]" if not r.error else "[ERR]"
            field_summary = ", ".join(
                f"{d.field_name}: {d.old_value!r}->{d.new_value!r}"
                for d in r.deltas
            )
            print(f"  {marker} {r.contract_name} ({r.contract_gid})")
            print(f"        {field_summary}")
            if r.error:
                print(f"        ERROR: {r.error}")

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
    dashboard_line = (
        f"Dashboard: {len(dashboard_rows)} live rows ({alarms_count} ALARM, "
        f"{over_count} Over)."
    )
    notes_lines.append(dashboard_line)
    notes_lines.append(
        "Dashboard skips: "
        f"not_active={skip_counts['not_active']}, "
        f"expired={skip_counts['expired']}, "
        f"future_start={skip_counts['future_start']}, "
        f"past_due={skip_counts['past_due']}, "
        f"no_start_data={skip_counts['no_start_data']}."
    )
    write_label = "Asana writes (DRY RUN)" if settings.DRY_RUN_ASANA else "Asana writes (LIVE)"
    if test_gid:
        write_label += f" — test contract {test_gid} only"
    if settings.DRY_RUN_ASANA:
        notes_lines.append(
            f"{write_label}: would write {write_summary.fields_would_write} field(s) "
            f"across {write_summary.contracts_changed} contract(s); "
            f"{write_summary.contracts_no_change} no-change."
        )
    else:
        notes_lines.append(
            f"{write_label}: wrote {write_summary.fields_written} field(s) "
            f"across {write_summary.contracts_changed} contract(s); "
            f"{write_summary.contracts_no_change} no-change; "
            f"{write_summary.contracts_errored} errored."
        )
    review_flags = []
    if needs_tag:
        review_flags.append(f"{len(needs_tag)} group(s) in Needs Tagging awaiting Assign Contract.")
    if alarms_count:
        review_flags.append(f"{alarms_count} contract(s) tripping ALARM.")
    if write_summary.contracts_errored:
        review_flags.append(f"{write_summary.contracts_errored} Asana write(s) errored.")
    if state_persist_errors:
        review_flags.append(
            f"{len(state_persist_errors)} State persist error(s):\n"
            + "\n".join(f"  - {name}: {err}" for name, err in state_persist_errors)
        )
    # Change-detection findings (spec §10). Empty review_block means no
    # noteworthy diffs; we don't add a line in that case.
    if review_block:
        # One-line summary up top, then the detail block.
        cd_summary = (
            f"Change detection: "
            f"{change_counts['decrease']} decrease(s), "
            f"{change_counts['alarm_transition']} alarm transition(s), "
            f"{change_counts['crossed_100']} crossed-100%, "
            f"{change_counts['large_swing']} large swing(s), "
            f"{change_counts['band_transition']} band change(s)."
        )
        review_flags.append(cd_summary)
        review_flags.append(review_block)
    notes_lines.append(
        f"Change detection: "
        f"decreases={change_counts['decrease']}, "
        f"alarm_transitions={change_counts['alarm_transition']}, "
        f"crossed_100={change_counts['crossed_100']}, "
        f"large_swings={change_counts['large_swing']}, "
        f"band_changes={change_counts['band_transition']}, "
        f"first_run={change_counts['first_run']}."
    )

    return {
        "summary_line": summary_line,
        "dashboard_line": dashboard_line,
        "notes": "\n".join(notes_lines),
        "review_flags": "\n".join(review_flags),
        "run": run,
        "promotions": promotions,
    }


def _run_ingest_file(path: str) -> int:
    """Parse a local Tableau export and record the run in the SQLite
    engine database.

    Phase 1 of the local-first migration: this path exercises the new
    engine.sqlite_client end-to-end against a real file. It does NOT
    run attribution / compute (those require Asana and the full Inbox
    plumbing that Phase 2 introduces via LocalInboxSource); it does
    parse, hash, dedup-check, and write an Inbox + Run Log row so the
    SQLite layer is provably populated by a real file.
    """
    from datetime import datetime, timezone

    from engine.filters import in_scope, out_of_scope
    from engine.ingest import LocalFileSource
    from engine import sqlite_client

    print("NOTE: --ingest-file is the Phase-1 SQLite smoke path — it "
          "populates data/engine.db with Inbox + Run Log rows but skips "
          "attribution and compute (no Asana required). The full pipeline "
          "moves here in Phase 2.")
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
    in_total, out_total = _print_ingest_report(df, meta, kept, rejected)

    conn = sqlite_client.get_db_connection()
    try:
        sqlite_client.ensure_schema(conn)

        run_id = datetime.now(timezone.utc).isoformat(timespec="seconds")
        today_iso = datetime.now(timezone.utc).date().isoformat()

        # Dedup check — if the same file was processed before, surface
        # that and record a no_new_data Run Log row rather than blowing
        # up on the UNIQUE constraint at insert time.
        if sqlite_client.file_hash_already_processed(conn, meta.hash):
            print()
            print(
                f"DUPLICATE: this file's hash ({meta.hash[:12]}...) is "
                f"already in the Inbox table. Recording a no_new_data "
                f"Run Log row and skipping the Inbox insert."
            )
            sqlite_client.append_run_log(
                conn, run_id=run_id, mode="ingest", outcome="no_new_data",
                file_name=meta.name, file_hash=meta.hash,
                notes="--ingest-file: duplicate hash, no Inbox insert.",
            )
            return 0

        sqlite_client.insert_inbox_processed(
            conn,
            name=meta.name,
            file_hash=meta.hash,
            rows_in_scope=len(kept),
            total_in_scope=in_total,
            processed_at_iso_date=today_iso,
            notes=(
                f"--ingest-file parser smoke: parsed {len(df):,} rows; "
                f"in-scope {len(kept):,}. Phase 1 does not run "
                f"attribution / compute."
            ),
        )
        sqlite_client.append_run_log(
            conn, run_id=run_id, mode="ingest", outcome="ok",
            file_name=meta.name, file_hash=meta.hash,
            rows_in_scope=len(kept), rows_out_of_scope=len(rejected),
            total_in_scope=in_total, total_out_of_scope=out_total,
            notes=(
                "--ingest-file Phase-1 smoke: parser + Inbox + Run Log only. "
                "Attribution, Dashboard, Needs Tagging, and State writes "
                "are Phase 2 work."
            ),
        )
        print()
        print(f"Wrote Inbox + Run Log rows to {sqlite_client.DEFAULT_DB_PATH}.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
