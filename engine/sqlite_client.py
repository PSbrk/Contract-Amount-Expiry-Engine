"""SQLite I/O — schema management, record I/O, dedup.

Storage: data/engine.db (path configurable via ENGINE_DB_PATH env or by
passing db_path explicitly). All persistent engine state lives in this
one file. This module replaces engine.airtable_client; the public
function signatures are intentionally identical so the rest of the
engine — compute, attribution, state, main — doesn't care about the
storage swap.

Record shape — read/write functions return dicts of the form
    {"id": int, "fields": {col_name: value, ...}}
matching the legacy pyairtable shape so the migration is a swap, not a
rewrite. "id" is the SQLite ROWID; "fields" keys are the display field
names from config.schema.TABLES_SCHEMA (with spaces and special chars
preserved — they round-trip through quoted SQL identifiers).

NULL handling: SQLite stores absent values as NULL, surfaced as None in
Python. This is simpler than Airtable's PATCH-merge gotcha (where a
None had to be sent EXPLICITLY on update to clear a previously-set cell)
— ON CONFLICT ... DO UPDATE SET col = excluded.col clears the cell
uniformly whether the new value is a number or None.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from config import schema, settings


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

DEFAULT_DB_PATH: Path = Path("data") / "engine.db"


def backup_database_safely(db_path, backup_path: str | None) -> bool:
    """Best-effort copy of the SQLite DB to `backup_path` (the OneDrive
    mirror). Returns True if a copy was made, False otherwise; NEVER raises
    — backup is a convenience, not a correctness boundary.

    Shared by the --ingest path (engine/main.py) and the web UI's
    after-request hook (#4), so operator decisions made in the UI between
    ingests reach OneDrive instead of living only in the local engine.db.
    """
    if not backup_path:
        return False
    import shutil
    try:
        src = Path(db_path)
        dest = Path(backup_path)
        if not src.exists():
            # The DB should always exist by the time a backup is requested
            # (ingest wrote it / the UI request just used it). A missing
            # source is anomalous — surface it rather than silently skip.
            log.warning(
                "engine.db backup skipped: source %s does not exist. "
                "Local DB remains the source of truth.",
                src,
            )
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "engine.db backup to %s failed (%s: %s). Continuing — the "
            "local DB remains the source of truth.",
            backup_path, type(exc).__name__, exc,
        )
        return False


def get_db_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with the engine's standard settings.

    db_path defaults to ENGINE_DB_PATH env or data/engine.db. The parent
    directory is created if missing (so a fresh install doesn't crash
    on first call). Pass ':memory:' for tests.

    Row factory is set to sqlite3.Row so columns can be accessed by name
    (e.g. row["Contract Name"]).
    """
    if db_path is None:
        env_path = os.environ.get("ENGINE_DB_PATH", "").strip()
        db_path = env_path or DEFAULT_DB_PATH
    path_str = str(db_path)
    is_memory = path_str.startswith(":memory:")
    if not is_memory:
        Path(path_str).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path_str)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Schema ensure
# ---------------------------------------------------------------------------

@dataclass
class SchemaPlan:
    """What ensure_schema either did or would do — same shape as the
    Airtable client's SchemaPlan so callers don't change."""
    tables_created: list[str]
    fields_added: list[tuple[str, str]]
    tables_already_present: list[str]
    fields_already_present: list[tuple[str, str]]

    @property
    def is_noop(self) -> bool:
        return not self.tables_created and not self.fields_added


# Tables with a UNIQUE index on a single column, used by ON CONFLICT upserts.
# Vendor Aliases intentionally has no unique field — multi-row per contract
# is allowed (operator can split aliases across rows for readability).
# Learned Mappings is ALSO multi-row per Key as of Phase 7c — the operator
# can author multiple pattern-specific LMs under one (Campus, Dept, Acct,
# Vendor) key (one per Description Pattern). Composite uniqueness on
# (Key, Description Pattern) is enforced at the application layer.
_UNIQUE_FIELDS: dict[str, str] = {
    "Dashboard": "Asana Task GID",
    "Needs Tagging": "Group Key",
    "State": "Asana Task GID",
    "Run Log": "Run ID",
    "Campus Map": "Tableau Code",
    "CapEx Budgets": "CapEx ID",
    "Inbox": "File Hash",
    # One amendment task has at most one parent -- the column is the
    # natural identity for upserts. Multiple amendments per parent are
    # allowed (no unique index on Parent Gid).
    "Amendment Links": "Amendment Gid",
    # One row per resolved contract; the GID is its identity for upserts.
    "Resolved Contracts": "Asana Task GID",
}

# Legacy indexes that ensure_schema actively drops on existing databases
# (one-time migration; idempotent).
_LEGACY_INDEXES_TO_DROP: tuple[str, ...] = (
    "ux_Learned_Mappings_Key",   # removed in Phase 7c — multi-LM-per-Key
)


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r["name"] for r in rows}


def _existing_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    return {r["name"] for r in rows}


def _create_table_sql(table_decl: dict) -> str:
    cols = ["id INTEGER PRIMARY KEY AUTOINCREMENT"]
    for f in table_decl["fields"]:
        cols.append(f'"{f["name"]}" {schema.sqlite_column_type(f)}')
    return f'CREATE TABLE "{table_decl["name"]}" ({", ".join(cols)})'


def _unique_index_sql(table_name: str, field_name: str) -> str:
    idx_name = ("ux_" + table_name + "_" + field_name).replace(" ", "_")
    return f'CREATE UNIQUE INDEX "{idx_name}" ON "{table_name}" ("{field_name}")'


def ensure_schema(
    conn: sqlite3.Connection, *, dry_run: bool = False
) -> SchemaPlan:
    """Create any missing tables / add any missing columns per schema.TABLES_SCHEMA.

    Idempotent — re-running against an already-provisioned database is
    a no-op apart from the metadata read. When dry_run=True, computes
    the plan but writes nothing.
    """
    plan = SchemaPlan(
        tables_created=[],
        fields_added=[],
        tables_already_present=[],
        fields_already_present=[],
    )
    existing = _existing_tables(conn)

    for tdecl in schema.TABLES_SCHEMA:
        name = tdecl["name"]
        if name not in existing:
            plan.tables_created.append(name)
            if not dry_run:
                conn.execute(_create_table_sql(tdecl))
                uniq_field = _UNIQUE_FIELDS.get(name)
                if uniq_field:
                    conn.execute(_unique_index_sql(name, uniq_field))
            continue
        plan.tables_already_present.append(name)
        existing_cols = _existing_columns(conn, name)
        for f in tdecl["fields"]:
            fname = f["name"]
            if fname in existing_cols:
                plan.fields_already_present.append((name, fname))
                continue
            plan.fields_added.append((name, fname))
            if not dry_run:
                col_type = schema.sqlite_column_type(f)
                conn.execute(
                    f'ALTER TABLE "{name}" ADD COLUMN "{fname}" {col_type}'
                )

    # One-time legacy-index migrations. ensure_schema is idempotent and
    # frequently re-run, so DROP IF EXISTS is the right shape here.
    if not dry_run:
        for idx_name in _LEGACY_INDEXES_TO_DROP:
            conn.execute(f'DROP INDEX IF EXISTS "{idx_name}"')
        conn.commit()
    return plan


# ---------------------------------------------------------------------------
# Record-shape helpers
# ---------------------------------------------------------------------------

