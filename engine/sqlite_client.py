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
_UNIQUE_FIELDS: dict[str, str] = {
    "Dashboard": "Asana Task GID",
    "Needs Tagging": "Group Key",
    "Learned Mappings": "Key",
    "State": "Asana Task GID",
    "Run Log": "Run ID",
    "Campus Map": "Tableau Code",
    "Inbox": "File Hash",
}


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

    if not dry_run:
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
) -> dict[tuple[str, str, str, str], str]:
    """Return {(Campus, Dept, Account No, Vendor): Contract Name}."""
    out: dict[tuple[str, str, str, str], str] = {}
    for row in conn.execute(
        '''SELECT "Campus", "Dept", "Account No", "Vendor", "Contract Name"
           FROM "Learned Mappings"'''
    ):
        key = (
            (row["Campus"] or "").strip(),
            (row["Dept"] or "").strip(),
            (row["Account No"] or "").strip(),
            (row["Vendor"] or "").strip(),
        )
        contract = (row["Contract Name"] or "").strip()
        if not contract or not all(key):
            continue
        out[key] = contract
    return out


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
) -> dict:
    """Idempotent upsert keyed by Group Key.

    On UPDATE only the three engine-owned rolling fields are touched
    (Sample Record Description, $ in group, Engine Candidates) — Notes
    is operator-owned and stays untouched. On CREATE, Notes is left
    NULL for the operator to fill in.
    """
    candidate_lines: list[str] = []
    if candidate_names:
        candidate_lines.append("Engine vendor candidates:")
        for n in candidate_names:
            candidate_lines.append(f"  - {n}")
    else:
        candidate_lines.append("No vendor candidates found.")
    engine_candidates = "\n".join(candidate_lines)

    existing = _fetch_by_field(conn, "Needs Tagging", "Group Key", group_key)
    if existing:
        conn.execute(
            '''UPDATE "Needs Tagging"
               SET "Sample Record Description" = ?,
                   "$ in group" = ?,
                   "Engine Candidates" = ?
               WHERE id = ?''',
            (sample_description, amount, engine_candidates, existing["id"]),
        )
        conn.commit()
        return _fetch_by_id(conn, "Needs Tagging", existing["id"])

    cur = conn.execute(
        '''INSERT INTO "Needs Tagging"
             ("Group Key", "Campus", "Dept", "Account No", "Vendor",
              "Sample Record Description", "$ in group",
              "Created At", "Engine Candidates")
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (group_key, campus, dept, account_no, vendor,
         sample_description, amount, created_at_iso_date, engine_candidates),
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
        existing_lm = _fetch_by_field(conn, "Learned Mappings", "Key", group_key)
        if existing_lm:
            conn.execute(
                '''UPDATE "Learned Mappings"
                   SET "Campus" = ?, "Dept" = ?, "Account No" = ?,
                       "Vendor" = ?, "Contract Name" = ?,
                       "Learned At" = ?, "Notes" = ?
                   WHERE id = ?''',
                (campus, dept, account_no, vendor, contract_name,
                 learned_at_iso_date, notes_text, existing_lm["id"]),
            )
        else:
            conn.execute(
                '''INSERT INTO "Learned Mappings"
                     ("Key", "Campus", "Dept", "Account No", "Vendor",
                      "Contract Name", "Learned At", "Notes")
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (group_key, campus, dept, account_no, vendor,
                 contract_name, learned_at_iso_date, notes_text),
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
    AND whose Assign Contract is empty.

    Filled rows (operator answers in flight) are NEVER deleted by this
    path — they are the promotion queue's responsibility.
    """
    rows = conn.execute(
        '''SELECT id, "Group Key" FROM "Needs Tagging"
           WHERE "Assign Contract" IS NULL
              OR TRIM("Assign Contract") = '' '''
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
              "Start", "Due", "Status", "PM Email", "Last Updated")
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
             "Last Updated" = excluded."Last Updated"''',
        (
            row.contract_name, row.asana_task_gid, row.campus_set,
            contract_amount, round(row.spent_so_far, 2),
            pct_spent, spending_rate, row.spending_rate_alarm, row.alarms,
            row.start.isoformat(),
            row.due.isoformat() if row.due is not None else None,
            row.status, row.pm_email, row.last_updated.isoformat(),
        ),
    )
    conn.commit()
    return _fetch_by_field(conn, "Dashboard", "Asana Task GID", row.asana_task_gid)


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
    "ensure_schema",
    "sha256_hex",
    "file_hash_already_processed",
    "insert_inbox_processed",
    "append_run_log",
    "prune_run_log_older_than",
    "load_vendor_aliases",
    "load_campus_map_overrides",
    "load_learned_mappings",
    "upsert_needs_tagging_group",
    "set_needs_tagging_assign_contract",
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
    "upsert_dashboard_row",
    "load_state_priors",
    "upsert_state_for_contract",
    "cleanup_stale_state",
]