def _row_to_record(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to the legacy {"id": int, "fields": {...}} shape."""
    d = dict(row)
    rec_id = d.pop("id")
    return {"id": rec_id, "fields": d}


def _fetch_by_id(
    conn: sqlite3.Connection, table_name: str, rec_id: int
) -> dict | None:
    row = conn.execute(
        f'SELECT * FROM "{table_name}" WHERE id = ?', (rec_id,)
    ).fetchone()
    return _row_to_record(row) if row is not None else None


def _fetch_by_field(
    conn: sqlite3.Connection,
    table_name: str,
    field_name: str,
    value: Any,
) -> dict | None:
    row = conn.execute(
        f'SELECT * FROM "{table_name}" WHERE "{field_name}" = ?',
        (value,),
    ).fetchone()
    return _row_to_record(row) if row is not None else None


# ---------------------------------------------------------------------------
# Inbox (audit log of processed files)
# ---------------------------------------------------------------------------

# In the local-first model, data/inbox/ is the queue and the SQLite Inbox
# table is the AUDIT LOG — one row per file the engine has ever processed,
# keyed by content hash for dedup. There is no "unprocessed Inbox record"
# concept anymore; every row in this table is post-processing.

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hash_already_processed(conn: sqlite3.Connection, sha256_hex: str) -> bool:
    """True if a prior Inbox row carries this exact File Hash and is Processed."""
    if not sha256_hex:
        return False
    row = conn.execute(
        '''SELECT 1 FROM "Inbox"
           WHERE "File Hash" = ? AND "Processed" = 1
           LIMIT 1''',
        (sha256_hex,),
    ).fetchone()
    return row is not None


def insert_inbox_processed(
    conn: sqlite3.Connection,
    *,
    name: str,
    file_hash: str,
    rows_in_scope: int,
    total_in_scope: float,
    processed_at_iso_date: str,
    notes: str = "",
) -> dict:
    """Insert one Inbox row marking a file as processed.

    Replaces the Airtable client's mark_inbox_processed (which UPDATED
    an existing attachment record). In the local-first model every Inbox
    row is post-processing audit trail, so this is an INSERT.
    """
    cur = conn.execute(
        '''INSERT INTO "Inbox"
             ("Name", "File Hash", "Processed", "Processed At",
              "Rows In Scope", "Total In Scope", "Notes")
           VALUES (?, ?, 1, ?, ?, ?, ?)''',
        (name, file_hash, processed_at_iso_date,
         rows_in_scope, total_in_scope, notes),
    )
    conn.commit()
    return _fetch_by_id(conn, "Inbox", cur.lastrowid)


# ---------------------------------------------------------------------------
# Run Log
# ---------------------------------------------------------------------------

# Same client-side validation as the Airtable client: a typo in Mode or
# Outcome must raise loudly rather than silently land in the DB. SQLite
# has no native enum type, so we keep the validator at this layer.
_RUN_LOG_MODES: tuple[str, ...] = ("ingest", "provision", "audit", "compute", "write")
_RUN_LOG_OUTCOMES: tuple[str, ...] = ("ok", "no_new_data", "partial", "error")


def append_run_log(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    mode: str,
    outcome: str,
    file_name: str = "",
    file_hash: str = "",
    rows_in_scope: int | None = None,
    rows_out_of_scope: int | None = None,
    total_in_scope: float | None = None,
    total_out_of_scope: float | None = None,
    anomalies: str = "",
    review_flags: str = "",
    notes: str = "",
) -> dict:
    if mode not in _RUN_LOG_MODES:
        raise ValueError(
            f"Run Log mode {mode!r} is not one of {_RUN_LOG_MODES}."
        )
    if outcome not in _RUN_LOG_OUTCOMES:
        raise ValueError(
            f"Run Log outcome {outcome!r} is not one of {_RUN_LOG_OUTCOMES}."
        )
    cur = conn.execute(
        '''INSERT INTO "Run Log"
             ("Run ID", "Mode", "Outcome", "File Name", "File Hash",
              "Rows In Scope", "Rows Out Of Scope",
              "Total In Scope", "Total Out Of Scope",
              "Anomalies", "Review Flags", "Notes")
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (run_id, mode, outcome, file_name, file_hash,
         rows_in_scope, rows_out_of_scope,
         total_in_scope, total_out_of_scope,
         anomalies, review_flags, notes),
    )
    conn.commit()
    return _fetch_by_id(conn, "Run Log", cur.lastrowid)


def prune_run_log_older_than(
    conn: sqlite3.Connection,
    *,
    retention_days: int,
    today: date,
) -> int:
    """Delete Run Log rows older than `retention_days` days from `today`.

    Returns the count of rows deleted. retention_days <= 0 is a no-op.
    Malformed Run IDs are LEFT IN PLACE — we never delete a row whose
    timestamp we couldn't read.
    """
    if retention_days <= 0:
        return 0

    cutoff = today - timedelta(days=retention_days)
    rows = conn.execute('SELECT id, "Run ID" FROM "Run Log"').fetchall()
    deleted = 0
    unparseable = 0
    for r in rows:
        run_id = (r["Run ID"] or "").strip()
        if not run_id:
            unparseable += 1
            continue
        date_part = run_id.split("T", 1)[0]
        try:
            row_date = date.fromisoformat(date_part)
        except ValueError:
            unparseable += 1
            continue
        if row_date < cutoff:
            try:
                conn.execute('DELETE FROM "Run Log" WHERE id = ?', (r["id"],))
                deleted += 1
            except Exception as exc:  # noqa: BLE001
                log.info(
                    "Run Log prune delete on row %s returned %s; continuing.",
                    r["id"], type(exc).__name__,
                )
    if deleted:
        conn.commit()
        log.info(
            "pruned %d Run Log row(s) older than %s (retention=%d days)",
            deleted, cutoff.isoformat(), retention_days,
        )
    if unparseable:
        log.info(
            "%d Run Log row(s) had an unparseable Run ID and were left in "
            "place (manual cleanup if needed).", unparseable,
        )
    return deleted


# ---------------------------------------------------------------------------
# Vendor Aliases / Campus Map / Learned Mappings — read-only loaders
# ---------------------------------------------------------------------------

_SPLIT_ALIASES = re.compile(r"[,\n]")


def _split_multiline_list(raw: str | None) -> list[str]:
    """Split a multilineText cell into a clean list. Operators use either
    newlines or commas; both work. Empty / None → empty list."""
    if not raw:
        return []
    return [p.strip() for p in _SPLIT_ALIASES.split(raw) if p.strip()]


def load_vendor_aliases(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Return {contract_name: [alias, ...]} from the Vendor Aliases table.

    Empty alias cells produce an empty list — attribution falls back to
    the contract name alone for that contract.
    """
    out: dict[str, list[str]] = {}
    for row in conn.execute(
        'SELECT "Contract Name", "Aliases" FROM "Vendor Aliases"'
    ):
        name = (row["Contract Name"] or "").strip()
        if not name:
            continue
        aliases = _split_multiline_list(row["Aliases"])
        out.setdefault(name, []).extend(
            a for a in aliases if a not in out.get(name, [])
        )
    return out


def load_campus_map_overrides(
    conn: sqlite3.Connection,
) -> tuple[dict[str, frozenset[str]], frozenset[str] | None]:
    """Return (forward_overrides, drop_codes_override).

    drop_codes_override is None if no row has Drop=true (operator hasn't
    used the Drop checkbox; fall back to config defaults). Empty
    frozenset means "operator has deliberately turned off all drops".
    """
    overrides: dict[str, frozenset[str]] = {}
    drop_codes: set[str] = set()
    any_drop_checkbox_seen = False

    for row in conn.execute(
        '''SELECT "Tableau Code", "Asana Option Names", "Drop"
           FROM "Campus Map"'''
    ):
        code = (row["Tableau Code"] or "").strip()
        if not code:
            continue
        is_drop = bool(row["Drop"])
        if is_drop:
            drop_codes.add(code)
            any_drop_checkbox_seen = True
            continue
        options = frozenset(_split_multiline_list(row["Asana Option Names"]))
        if options:
            overrides[code] = options

    return (
        overrides,
        frozenset(drop_codes) if any_drop_checkbox_seen else None,
    )


def load_learned_mappings(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str, str, str], list[tuple[str, str | None, str | None]]]:
    """Return {(Campus, Dept, Account No, Vendor): [LearnedMapping, ...]}.

    Each LearnedMapping is a 3-tuple (Contract Name, Contract Gid, Description
    Pattern):
      - Contract Gid: None for legacy mappings (operator picked by name only).
        When set, attribution prefers the specific Asana task GID over the
        name-based resolution.
      - Description Pattern: None for group-level LMs (apply to every row in
        the group). When set, the LM only applies to rows whose Tableau
        Record Description CONTAINS the pattern (case-insensitive substring),
        letting the operator split one (Campus, Dept, Acct, Vendor) group
        across multiple Asana tasks by line-item scope (landscaping vs snow,
        etc.).

    Multiple LMs can share a key — that's the whole point of patterns. The
    list is sorted with pattern-bearing LMs first (longest pattern first)
    and the plain LM (if any) last; the attribution lookup walks the list
    and picks the most-specific match.
    """
    out: dict[tuple[str, str, str, str], list[tuple[str, str | None, str | None]]] = {}
    for row in conn.execute(
        '''SELECT "Campus", "Dept", "Account No", "Vendor",
                  "Contract Name", "Contract Gid", "Description Pattern"
           FROM "Learned Mappings"'''
    ):
        key = (
            (row["Campus"] or "").strip(),
            (row["Dept"] or "").strip(),
            (row["Account No"] or "").strip(),
            (row["Vendor"] or "").strip(),
        )
        contract = (row["Contract Name"] or "").strip()
        gid = (row["Contract Gid"] or "").strip() or None
        pattern = (row["Description Pattern"] or "").strip() or None
        if not contract or not all(key):
            continue
        out.setdefault(key, []).append((contract, gid, pattern))

    # Sort each key's list: pattern-bearing LMs first (longest pattern wins
    # the tiebreaker at lookup time, but presorting reduces work there),
    # plain LM last.
    for key_list in out.values():
        key_list.sort(
            key=lambda lm: (0, -len(lm[2])) if lm[2] else (1, 0),
        )
    return out


def load_pcard_links(conn: sqlite3.Connection) -> list[dict]:
    """Return the operator's P-Card links: blank-vendor, pattern-bearing
    Learned Mappings written by the P-Card Spend "Attribute to X" action.

    Each is {campus, dept, account_no, gid, name, pattern}. They drive a
    pre-attribution vendor stamp (engine.attribution.stamp_pcard_links): a
    blank-vendor row whose description matches the pattern gets the contract's
    vendor name stamped on, so it splits into its own clean vendor group and
    attributes normally — instead of poisoning the whole blank-vendor group to
    'ambiguous'. Stored on the Learned Mappings table (reuses its columns); the
    blank Vendor + present Description Pattern is what distinguishes them."""
    out: list[dict] = []
    for row in conn.execute(
        '''SELECT "Campus", "Dept", "Account No", "Contract Name",
                  "Contract Gid", "Description Pattern"
           FROM "Learned Mappings"
           WHERE COALESCE("Vendor", '') = ''
             AND COALESCE("Description Pattern", '') <> '' '''
    ):
        name = (row["Contract Name"] or "").strip()
        pattern = (row["Description Pattern"] or "").strip()
        campus = (row["Campus"] or "").strip()
        dept = (row["Dept"] or "").strip()
        account_no = (row["Account No"] or "").strip()
        if not (name and pattern and campus and dept and account_no):
            continue
        out.append({
            "campus": campus, "dept": dept, "account_no": account_no,
            "gid": (row["Contract Gid"] or "").strip() or None,
            "name": name, "pattern": pattern,
        })
    return out


def load_capex_budgets(conn: sqlite3.Connection) -> dict[str, float]:
    """Return {normalized CapEx ID: budget} from the CapEx Budgets table.

    Re-normalizes the stored key defensively so a hand-edited row with stray
    whitespace still joins. Rows with a blank id or non-numeric budget are
    skipped (a half-typed row shouldn't poison the project's %)."""
    from engine.asana_contracts import normalize_capex_id

    out: dict[str, float] = {}
    for row in conn.execute('SELECT "CapEx ID", "Budget" FROM "CapEx Budgets"'):
        cid = normalize_capex_id(row["CapEx ID"])
        budget = row["Budget"]
        if not cid or budget is None:
            continue
        try:
            out[cid] = float(budget)
        except (TypeError, ValueError):
            log.warning("CapEx Budgets row %r has non-numeric budget %r; skipping.",
                        cid, budget)
    return out


def upsert_capex_budget(
    conn: sqlite3.Connection,
    *,
    capex_id: str,
    budget: float,
    entered_at: str,
    notes: str = "",
    commit: bool = True,
) -> dict:
    """Insert or update one project budget, keyed by normalized CapEx ID.

    Used by the Needs-Budget UI (single-ID save and bulk paste-grid). The id
    is normalized here so the operator can paste it however the Google Doc has
    it and the join still lands."""
    from engine.asana_contracts import normalize_capex_id

    cid = normalize_capex_id(capex_id)
    if not cid:
        raise ValueError("upsert_capex_budget: blank CapEx ID")
    conn.execute(
        '''INSERT INTO "CapEx Budgets" ("CapEx ID", "Budget", "Entered At", "Notes")
           VALUES (?, ?, ?, ?)
           ON CONFLICT("CapEx ID") DO UPDATE SET
             "Budget" = excluded."Budget",
             "Entered At" = excluded."Entered At",
             "Notes" = excluded."Notes"''',
        (cid, float(budget), entered_at, notes),
    )
    if commit:
        conn.commit()
    return _fetch_by_field(conn, "CapEx Budgets", "CapEx ID", cid)


def delete_capex_budget(conn: sqlite3.Connection, *, capex_id: str) -> None:
    """Remove a project budget. Idempotent."""
    from engine.asana_contracts import normalize_capex_id
    cid = normalize_capex_id(capex_id) or capex_id
    conn.execute('DELETE FROM "CapEx Budgets" WHERE "CapEx ID" = ?', (cid,))
    conn.commit()


def upsert_plain_learned_mapping(
    conn: sqlite3.Connection,
    *,
    key: str,
    campus: str,
    dept: str,
    account_no: str,
    vendor: str,
    contract_name: str,
    contract_gid: str = "",
    learned_at: str,
    notes: str = "",
    ignore_coding: bool = False,
    commit: bool = True,
) -> None:
    """Insert or update the PLAIN (no-pattern) Learned Mapping for `key`.

    ignore_coding=True marks the mapping as a Miscoded? 'Accept' override
    (the gid-pinned learned path already bypasses the coding-narrow; this
    flag is the MARKER so the Miscoded? 'Accepted' view can list it). The
    UPDATE path ALWAYS writes it, so a later plain name-promotion clears a
    stale override flag — same discipline as Contract Gid.

    Single owner of the SELECT-then-UPDATE/INSERT dance for the catch-all
    LM, shared by promote_filled_needs_tagging and the Vendor Conflicts
    pin / mark-amendment routes (previously three near-identical copies —
    one of which forgot to write Contract Gid, letting a stale pin override
    an operator's later name answer).

    CRITICAL: the UPDATE path ALWAYS writes "Contract Gid" (to ''/NULL when
    the caller passes no gid). A name-based promotion therefore CLEARS any
    stale pinned gid on the existing row, so attribution resolves by the
    operator's freshly-chosen name instead of the leftover pin.
    """
    gid_val = (contract_gid or "").strip()
    ic_val = 1 if ignore_coding else 0
    existing = conn.execute(
        '''SELECT id FROM "Learned Mappings"
           WHERE "Key" = ?
             AND COALESCE("Description Pattern", '') = '' ''',
        (key,),
    ).fetchone()
    if existing:
        conn.execute(
            '''UPDATE "Learned Mappings"
               SET "Campus" = ?, "Dept" = ?, "Account No" = ?,
                   "Vendor" = ?, "Contract Name" = ?, "Contract Gid" = ?,
                   "Ignore Coding" = ?, "Learned At" = ?, "Notes" = ?
               WHERE id = ?''',
            (campus, dept, account_no, vendor, contract_name, gid_val,
             ic_val, learned_at, notes, existing["id"]),
        )
    else:
        conn.execute(
            '''INSERT INTO "Learned Mappings"
                 ("Key", "Campus", "Dept", "Account No", "Vendor",
                  "Contract Name", "Contract Gid", "Ignore Coding",
                  "Learned At", "Notes")
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (key, campus, dept, account_no, vendor, contract_name, gid_val,
             ic_val, learned_at, notes),
        )
    if commit:
        conn.commit()


# ---------------------------------------------------------------------------
# Needs Tagging — upsert / promote / cleanup
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Promotion:
    """One Needs Tagging → Learned Mappings promotion that just happened."""
    needs_tagging_record_id: str
    group_key: str
    campus: str
    dept: str
    account_no: str
    vendor: str
    contract_name: str


def upsert_needs_tagging_group(
    conn: sqlite3.Connection,
    *,
    group_key: str,
    campus: str,
    dept: str,
    account_no: str,
    vendor: str,
    sample_description: str,
    amount: float,
    candidate_names: list[str],
    created_at_iso_date: str,
    first_date: str = "",
    last_date: str = "",
    candidate_gids: list[str] | None = None,
    distinct_descriptions: (
        list[tuple[str, int, float]]
        | list[tuple[str, int, float, str, str]]
        | None
    ) = None,
    out_of_term: bool = False,
    coding_mismatch: bool = False,
    cross_tier_hint: str = "",
) -> dict:
    """Idempotent upsert keyed by Group Key.

    On UPDATE only the engine-owned rolling fields are touched (Sample
    Record Description, $ in group, First Date, Last Date, Engine
    Candidates) -- Notes, Assign Contract, and Dismissed are operator-owned
    and stay untouched. On CREATE, Notes / Assign Contract are NULL and
    Dismissed defaults to 0.

    If the existing row is Dismissed=1, the upsert is a no-op: the
    operator dismissed it as irrelevant and the engine must not keep
    re-surfacing the same group's amount / sample / candidates as if it
    still needs review. Returns the unchanged dismissed row.
    """
    candidate_lines: list[str] = []
    if candidate_names:
        candidate_lines.append("Engine vendor candidates:")
        for n in candidate_names:
            candidate_lines.append(f"  - {n}")
    else:
        candidate_lines.append("No vendor candidates found.")
    engine_candidates = "\n".join(candidate_lines)
    # Cross-tier coding-mismatch hint (engine-managed): an opex charge whose
    # vendor matches a CapEx-coded contract (excluded from the opex pool) lands
    # here as "no candidates". Lead the Engine Candidates text with the hint so
    # the operator sees WHY and that it can't be tagged here — the UI keys off
    # the "Coding mismatch:" marker to surface it prominently.
    if cross_tier_hint:
        engine_candidates = cross_tier_hint + "\n" + engine_candidates
    engine_candidate_gids = "\n".join(candidate_gids or [])
    # distinct_descriptions is a list of (desc, rows, amount) or, since
    # Phase 13, (desc, rows, amount, min_date_iso, max_date_iso) tuples.
    # We serialize to JSON so the UI can render the per-description picker
    # without losing the structured fields. min_date/max_date default to ""
    # when the input is the legacy 3-tuple shape (tolerated so a partial
    # rollout — old code writing rows, new code reading them, or vice
    # versa — degrades to text-only matching rather than crashing).
    import json as _json
    _dd_dicts: list[dict] = []
    for _t in (distinct_descriptions or []):
        if len(_t) == 5:
            d, r, a, dmin, dmax = _t
        elif len(_t) == 3:
            d, r, a = _t
            dmin, dmax = "", ""
        else:
            # Defensive: an unexpected tuple shape gets stringified into the
            # description so it's at least visible to the operator and we
            # don't silently drop the bucket.
            d, r, a, dmin, dmax = (repr(_t), 0, 0.0, "", "")
        _dd_dicts.append({
            "description": d, "rows": r, "amount": a,
            "min_date": dmin, "max_date": dmax,
        })
    distinct_descriptions_json = _json.dumps(_dd_dicts)

    # Phase 11: classify p-card / journal rows so they route to the
    # /p-card-spend audit surface instead of Needs Tagging Open. The
    # predicate is engine-owned and recomputed on every upsert -- the
    # classifier can evolve without an operator-driven flip.
    from engine.ingest import is_p_card_row
    is_p_card_flag = 1 if is_p_card_row(vendor, sample_description) else 0

    existing = _fetch_by_field(conn, "Needs Tagging", "Group Key", group_key)
    if existing:
        # _fetch_by_field returns the legacy {"id": int, "fields": {...}}
        # shape -- the actual column values live in existing["fields"].
        if existing["fields"].get("Dismissed"):
            # Dismissed by the operator; do not refresh. The row stays
            # exactly as it was when dismissed so the operator's audit
            # trail (sample / amount as of dismissal time) is preserved.
            return existing
        # #9: never let a re-ingest flip an operator-ENGAGED row into the
        # hidden /p-card-spend surface. If a later export emits the same
        # group with a blank vendor (backfill missed, reversal memo, etc.)
        # is_p_card_row would return True and the row would vanish from both
        # Needs Tagging and Vendor Conflicts, stranding the operator's
        # Assign / Conflict-Other / Once-Off decision where it can't be
        # acted on. Preserve the existing flag whenever the operator has
        # engaged; recompute freely for untouched rows.
        existing_is_p_card = 1 if existing["fields"].get("Is P-Card") else 0
        operator_engaged = bool(
            (existing["fields"].get("Assign Contract") or "").strip()
            or existing["fields"].get("Conflict Other")
            or existing["fields"].get("Once Off")
        )
        effective_is_p_card = (
            existing_is_p_card if operator_engaged else is_p_card_flag
        )
        if existing["fields"].get("Once Off"):
            # Once Off: the operator parked this group as a valid one-time
            # charge. Re-surface ONLY if NEW activity has arrived since the
            # anchor — i.e. the export's Last Date is strictly after the
            # anchor. Same-anchor / earlier-than-anchor → no-op (preserves
            # the operator's snapshot of state-at-marking).
            anchor = (existing["fields"].get("Once Off Anchor") or "").strip()
            # Resurface when genuinely NEW dated activity has arrived. An
            # EMPTY anchor means the group had no parsable dates when it was
            # parked (#10) — treat any later non-empty Last Date as new
            # activity so the group can't be suppressed forever once real
            # dates show up.
            new_activity = bool(last_date) and (not anchor or last_date > anchor)
            # A group NEWLY recognized as a coding mismatch (a matching live
            # contract now exists — e.g. a Vendor Alias was just added) is
            # materially different from the "one-time, ignore" the operator
            # parked. Surface it in Miscoded? even without new dated activity,
            # so a stale once-off flag can't shadow the new decision.
            if new_activity or coding_mismatch:
                # Resurface: clear the once-off flag and refresh the row
                # normally so the operator sees the new state.
                conn.execute(
                    '''UPDATE "Needs Tagging"
                       SET "Sample Record Description" = ?,
                           "$ in group" = ?,
                           "First Date" = ?,
                           "Last Date" = ?,
                           "Engine Candidates" = ?,
                           "Engine Candidate Gids" = ?,
                           "Distinct Descriptions JSON" = ?,
                           "Is P-Card" = ?,
                           "Out Of Term" = ?,
                           "Coding Mismatch" = ?,
                           "Once Off" = 0,
                           "Once Off Anchor" = NULL
                       WHERE id = ?''',
                    (sample_description, amount, first_date, last_date,
                     engine_candidates, engine_candidate_gids,
                     distinct_descriptions_json, effective_is_p_card,
                     1 if out_of_term else 0,
                     1 if coding_mismatch else 0,
                     existing["id"]),
                )
                conn.commit()
                return _fetch_by_id(conn, "Needs Tagging", existing["id"])
            # No new activity → leave the once-off snapshot intact.
            return existing
        conn.execute(
            '''UPDATE "Needs Tagging"
               SET "Sample Record Description" = ?,
                   "$ in group" = ?,
                   "First Date" = ?,
                   "Last Date" = ?,
                   "Engine Candidates" = ?,
                   "Engine Candidate Gids" = ?,
                   "Distinct Descriptions JSON" = ?,
                   "Is P-Card" = ?,
                   "Out Of Term" = ?,
                   "Coding Mismatch" = ?
               WHERE id = ?''',
            (sample_description, amount, first_date, last_date,
             engine_candidates, engine_candidate_gids,
             distinct_descriptions_json, effective_is_p_card,
             1 if out_of_term else 0, 1 if coding_mismatch else 0,
             existing["id"]),
        )
        conn.commit()
        return _fetch_by_id(conn, "Needs Tagging", existing["id"])

    cur = conn.execute(
        '''INSERT INTO "Needs Tagging"
             ("Group Key", "Campus", "Dept", "Account No", "Vendor",
              "Sample Record Description", "$ in group",
              "First Date", "Last Date",
              "Created At", "Engine Candidates", "Engine Candidate Gids",
              "Distinct Descriptions JSON", "Is P-Card", "Out Of Term",
              "Coding Mismatch")
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (group_key, campus, dept, account_no, vendor,
         sample_description, amount, first_date, last_date,
         created_at_iso_date, engine_candidates, engine_candidate_gids,
         distinct_descriptions_json, is_p_card_flag,
         1 if out_of_term else 0, 1 if coding_mismatch else 0),
    )
    conn.commit()
    return _fetch_by_id(conn, "Needs Tagging", cur.lastrowid)


def promote_filled_needs_tagging(
    conn: sqlite3.Connection,
    *,
    learned_at_iso_date: str,
    valid_contract_names: frozenset[str] | None = None,
) -> list[Promotion]:
    """For every Needs Tagging row with a filled Assign Contract:
    1. Validate against open Asana contract names (if provided).
    2. Upsert Learned Mappings by Key.
    3. Delete the Needs Tagging row.

    Idempotent against partial failures: if step 3 dies after step 2,
    the next run re-enters with a no-op LM upsert, the delete succeeds,
    and we converge.
    """
    filled_rows = conn.execute(
        '''SELECT * FROM "Needs Tagging"
           WHERE "Assign Contract" IS NOT NULL
             AND TRIM("Assign Contract") != '' '''
    ).fetchall()

    promotions: list[Promotion] = []
    for r in filled_rows:
        record_id = r["id"]
        campus = (r["Campus"] or "").strip()
        dept = (r["Dept"] or "").strip()
        account_no = (r["Account No"] or "").strip()
        vendor = (r["Vendor"] or "").strip()
        contract_name = (r["Assign Contract"] or "").strip()

        if not all([campus, dept, account_no, vendor, contract_name]):
            log.warning(
                "Skipping Needs Tagging row %s with incomplete fields.",
                record_id,
            )
            continue
        if (valid_contract_names is not None
                and contract_name not in valid_contract_names):
            log.warning(
                "Needs Tagging row %s has Assign Contract %r which does not "
                "match any open Asana contract — possible typo or stale name. "
                "Skipping promotion; please correct in the UI.",
                record_id, contract_name,
            )
            continue
        group_key = (
            (r["Group Key"] or "").strip()
            or f"{campus}|{dept}|{account_no}|{vendor}"
        )

        notes_text = (
            f"Promoted from Needs Tagging on {learned_at_iso_date}. "
            f"Operator selected: {contract_name}."
        )
        # Write the PLAIN (no-pattern) catch-all LM for the group via the
        # shared helper. contract_gid is intentionally omitted (->''): a
        # NAME-based promotion must CLEAR any stale pinned gid left by a
        # prior Vendor Conflicts pin, so attribution resolves by the
        # operator's freshly-chosen name rather than the leftover pin (#1).
        upsert_plain_learned_mapping(
            conn,
            key=group_key,
            campus=campus, dept=dept, account_no=account_no, vendor=vendor,
            contract_name=contract_name,
            learned_at=learned_at_iso_date,
            notes=notes_text,
            commit=False,
        )

        try:
            conn.execute(
                'DELETE FROM "Needs Tagging" WHERE id = ?', (record_id,)
            )
        except Exception as exc:  # noqa: BLE001
            log.info(
                "Needs Tagging row %s delete returned %s — continuing.",
                record_id, type(exc).__name__,
            )

        promotions.append(Promotion(
            needs_tagging_record_id=str(record_id),
            group_key=group_key,
            campus=campus, dept=dept, account_no=account_no, vendor=vendor,
            contract_name=contract_name,
        ))

    if promotions:
        conn.commit()
        log.info("promoted %d Needs Tagging → Learned Mappings", len(promotions))
    return promotions


# ---------------------------------------------------------------------------
# Admin-table CRUD — used only by the web UI's editable list pages.
#
# Vendor Aliases / Campus Map / Learned Mappings are operator-curated tables;
# the ingest pipeline only READS them. These helpers exist so the UI can also
# WRITE without each route reaching directly into raw SQL. Each helper commits
# eagerly so the operator's edit lands before the response is rendered.
# ---------------------------------------------------------------------------

def insert_vendor_alias(
    conn: sqlite3.Connection,
    *,
    contract_name: str,
    aliases: str = "",
    notes: str = "",
) -> dict:
    cur = conn.execute(
        'INSERT INTO "Vendor Aliases" ("Contract Name", "Aliases", "Notes") '
        'VALUES (?, ?, ?)',
        (contract_name, aliases, notes),
    )
    conn.commit()
    return _fetch_by_id(conn, "Vendor Aliases", cur.lastrowid)


def update_vendor_alias(
    conn: sqlite3.Connection,
    *,
    record_id: int,
    contract_name: str,
    aliases: str = "",
    notes: str = "",
) -> None:
    conn.execute(
        'UPDATE "Vendor Aliases" SET "Contract Name" = ?, "Aliases" = ?, '
        '"Notes" = ? WHERE id = ?',
        (contract_name, aliases, notes, record_id),
    )
    conn.commit()


def delete_vendor_alias(conn: sqlite3.Connection, *, record_id: int) -> None:
    conn.execute('DELETE FROM "Vendor Aliases" WHERE id = ?', (record_id,))
    conn.commit()


def insert_campus_map(
    conn: sqlite3.Connection,
    *,
    tableau_code: str,
    asana_option_names: str = "",
    drop: bool = False,
    notes: str = "",
) -> dict:
    """Insert one Campus Map row. UNIQUE on Tableau Code — caller must
    catch sqlite3.IntegrityError on duplicate insert (the web UI flashes
    the conflict instead of 500-ing)."""
    cur = conn.execute(
        'INSERT INTO "Campus Map" ("Tableau Code", "Asana Option Names", '
        '"Drop", "Notes") VALUES (?, ?, ?, ?)',
        (tableau_code, asana_option_names, 1 if drop else 0, notes),
    )
    conn.commit()
    return _fetch_by_id(conn, "Campus Map", cur.lastrowid)


def update_campus_map(
    conn: sqlite3.Connection,
    *,
    record_id: int,
    tableau_code: str,
    asana_option_names: str = "",
    drop: bool = False,
    notes: str = "",
) -> None:
    conn.execute(
        'UPDATE "Campus Map" SET "Tableau Code" = ?, "Asana Option Names" = ?, '
        '"Drop" = ?, "Notes" = ? WHERE id = ?',
        (tableau_code, asana_option_names, 1 if drop else 0, notes, record_id),
    )
    conn.commit()


def delete_campus_map(conn: sqlite3.Connection, *, record_id: int) -> None:
    conn.execute('DELETE FROM "Campus Map" WHERE id = ?', (record_id,))
    conn.commit()


def insert_learned_mapping(
    conn: sqlite3.Connection,
    *,
    key: str,
    campus: str,
    dept: str,
    account_no: str,
    vendor: str,
    contract_name: str,
    learned_at: str = "",
    notes: str = "",
) -> dict:
    """Insert one Learned Mappings row. UNIQUE on Key. Routine usage
    creates these via promote_filled_needs_tagging; this manual helper
    is for the rare case where the operator wants to hand-author a
    mapping (e.g. to backfill historical attribution)."""
    cur = conn.execute(
        'INSERT INTO "Learned Mappings" '
        '("Key", "Campus", "Dept", "Account No", "Vendor", '
        ' "Contract Name", "Learned At", "Notes") '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (key, campus, dept, account_no, vendor, contract_name,
         learned_at, notes),
    )
    conn.commit()
    return _fetch_by_id(conn, "Learned Mappings", cur.lastrowid)


def update_learned_mapping(
    conn: sqlite3.Connection,
    *,
    record_id: int,
    key: str,
    campus: str,
    dept: str,
    account_no: str,
    vendor: str,
    contract_name: str,
    learned_at: str = "",
    notes: str = "",
) -> None:
    conn.execute(
        'UPDATE "Learned Mappings" '
        'SET "Key" = ?, "Campus" = ?, "Dept" = ?, "Account No" = ?, '
        '    "Vendor" = ?, "Contract Name" = ?, "Learned At" = ?, "Notes" = ? '
        'WHERE id = ?',
        (key, campus, dept, account_no, vendor, contract_name,
         learned_at, notes, record_id),
    )
    conn.commit()


def delete_learned_mapping(conn: sqlite3.Connection, *, record_id: int) -> None:
    conn.execute('DELETE FROM "Learned Mappings" WHERE id = ?', (record_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Amendment Links -- operator-declared "task A is amendment of task B"
# ---------------------------------------------------------------------------

def load_amendment_links(
    conn: sqlite3.Connection,
) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    """Return (parent_by_amendment, amendments_by_parent).

    parent_by_amendment maps amendment_gid -> {parent_gid, parent_name,
    amendment_name, linked_at}. amendments_by_parent maps parent_gid ->
    list of {amendment_gid, parent_name, amendment_name, linked_at} (one
    parent can have multiple amendments).
    """
    parent_by_amendment: dict[str, dict] = {}
    amendments_by_parent: dict[str, list[dict]] = {}
    for row in conn.execute(
        '''SELECT "Parent Gid", "Amendment Gid", "Parent Name",
                  "Amendment Name", "Linked At"
           FROM "Amendment Links"'''
    ):
        parent_gid = (row["Parent Gid"] or "").strip()
        amendment_gid = (row["Amendment Gid"] or "").strip()
        if not parent_gid or not amendment_gid:
            continue
        link = {
            "parent_gid": parent_gid,
            "amendment_gid": amendment_gid,
            "parent_name": (row["Parent Name"] or "").strip(),
            "amendment_name": (row["Amendment Name"] or "").strip(),
            "linked_at": (row["Linked At"] or "").strip(),
        }
        parent_by_amendment[amendment_gid] = link
        amendments_by_parent.setdefault(parent_gid, []).append(link)
    return parent_by_amendment, amendments_by_parent


def insert_amendment_link(
    conn: sqlite3.Connection,
    *,
    parent_gid: str,
    amendment_gid: str,
    parent_name: str = "",
    amendment_name: str = "",
    linked_at: str = "",
    notes: str = "",
) -> dict:
    """Insert one Amendment Links row. UNIQUE on Amendment Gid -- ON CONFLICT
    upserts so the operator can re-link to a different parent without first
    deleting. Self-link (parent_gid == amendment_gid) is rejected up front.
    """
    if not parent_gid or not amendment_gid:
        raise ValueError("parent_gid and amendment_gid are both required.")
    if parent_gid == amendment_gid:
        raise ValueError(
            f"Refusing self-link: a task cannot be an amendment of itself "
            f"(gid {amendment_gid})."
        )
    conn.execute(
        '''INSERT INTO "Amendment Links"
             ("Parent Gid", "Amendment Gid", "Parent Name",
              "Amendment Name", "Linked At", "Notes")
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT("Amendment Gid") DO UPDATE SET
             "Parent Gid" = excluded."Parent Gid",
             "Parent Name" = excluded."Parent Name",
             "Amendment Name" = excluded."Amendment Name",
             "Linked At" = excluded."Linked At",
             "Notes" = excluded."Notes"''',
        (parent_gid, amendment_gid, parent_name, amendment_name,
         linked_at, notes),
    )
    conn.commit()
    return _fetch_by_field(
        conn, "Amendment Links", "Amendment Gid", amendment_gid
    )


def delete_amendment_link(
    conn: sqlite3.Connection, *, amendment_gid: str
) -> None:
    """Remove the amendment-of relationship for the given amendment_gid.
    Idempotent: deleting a non-existent link is a no-op."""
    conn.execute(
        'DELETE FROM "Amendment Links" WHERE "Amendment Gid" = ?',
        (amendment_gid,),
    )
    conn.commit()


def load_resolved_contracts(conn: sqlite3.Connection) -> dict[str, dict]:
    """Return {gid: {"contract_name", "baseline_band", "resolved_at"}} for
    every operator-resolved contract. Used by Step 5 (alarm suppression +
    re-arm baseline) and by the UI to show the resolved state."""
    out: dict[str, dict] = {}
    for row in conn.execute(
        '''SELECT "Asana Task GID", "Contract Name", "Baseline Band", "Resolved At"
           FROM "Resolved Contracts"'''
    ):
        gid = (row["Asana Task GID"] or "").strip()
        if not gid:
            continue
        out[gid] = {
            "contract_name": (row["Contract Name"] or "").strip(),
            "baseline_band": (row["Baseline Band"] or "").strip(),
            "resolved_at": (row["Resolved At"] or "").strip(),
        }
    return out


def set_contract_resolved(
    conn: sqlite3.Connection,
    *,
    gid: str,
    contract_name: str = "",
    baseline_band: str = "",
    resolved_at: str = "",
) -> None:
    """Mark a contract resolved (mute its alarm writes). UNIQUE on GID --
    re-resolving an already-resolved contract refreshes the baseline band."""
    if not gid:
        raise ValueError("gid is required.")
    conn.execute(
        '''INSERT INTO "Resolved Contracts"
             ("Asana Task GID", "Contract Name", "Baseline Band", "Resolved At")
           VALUES (?, ?, ?, ?)
           ON CONFLICT("Asana Task GID") DO UPDATE SET
             "Contract Name" = excluded."Contract Name",
             "Baseline Band" = excluded."Baseline Band",
             "Resolved At" = excluded."Resolved At"''',
        (gid, contract_name, baseline_band or "", resolved_at),
    )
    conn.commit()


def update_resolved_baseline(
    conn: sqlite3.Connection, *, gid: str, baseline_band: str
) -> None:
    """Raise a resolved contract's baseline band after a re-arm. No-op if the
    contract isn't resolved."""
    conn.execute(
        'UPDATE "Resolved Contracts" SET "Baseline Band" = ? WHERE "Asana Task GID" = ?',
        (baseline_band or "", gid),
    )
    conn.commit()


def unresolve_contract(conn: sqlite3.Connection, *, gid: str) -> None:
    """Clear the resolved flag (resume normal alarm writes). Idempotent."""
    conn.execute(
        'DELETE FROM "Resolved Contracts" WHERE "Asana Task GID" = ?', (gid,)
    )
    conn.commit()


def set_needs_tagging_dismissed(
    conn: sqlite3.Connection,
    *,
    record_id: int,
    dismissed: bool,
) -> None:
    """Operator-driven UPDATE of the Dismissed flag on a single Needs
    Tagging row. Idempotent. Dismissing does NOT touch Assign Contract --
    those are independent concerns. If the operator later sets Assign
    Contract on a dismissed row, promote_filled_needs_tagging will still
    promote it (and the row will be deleted afterward, naturally
    resolving any ambiguity)."""
    conn.execute(
        'UPDATE "Needs Tagging" SET "Dismissed" = ? WHERE id = ?',
        (1 if dismissed else 0, record_id),
    )
    conn.commit()


def set_needs_tagging_once_off(
    conn: sqlite3.Connection,
    *,
    record_id: int,
    once_off: bool,
) -> None:
    """Operator-driven mark/unmark of the Once Off flag. When MARKING,
    the row's current Last Date is snapshotted as the Once Off Anchor —
    that's what the next ingest compares against to decide whether to
    re-surface. UNMARKING clears both the flag and the anchor so the row
    goes back to the open queue normally.

    Mutually exclusive with Dismissed at the UX level (the buttons aren't
    both shown), but not enforced at the storage level — a hand-edited
    row with both flags set will be treated as Dismissed (which checks
    first in upsert_needs_tagging_group).
    """
    if once_off:
        # Read the current Last Date to use as the resurface anchor.
        row = conn.execute(
            'SELECT "Last Date" FROM "Needs Tagging" WHERE id = ?',
            (record_id,),
        ).fetchone()
        anchor = (row["Last Date"] if row else "") or ""
        conn.execute(
            '''UPDATE "Needs Tagging"
               SET "Once Off" = 1, "Once Off Anchor" = ?
               WHERE id = ?''',
            (anchor, record_id),
        )
    else:
        conn.execute(
            '''UPDATE "Needs Tagging"
               SET "Once Off" = 0, "Once Off Anchor" = NULL
               WHERE id = ?''',
            (record_id,),
        )
    conn.commit()


def set_needs_tagging_p_card_ignored(
    conn: sqlite3.Connection,
    *,
    record_id: int,
    p_card_ignored: bool,
) -> None:
    """Operator-driven UPDATE of the P-Card Ignored flag on a single Needs
    Tagging row. Idempotent. Used by the /p-card-spend "Ignore once" button
    so the operator can clear rows they've eyeballed off the active P-Card
    list; restorable via the Ignored tab. Operator-owned: the engine upsert
    never touches this flag."""
    conn.execute(
        'UPDATE "Needs Tagging" SET "P-Card Ignored" = ? WHERE id = ?',
        (1 if p_card_ignored else 0, record_id),
    )
    conn.commit()


def set_needs_tagging_conflict_other(
    conn: sqlite3.Connection,
    *,
    record_id: int,
    conflict_other: bool,
) -> None:
    """Operator-driven UPDATE of the Conflict Other flag on a single Needs
    Tagging row. Idempotent. When True, the row is hidden from the Vendor
    Conflicts review panel (operator has declared none of the engine's
    vendor candidates fit) but remains in the open Needs Tagging queue so
    the operator can resolve it via Assign Contract. When False (undo),
    the row reappears in Vendor Conflicts if it still has candidate gids."""
    conn.execute(
        'UPDATE "Needs Tagging" SET "Conflict Other" = ? WHERE id = ?',
        (1 if conflict_other else 0, record_id),
    )
    conn.commit()


def set_needs_tagging_assign_contract(
    conn: sqlite3.Connection,
    *,
    record_id: int,
    contract_name: str,
) -> None:
    """Operator-driven UPDATE of the Assign Contract column on a single
    Needs Tagging row. Called from the web UI when the operator saves a
    row inline.

    Stored as TEXT verbatim; an empty string is allowed (clears a prior
    answer). Validation against open Asana contract names is deferred
    to promote_filled_needs_tagging — typos that don't match a real
    contract block promotion with a logged warning and the row stays
    for the operator to correct, rather than failing here on save.
    """
    conn.execute(
        'UPDATE "Needs Tagging" SET "Assign Contract" = ? WHERE id = ?',
        (contract_name, record_id),
    )
    conn.commit()


def cleanup_stale_needs_tagging(
    conn: sqlite3.Connection, *, live_group_keys: set[str]
) -> int:
    """Delete Needs Tagging rows whose Group Key is NOT in live_group_keys
    AND whose Assign Contract is empty AND that are NOT Dismissed AND that
    are NOT Once Off.

    Filled rows (operator answers in flight) are NEVER deleted by this
    path -- they are the promotion queue's responsibility. Dismissed
    rows are also kept indefinitely so the same group does not get
    re-detected and re-surfaced after the operator marked it irrelevant.
    Once Off rows are kept too — the anchor date stored on the row is
    what lets the engine decide whether to re-surface, so deleting the
    row would lose the operator's snapshot.
    """
    rows = conn.execute(
        '''SELECT id, "Group Key" FROM "Needs Tagging"
           WHERE ("Assign Contract" IS NULL OR TRIM("Assign Contract") = '')
             AND COALESCE("Dismissed", 0) = 0
             AND COALESCE("Once Off", 0) = 0'''
    ).fetchall()
    deleted = 0
    for r in rows:
        gk = (r["Group Key"] or "").strip()
        if gk and gk not in live_group_keys:
            try:
                conn.execute(
                    'DELETE FROM "Needs Tagging" WHERE id = ?', (r["id"],)
                )
                deleted += 1
            except Exception as exc:  # noqa: BLE001
                log.info(
                    "stale Needs Tagging delete on %s returned %s; continuing.",
                    r["id"], type(exc).__name__,
                )
    if deleted:
        conn.commit()
        log.info("cleaned up %d stale Needs Tagging row(s)", deleted)
    return deleted


# ---------------------------------------------------------------------------
# Dashboard upsert
# ---------------------------------------------------------------------------

# Pinned client-side so a typo or stale band string raises a clear error
# rather than silently writing a value that's not in the Asana option
# set. Derived from settings to keep the four sources of truth in
# lock-step (a divergence fails CI loudly via test_dashboard_singleSelect_*).
_DASHBOARD_SPENDING_RATE_ALARM_VALUES: frozenset[str] = frozenset(
    settings.ASANA_SPENDING_RATE_ALARM_OPTIONS
)
_DASHBOARD_ALARMS_VALUES: frozenset[str] = frozenset(settings.ASANA_ALARMS_OPTIONS)


def upsert_dashboard_row(conn: sqlite3.Connection, row) -> dict:
    """Idempotent upsert of a Dashboard row, keyed by Asana Task GID.

    SQLite UPSERT writes all columns including NULLs uniformly — there's
    no PATCH-merge gotcha to handle (the Airtable client had to send
    None EXPLICITLY on update to clear a previously-non-None cell;
    here, ON CONFLICT ... DO UPDATE SET col = excluded.col clears the
    cell whether the new value is a number or None).

    `row` is an engine.compute.DashboardRow.
    """
    if (row.spending_rate_alarm is not None
            and row.spending_rate_alarm not in _DASHBOARD_SPENDING_RATE_ALARM_VALUES):
        raise ValueError(
            f"Dashboard Spending Rate Alarm {row.spending_rate_alarm!r} is "
            f"not one of {sorted(_DASHBOARD_SPENDING_RATE_ALARM_VALUES)}."
        )
    if row.alarms not in _DASHBOARD_ALARMS_VALUES:
        raise ValueError(
            f"Dashboard Alarms {row.alarms!r} is not one of "
            f"{sorted(_DASHBOARD_ALARMS_VALUES)}."
        )

    contract_amount = (
        round(row.contract_amount, 2) if row.contract_amount is not None else None
    )
    pct_spent = round(row.pct_spent, 2) if row.pct_spent is not None else None
    spending_rate = (
        round(row.spending_rate, 2) if row.spending_rate is not None else None
    )

    conn.execute(
        '''INSERT INTO "Dashboard"
             ("Contract", "Asana Task GID", "Campus Set", "Contract Amount",
              "Spent so far", "% Spent", "Spending Rate",
              "Spending Rate Alarm", "Alarms",
              "Start", "Due", "Status", "PM Email",
              "Contract Reason Text", "Last Updated")
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT("Asana Task GID") DO UPDATE SET
             "Contract" = excluded."Contract",
             "Campus Set" = excluded."Campus Set",
             "Contract Amount" = excluded."Contract Amount",
             "Spent so far" = excluded."Spent so far",
             "% Spent" = excluded."% Spent",
             "Spending Rate" = excluded."Spending Rate",
             "Spending Rate Alarm" = excluded."Spending Rate Alarm",
             "Alarms" = excluded."Alarms",
             "Start" = excluded."Start",
             "Due" = excluded."Due",
             "Status" = excluded."Status",
             "PM Email" = excluded."PM Email",
             "Contract Reason Text" = excluded."Contract Reason Text",
             "Last Updated" = excluded."Last Updated"''',
        (
            row.contract_name, row.asana_task_gid, row.campus_set,
            contract_amount, round(row.spent_so_far, 2),
            pct_spent, spending_rate, row.spending_rate_alarm, row.alarms,
            row.start.isoformat(),
            row.due.isoformat() if row.due is not None else None,
            row.status, row.pm_email, row.contract_reason_text,
            row.last_updated.isoformat(),
        ),
    )
    conn.commit()
    return _fetch_by_field(conn, "Dashboard", "Asana Task GID", row.asana_task_gid)


# ---------------------------------------------------------------------------
# Attributed Lines — Dashboard drill-down snapshot (latest ingest only)
# ---------------------------------------------------------------------------

def replace_attributed_lines(
    conn: sqlite3.Connection, lines: list[dict],
) -> int:
    """Wholesale-replace the Attributed Lines table with this run's lines.

    A snapshot, not an audit log — the drill-down only ever wants the latest
    ingest's attribution. Each line dict is the shape produced by
    engine.compute.line_dict. Returns the count written.
    """
    conn.execute('DELETE FROM "Attributed Lines"')
    if lines:
        conn.executemany(
            '''INSERT INTO "Attributed Lines"
                 ("Asana Task GID", "Date", "Campus", "Account No", "Vendor",
                  "Record Description", "Reference", "Amount", "In Term", "Tier")
               VALUES (:gid, :date, :campus, :account_no, :vendor,
                       :description, :reference, :amount, :in_term_i, :tier)''',
            [{**l, "in_term_i": 1 if l["in_term"] else 0} for l in lines],
        )
    conn.commit()
    return len(lines)


def load_attributed_lines(
    conn: sqlite3.Connection, gid: str,
) -> list[sqlite3.Row]:
    """Attributed Tableau lines for one contract gid, in-term first then by
    date. Empty list when nothing attributed here (the $0 / unmatched case)."""
    return conn.execute(
        'SELECT * FROM "Attributed Lines" WHERE "Asana Task GID" = ? '
        'ORDER BY "In Term" DESC, "Date"',
        (gid,),
    ).fetchall()


def replace_unlinked_capex(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Wholesale-replace the Unlinked CapEx table with this ingest's parked
    projects (engine.capex.summarize_unlinked output). A snapshot, not an audit
    log. Each dict: capex_id, spend, campuses, descriptions, rows, updated."""
    conn.execute('DELETE FROM "Unlinked CapEx"')
    if rows:
        conn.executemany(
            '''INSERT INTO "Unlinked CapEx"
                 ("CapEx ID", "Spend", "Campuses", "Descriptions",
                  "Rows", "Last Updated")
               VALUES (:capex_id, :spend, :campuses, :descriptions,
                       :rows, :updated)''',
            rows,
        )
    conn.commit()
    return len(rows)


def load_unlinked_capex(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Parked CapEx projects from the last ingest, biggest spend first."""
    return conn.execute(
        'SELECT * FROM "Unlinked CapEx" ORDER BY COALESCE("Spend", 0) DESC'
    ).fetchall()


# ---------------------------------------------------------------------------
# State table I/O
# ---------------------------------------------------------------------------
#
# Keyed by Asana Task GID (NOT Contract Name) so a rename in Asana
# self-corrects rather than orphaning the prior State row.

_STATE_SPENDING_RATE_ALARM_VALUES: frozenset[str] = frozenset(
    settings.ASANA_SPENDING_RATE_ALARM_OPTIONS
)
_STATE_ALARMS_VALUES: frozenset[str] = frozenset(settings.ASANA_ALARMS_OPTIONS)


def load_state_priors(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return {asana_task_gid: StatePrior} from the State table.

    Rows missing Asana Task GID are skipped with a logged warning
    (legacy hand-edited rows, or rows from before the GID column was
    added).
    """
    # Lazy import — engine.state imports DashboardRow from compute, and
    # main → state → sqlite_client → state would loop.
    from engine.state import StatePrior

    out: dict[str, Any] = {}
    for row in conn.execute('SELECT * FROM "State"'):
        gid = (row["Asana Task GID"] or "").strip()
        if not gid:
            name = (row["Contract Name"] or "").strip() or "<unnamed>"
            log.warning(
                "State row for %r has no Asana Task GID; skipping. Delete "
                "or backfill the row to clean up.", name,
            )
            continue
        name = (row["Contract Name"] or "").strip()
        last_updated_raw = row["Last Updated At"]
        last_updated_parsed: date | None = None
        if last_updated_raw:
            try:
                last_updated_parsed = date.fromisoformat(
                    str(last_updated_raw)[:10]
                )
            except ValueError:
                log.warning(
                    "State row %r has malformed Last Updated At %r; "
                    "treating as missing.", name or gid, last_updated_raw,
                )
        out[gid] = StatePrior(
            contract_name=name,
            asana_task_gid=gid,
            prior_spent=row["Prior Spent"],
            prior_pct_spent=row["Prior % Spent"],
            prior_spending_rate=row["Prior Spending Rate"],
            prior_spending_rate_alarm=(row["Prior Spending Rate Alarm"] or None),
            prior_alarms=(row["Prior Alarms"] or None),
            last_processed_hash=(row["Last Processed Hash"] or None),
            last_updated_at=last_updated_parsed,
        )
    return out


def upsert_state_for_contract(
    conn: sqlite3.Connection,
    *,
    contract_name: str,
    asana_task_gid: str,
    spent: float,
    pct_spent: float | None,
    spending_rate: float | None,
    spending_rate_alarm: str | None,
    alarms: str,
    last_processed_hash: str,
    last_updated_iso_date: str,
) -> dict:
    """Idempotent upsert of one State row, keyed by Asana Task GID.

    Same client-side singleSelect validation as Dashboard upsert. The
    PATCH-merge / NULL-clearing gotcha from the Airtable era doesn't
    apply here — ON CONFLICT writes all columns uniformly.
    """
    if (spending_rate_alarm is not None
            and spending_rate_alarm not in _STATE_SPENDING_RATE_ALARM_VALUES):
        raise ValueError(
            f"State Prior Spending Rate Alarm {spending_rate_alarm!r} is not "
            f"one of {sorted(_STATE_SPENDING_RATE_ALARM_VALUES)}."
        )
    if alarms not in _STATE_ALARMS_VALUES:
        raise ValueError(
            f"State Prior Alarms {alarms!r} is not one of "
            f"{sorted(_STATE_ALARMS_VALUES)}."
        )

    pct_spent_r = round(pct_spent, 2) if pct_spent is not None else None
    spending_rate_r = (
        round(spending_rate, 2) if spending_rate is not None else None
    )

    conn.execute(
        '''INSERT INTO "State"
             ("Contract Name", "Asana Task GID", "Prior Spent",
              "Prior % Spent", "Prior Spending Rate",
              "Prior Spending Rate Alarm", "Prior Alarms",
              "Last Processed Hash", "Last Updated At")
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT("Asana Task GID") DO UPDATE SET
             "Contract Name" = excluded."Contract Name",
             "Prior Spent" = excluded."Prior Spent",
             "Prior % Spent" = excluded."Prior % Spent",
             "Prior Spending Rate" = excluded."Prior Spending Rate",
             "Prior Spending Rate Alarm" = excluded."Prior Spending Rate Alarm",
             "Prior Alarms" = excluded."Prior Alarms",
             "Last Processed Hash" = excluded."Last Processed Hash",
             "Last Updated At" = excluded."Last Updated At"''',
        (
            contract_name, asana_task_gid, round(spent, 2),
            pct_spent_r, spending_rate_r,
            spending_rate_alarm, alarms,
            last_processed_hash, last_updated_iso_date,
        ),
    )
    conn.commit()
    return _fetch_by_field(conn, "State", "Asana Task GID", asana_task_gid)


def cleanup_stale_state(
    conn: sqlite3.Connection, *, live_asana_task_gids: set[str]
) -> int:
    """Delete State rows whose Asana Task GID is no longer in the live set."""
    rows = conn.execute(
        'SELECT id, "Asana Task GID" FROM "State"'
    ).fetchall()
    deleted = 0
    for r in rows:
        gid = (r["Asana Task GID"] or "").strip()
        if gid and gid not in live_asana_task_gids:
            try:
                conn.execute(
                    'DELETE FROM "State" WHERE id = ?', (r["id"],)
                )
                deleted += 1
            except Exception as exc:  # noqa: BLE001
                log.info(
                    "stale State delete on %s returned %s; continuing.",
                    r["id"], type(exc).__name__,
                )
    if deleted:
        conn.commit()
        log.info("cleaned up %d stale State row(s)", deleted)
    return deleted


__all__ = [
    "SchemaPlan",
    "Promotion",
    "DEFAULT_DB_PATH",
    "get_db_connection",
    "backup_database_safely",
    "ensure_schema",
    "sha256_hex",
    "file_hash_already_processed",
    "insert_inbox_processed",
    "append_run_log",
    "prune_run_log_older_than",
    "load_vendor_aliases",
    "load_campus_map_overrides",
    "load_learned_mappings",
    "load_capex_budgets",
    "upsert_capex_budget",
    "delete_capex_budget",
    "upsert_plain_learned_mapping",
    "upsert_needs_tagging_group",
    "set_needs_tagging_assign_contract",
    "set_needs_tagging_dismissed",
    "set_needs_tagging_once_off",
    "set_needs_tagging_conflict_other",
    "set_needs_tagging_p_card_ignored",
    "promote_filled_needs_tagging",
    "cleanup_stale_needs_tagging",
    "insert_vendor_alias",
    "update_vendor_alias",
    "delete_vendor_alias",
    "insert_campus_map",
    "update_campus_map",
    "delete_campus_map",
    "insert_learned_mapping",
    "update_learned_mapping",
    "delete_learned_mapping",
    "load_amendment_links",
    "insert_amendment_link",
    "delete_amendment_link",
    "load_resolved_contracts",
    "set_contract_resolved",
    "update_resolved_baseline",
    "unresolve_contract",
    "upsert_dashboard_row",
    "replace_attributed_lines",
    "load_attributed_lines",
    "load_state_priors",
    "upsert_state_for_contract",
    "cleanup_stale_state",
]
