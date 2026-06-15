"""Airtable I/O — schema management, record I/O, attachment download.

Auth: PAT from AIRTABLE_PAT + base id from AIRTABLE_BASE_ID. Retry strategy
wraps the underlying requests session so 429s against Airtable's 5-req/sec/base
ceiling are handled transparently.

Schema management is idempotent: ensure_schema() creates missing tables and
adds missing fields, but never destroys data. Renames are deliberately NOT
attempted — operators rename in the Airtable UI; the engine treats a renamed
field as "missing" (and would re-create the old name), so don't rename without
also updating config/airtable_schema.py.

Attachment download note (per Step 1 research): Airtable attachment URLs are
pre-signed and expire ~2h after the record is fetched. They must be downloaded
with NO Authorization header — sending the PAT can fail the request. We use a
plain requests.get() with no auth.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

import requests
from pyairtable import Api, retry_strategy

from config import airtable_schema, settings


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def get_api_and_base(
    pat: str | None = None,
    base_id: str | None = None,
) -> tuple[Api, Any]:
    """Return (Api, Base) configured with retry against Airtable's 5/sec/base limit.

    Raises a clear error when either env var is missing, rather than handing
    back a misconfigured client that 401s on the first call.
    """
    token = pat if pat is not None else os.environ.get("AIRTABLE_PAT", "").strip()
    if not token:
        raise RuntimeError(
            "AIRTABLE_PAT is not set. Add it to .env locally or to GitHub "
            "Actions secrets for CI runs."
        )
    base_value = base_id if base_id is not None else os.environ.get("AIRTABLE_BASE_ID", "").strip()
    if not base_value:
        raise RuntimeError(
            "AIRTABLE_BASE_ID is not set. Add it to .env locally or to GitHub "
            "Actions secrets for CI runs."
        )

    # retry_strategy is a module-level function in pyairtable 3.x, NOT a
    # classmethod on Api. Older docs (including some tutorials) show
    # `Api.retry_strategy(...)` which fails with AttributeError.
    api = Api(
        token,
        retry_strategy=retry_strategy(
            total=5,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
        ),
    )
    base = api.base(base_value)
    return api, base


# ---------------------------------------------------------------------------
# Schema ensure
# ---------------------------------------------------------------------------

@dataclass
class SchemaPlan:
    """What ensure_schema either did or would do."""
    tables_created: list[str]
    fields_added: list[tuple[str, str]]    # (table, field)
    tables_already_present: list[str]
    fields_already_present: list[tuple[str, str]]

    @property
    def is_noop(self) -> bool:
        return not self.tables_created and not self.fields_added


def _live_schema(base) -> dict[str, dict[str, Any]]:
    """Snapshot of the base's current schema as {table_name: {id, fields: {field_name: FieldSchema}}}.

    force=True bypasses pyairtable's in-process cache so a plan computed at
    startup reflects what's actually in Airtable right now.
    """
    schema = base.schema(force=True)
    out: dict[str, dict[str, Any]] = {}
    for tbl in schema.tables:
        out[tbl.name] = {
            "id": tbl.id,
            "fields": {f.name: f for f in tbl.fields},
        }
    return out


def _field_payload(field_decl: dict) -> dict:
    """Pick only the keys Airtable's create-table/create-field API accepts."""
    return {k: v for k, v in field_decl.items()
            if k in ("name", "type", "options", "description") and v is not None}


def ensure_schema(base, *, dry_run: bool = False) -> SchemaPlan:
    """Create any missing tables and add any missing fields per TABLES_SCHEMA.

    Idempotent: re-running against a base already in shape is a no-op (apart
    from one base.schema fetch). When dry_run=True, computes the plan but
    makes no API writes.
    """
    live = _live_schema(base)
    plan = SchemaPlan(
        tables_created=[],
        fields_added=[],
        tables_already_present=[],
        fields_already_present=[],
    )

    for table_decl in airtable_schema.TABLES_SCHEMA:
        name = table_decl["name"]
        fields_decl = table_decl["fields"]
        description = table_decl.get("description")

        if name not in live:
            plan.tables_created.append(name)
            if not dry_run:
                base.create_table(
                    name,
                    fields=[_field_payload(fd) for fd in fields_decl],
                    description=description,
                )
            continue

        plan.tables_already_present.append(name)
        live_fields = live[name]["fields"]
        table = base.table(name) if not dry_run else None
        for fd in fields_decl:
            fname = fd["name"]
            if fname in live_fields:
                plan.fields_already_present.append((name, fname))
                continue
            plan.fields_added.append((name, fname))
            if not dry_run:
                table.create_field(
                    fname,
                    fd["type"],
                    description=fd.get("description"),
                    options=fd.get("options"),
                )

    return plan


# ---------------------------------------------------------------------------
# Inbox I/O
# ---------------------------------------------------------------------------

@dataclass
class InboxRecord:
    """A subset of an Airtable Inbox record the engine cares about."""
    id: str
    name: str
    created_time: str       # ISO 8601 from Airtable
    attachments: list[dict]
    file_hash: str          # may be "" if not yet computed by a prior run
    processed: bool


def _coerce_inbox(record: dict) -> InboxRecord:
    fields = record.get("fields") or {}
    return InboxRecord(
        id=record["id"],
        name=fields.get("Name", ""),
        created_time=record.get("createdTime", ""),
        attachments=list(fields.get("Attachment") or []),
        file_hash=fields.get("File Hash", "") or "",
        processed=bool(fields.get("Processed", False)),
    )


def get_unprocessed_inbox(base, *, limit: int | None = None) -> list[InboxRecord]:
    """Return all unprocessed Inbox records, newest first (by createdTime).

    pyairtable's sort= field accepts user fields by name; createdTime is a
    system field, so we sort in Python after fetching.
    """
    raw = base.table(airtable_schema.table_spec("Inbox")["name"]).all(
        formula="NOT({Processed})",
    )
    records = [_coerce_inbox(r) for r in raw]
    # Newest first.
    records.sort(key=lambda r: r.created_time, reverse=True)
    if limit is not None:
        records = records[:limit]
    return records


def get_newest_unprocessed_inbox(base) -> InboxRecord | None:
    records = get_unprocessed_inbox(base, limit=1)
    return records[0] if records else None


def file_hash_already_processed(base, sha256_hex: str) -> bool:
    """True if a prior Processed Inbox record carries this exact File Hash."""
    if not sha256_hex:
        return False
    table = base.table(airtable_schema.table_spec("Inbox")["name"])
    hit = table.first(formula=f"AND({{File Hash}}={_formula_literal(sha256_hex)}, {{Processed}})")
    return hit is not None


# Attachment download — NO authorization header. The URL is pre-signed and
# sending the PAT can fail the request (see Step 1 research). The URL also
# expires ~2h after the record was fetched, so download inside the same run
# that read the record.
def download_attachment_bytes(attachment: dict, *, timeout_s: int = 60) -> bytes:
    url = attachment["url"]
    resp = requests.get(url, timeout=timeout_s)
    resp.raise_for_status()
    return resp.content


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Inbox writes
# ---------------------------------------------------------------------------

def mark_inbox_processed(
    base,
    record_id: str,
    *,
    file_hash: str,
    rows_in_scope: int,
    total_in_scope: float,
    processed_at_iso_date: str,
    notes: str = "",
) -> dict:
    table = base.table(airtable_schema.table_spec("Inbox")["name"])
    return table.update(
        record_id,
        {
            "Processed": True,
            "File Hash": file_hash,
            "Rows In Scope": rows_in_scope,
            "Total In Scope": total_in_scope,
            "Processed At": processed_at_iso_date,
            "Notes": notes,
        },
        typecast=True,
    )


# ---------------------------------------------------------------------------
# Run Log
# ---------------------------------------------------------------------------

# Allowed values for the Run Log singleSelect fields. Validated client-side
# before the create() call so a typo or stale option name surfaces loudly
# rather than silently spawning a new Airtable dropdown option (which is what
# typecast=True would do — see Step 2 review).
_RUN_LOG_MODES: tuple[str, ...] = ("ingest", "provision", "audit", "compute", "write")
_RUN_LOG_OUTCOMES: tuple[str, ...] = ("ok", "no_new_data", "partial", "error")


def append_run_log(
    base,
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
            f"Run Log mode {mode!r} is not one of {_RUN_LOG_MODES}. "
            f"Add to airtable_schema._RUN_MODE_CHOICES first (and to this list)."
        )
    if outcome not in _RUN_LOG_OUTCOMES:
        raise ValueError(
            f"Run Log outcome {outcome!r} is not one of {_RUN_LOG_OUTCOMES}. "
            f"Add to airtable_schema._RUN_OUTCOME_CHOICES first (and to this list)."
        )
    table = base.table(airtable_schema.table_spec("Run Log")["name"])
    payload: dict[str, Any] = {
        "Run ID": run_id,
        "Mode": mode,
        "Outcome": outcome,
        "File Name": file_name,
        "File Hash": file_hash,
        "Anomalies": anomalies,
        "Review Flags": review_flags,
        "Notes": notes,
    }
    if rows_in_scope is not None:
        payload["Rows In Scope"] = rows_in_scope
    if rows_out_of_scope is not None:
        payload["Rows Out Of Scope"] = rows_out_of_scope
    if total_in_scope is not None:
        payload["Total In Scope"] = total_in_scope
    if total_out_of_scope is not None:
        payload["Total Out Of Scope"] = total_out_of_scope
    # NOTE: typecast intentionally OMITTED here. A typo in Mode/Outcome must
    # raise a clear API error, NOT silently create a new dropdown option in
    # Airtable. The client-side validation above is the first defense.
    return table.create(payload)


# ---------------------------------------------------------------------------
# Vendor Aliases / Campus Map / Learned Mappings / Needs Tagging — Step 3
# ---------------------------------------------------------------------------

# Operator-editable aliases / campus overrides / learned attributions live in
# small Airtable tables. We fetch all rows at startup; volumes are tiny
# (dozens to low hundreds) so unsorted .all() is fine.

_SPLIT_ALIASES = re.compile(r"[,\n]")


def _formula_literal(value: str) -> str:
    """Return a safe Airtable formula string literal for `value`.

    Uses DOUBLE-quoted form. Airtable's formula language does not support
    backslash escapes inside single-quoted literals — a value like
    "Domino's" interpolated into `{Field}='...'` produces an unterminated
    string. Double-quoted form sidesteps the apostrophe case entirely;
    Tableau vendor names virtually never contain double quotes. If a value
    ever does, raise loudly rather than risk a malformed formula that
    silently fails to find a row and creates a duplicate on upsert.
    """
    if '"' in value:
        raise ValueError(
            f"Airtable formula value contains a literal double-quote, which "
            f"cannot be safely interpolated: {value!r}"
        )
    return f'"{value}"'


def _split_multiline_list(raw: str | None) -> list[str]:
    """Split a multilineText cell's value into a clean list. Operators use
    either newlines or commas; both work."""
    if not raw:
        return []
    return [p.strip() for p in _SPLIT_ALIASES.split(raw) if p.strip()]


def load_vendor_aliases(base) -> dict[str, list[str]]:
    """Return {contract_name: [alias, ...]} from the Vendor Aliases table.

    Empty alias cells produce an empty list — Step 3 attribution treats this
    as "no aliases for this contract" and falls back to the contract name
    alone."""
    table = base.table(airtable_schema.table_spec("Vendor Aliases")["name"])
    out: dict[str, list[str]] = {}
    for record in table.all():
        fields = record.get("fields") or {}
        name = (fields.get("Contract Name") or "").strip()
        if not name:
            continue
        aliases = _split_multiline_list(fields.get("Aliases"))
        # Merge if a name appears in multiple rows (operator quirk).
        out.setdefault(name, []).extend(a for a in aliases if a not in out.get(name, []))
    return out


def load_campus_map_overrides(base) -> tuple[dict[str, frozenset[str]], frozenset[str] | None]:
    """Return (forward_overrides, drop_codes_override) from the Campus Map table.

    forward_overrides: {tableau_code: frozenset[asana_option_names]} — only
        codes the operator has explicitly set. Codes not present here keep
        their config defaults.
    drop_codes_override: frozenset of Tableau codes where Drop=true. None if
        the operator has not used the Drop checkbox on any row (which means
        "fall back to config defaults"); empty frozenset means "the operator
        has deliberately turned off all drops".
    """
    table = base.table(airtable_schema.table_spec("Campus Map")["name"])
    overrides: dict[str, frozenset[str]] = {}
    drop_codes: set[str] = set()
    any_drop_checkbox_seen = False

    for record in table.all():
        fields = record.get("fields") or {}
        code = (fields.get("Tableau Code") or "").strip()
        if not code:
            continue
        is_drop = bool(fields.get("Drop", False))
        if is_drop:
            drop_codes.add(code)
            any_drop_checkbox_seen = True
            continue
        options = frozenset(_split_multiline_list(fields.get("Asana Option Names")))
        if options:
            overrides[code] = options

    return overrides, frozenset(drop_codes) if any_drop_checkbox_seen else None


def load_learned_mappings(base) -> dict[tuple[str, str, str, str], str]:
    """Return {(Campus, Dept, Account No, Vendor): Contract Name}."""
    table = base.table(airtable_schema.table_spec("Learned Mappings")["name"])
    out: dict[tuple[str, str, str, str], str] = {}
    for record in table.all():
        fields = record.get("fields") or {}
        key = (
            (fields.get("Campus") or "").strip(),
            (fields.get("Dept") or "").strip(),
            (fields.get("Account No") or "").strip(),
            (fields.get("Vendor") or "").strip(),
        )
        contract = (fields.get("Contract Name") or "").strip()
        if not contract or not all(key):
            continue
        out[key] = contract
    return out


def _find_needs_tagging_by_group_key(base, group_key: str) -> dict | None:
    table = base.table(airtable_schema.table_spec("Needs Tagging")["name"])
    return table.first(formula=f"{{Group Key}}={_formula_literal(group_key)}")


def upsert_needs_tagging_group(
    base,
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

    If a row with that Group Key exists, the engine updates the rolling
    fields (Sample Record Description, $ in group, candidate Notes) but
    NEVER overwrites a non-empty Assign Contract — that's the operator's
    answer and gets promoted to Learned Mappings on the next run.

    candidate_names lands in Notes so the operator sees the engine's vendor
    matches (if any) when filling in Assign Contract. Empty list means the
    vendor was unrecognized.
    """
    table = base.table(airtable_schema.table_spec("Needs Tagging")["name"])
    existing = _find_needs_tagging_by_group_key(base, group_key)
    candidate_lines = []
    if candidate_names:
        candidate_lines.append("Engine vendor candidates:")
        for n in candidate_names:
            candidate_lines.append(f"  - {n}")
    else:
        candidate_lines.append("No vendor candidates found.")
    engine_candidates = "\n".join(candidate_lines)

    if existing:
        # Engine owns these three rolling fields; Notes belongs to the
        # operator and is never touched on update (would clobber annotations).
        return table.update(
            existing["id"],
            {
                "Sample Record Description": sample_description,
                "$ in group": amount,
                "Engine Candidates": engine_candidates,
            },
            typecast=True,
        )
    return table.create(
        {
            "Group Key": group_key,
            "Campus": campus,
            "Dept": dept,
            "Account No": account_no,
            "Vendor": vendor,
            "Sample Record Description": sample_description,
            "$ in group": amount,
            "Created At": created_at_iso_date,
            "Engine Candidates": engine_candidates,
        },
        typecast=True,
    )


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


def promote_filled_needs_tagging(
    base,
    *,
    learned_at_iso_date: str,
    valid_contract_names: frozenset[str] | None = None,
) -> list[Promotion]:
    """For every Needs Tagging row with a filled Assign Contract:

    1. Validate Assign Contract matches an open Asana contract name (when
       valid_contract_names is provided). A typo or stale rename skips the
       promotion with a logged warning so the operator can correct it.
    2. Upsert a Learned Mappings row (by Key) with the
       (Campus, Dept, Account No, Vendor) → Contract Name mapping.
    3. Delete the Needs Tagging row.

    Idempotent against partial failures: if step 3 dies after step 2, the
    next run re-enters here, the LM upsert is a no-op match, the delete
    succeeds, and we converge.
    """
    nt_table = base.table(airtable_schema.table_spec("Needs Tagging")["name"])
    lm_table = base.table(airtable_schema.table_spec("Learned Mappings")["name"])

    # Canonical: "NOT empty". BLANK() and !='' are equivalent for text fields.
    filled = nt_table.all(formula="NOT({Assign Contract}='')")

    promotions: list[Promotion] = []
    for record in filled:
        fields = record.get("fields") or {}
        campus = (fields.get("Campus") or "").strip()
        dept = (fields.get("Dept") or "").strip()
        account_no = (fields.get("Account No") or "").strip()
        vendor = (fields.get("Vendor") or "").strip()
        contract_name = (fields.get("Assign Contract") or "").strip()
        if not all([campus, dept, account_no, vendor, contract_name]):
            log.warning(
                "Skipping Needs Tagging row %s with incomplete fields: %r",
                record["id"], fields,
            )
            continue
        if valid_contract_names is not None and contract_name not in valid_contract_names:
            log.warning(
                "Needs Tagging row %s has Assign Contract %r which does not "
                "match any open Asana contract — possible typo or stale name. "
                "Skipping promotion; please correct in Airtable.",
                record["id"], contract_name,
            )
            continue
        group_key = fields.get("Group Key") or f"{campus}|{dept}|{account_no}|{vendor}"

        # Upsert Learned Mappings by Key.
        existing_lm = lm_table.first(formula=f"{{Key}}={_formula_literal(group_key)}")
        payload = {
            "Key": group_key,
            "Campus": campus,
            "Dept": dept,
            "Account No": account_no,
            "Vendor": vendor,
            "Contract Name": contract_name,
            "Learned At": learned_at_iso_date,
            "Notes": (
                f"Promoted from Needs Tagging on {learned_at_iso_date}. "
                f"Operator selected: {contract_name}."
            ),
        }
        if existing_lm:
            lm_table.update(existing_lm["id"], payload, typecast=True)
        else:
            lm_table.create(payload, typecast=True)

        # Delete the Needs Tagging row. On a concurrent race, the other
        # worker may have already deleted it (HTTP 404) — treat that as
        # success rather than letting it break the whole loop.
        try:
            nt_table.delete(record["id"])
        except Exception as exc:  # noqa: BLE001 — pyairtable / requests variants
            log.info(
                "Needs Tagging row %s delete returned %s — likely already "
                "deleted by a concurrent run; continuing.",
                record["id"], type(exc).__name__,
            )

        promotions.append(Promotion(
            needs_tagging_record_id=record["id"],
            group_key=group_key,
            campus=campus, dept=dept, account_no=account_no, vendor=vendor,
            contract_name=contract_name,
        ))

    if promotions:
        log.info("promoted %d Needs Tagging → Learned Mappings", len(promotions))
    return promotions


# ---------------------------------------------------------------------------
# Dashboard upsert — Step 4
# ---------------------------------------------------------------------------

# Pinned client-side so a typo or stale band string raises a clear error
# rather than silently spawning a new Airtable singleSelect option via
# typecast. Derived from config.settings to keep the four sources of truth
# (Asana options, Airtable schema choices, Airtable client validator, Step 1
# audit) in lock-step; a divergence in one place fails CI loudly.
_DASHBOARD_SPENDING_RATE_ALARM_VALUES: frozenset[str] = frozenset(
    settings.ASANA_SPENDING_RATE_ALARM_OPTIONS
)
_DASHBOARD_ALARMS_VALUES: frozenset[str] = frozenset(settings.ASANA_ALARMS_OPTIONS)


# Engine-owned Dashboard fields whose value transitions None ↔ non-None
# between runs. On the UPDATE path these MUST be sent as null (rather than
# omitted from the payload) so a cell can transition BACK to blank — e.g.
# Spending Rate Alarm flipping from "75%" to blank when the operator raised
# Contract Amount and %spent dropped below 75. Without this, pyairtable's
# default PATCH-merge keeps the prior value forever.
_DASHBOARD_NULLABLE_FIELDS: tuple[str, ...] = (
    "Contract Amount",
    "% Spent",
    "Spending Rate",
    "Spending Rate Alarm",
    "Due",
    "Status",
    "PM Email",
)


def _find_dashboard_by_gid(base, asana_task_gid: str) -> dict | None:
    table = base.table(airtable_schema.table_spec("Dashboard")["name"])
    return table.first(
        formula=f"{{Asana Task GID}}={_formula_literal(asana_task_gid)}"
    )


def upsert_dashboard_row(base, row) -> dict:
    """Idempotent upsert of a Dashboard row, keyed by Asana Task GID.

    Single-select values (Spending Rate Alarm, Alarms) are validated
    client-side before the API call — a stale or typo'd band would
    otherwise be silently created as a new Airtable option under
    typecast=True. None values are omitted from the payload so cells like
    Spending Rate (blanked by the pace guard) stay empty in Airtable
    rather than being written as 0.

    `row` is an engine.compute.DashboardRow.
    """
    if row.spending_rate_alarm is not None and row.spending_rate_alarm not in _DASHBOARD_SPENDING_RATE_ALARM_VALUES:
        raise ValueError(
            f"Dashboard Spending Rate Alarm {row.spending_rate_alarm!r} is "
            f"not one of {sorted(_DASHBOARD_SPENDING_RATE_ALARM_VALUES)}. "
            f"Update airtable_schema + this validator together."
        )
    if row.alarms not in _DASHBOARD_ALARMS_VALUES:
        raise ValueError(
            f"Dashboard Alarms {row.alarms!r} is not one of "
            f"{sorted(_DASHBOARD_ALARMS_VALUES)}."
        )

    # Required + unconditional fields. Spent so far is rounded defensively
    # in case a caller passed an unrounded float; compute.py already rounds.
    payload: dict[str, Any] = {
        "Contract": row.contract_name,
        "Asana Task GID": row.asana_task_gid,
        "Campus Set": row.campus_set,
        "Spent so far": round(row.spent_so_far, 2),
        "Alarms": row.alarms,
        "Start": row.start.isoformat(),
        "Last Updated": row.last_updated.isoformat(),
    }
    # Optional nullable fields. On UPDATE we must EXPLICITLY send None to
    # clear a previously-non-blank cell; pyairtable's table.update() is a
    # PATCH-merge, so omitting the key keeps the prior value. On CREATE
    # we omit None so we don't initialize a cell as null when blank is fine.
    # The branch is decided below after the existing-record lookup.
    nullable_values: dict[str, Any] = {
        "Contract Amount":
            round(row.contract_amount, 2) if row.contract_amount is not None else None,
        "% Spent":
            round(row.pct_spent, 2) if row.pct_spent is not None else None,
        "Spending Rate":
            round(row.spending_rate, 2) if row.spending_rate is not None else None,
        "Spending Rate Alarm": row.spending_rate_alarm,
        "Due": row.due.isoformat() if row.due is not None else None,
        "Status": row.status,
        "PM Email": row.pm_email,
    }

    table = base.table(airtable_schema.table_spec("Dashboard")["name"])
    existing = _find_dashboard_by_gid(base, row.asana_task_gid)

    if existing:
        # UPDATE: include all nullable keys, sending None to clear the cell.
        # This is the fix for a cell that transitioned non-None → None
        # (e.g. Spending Rate Alarm dropping from "75%" back to blank).
        payload.update(nullable_values)
        return table.update(existing["id"], payload, typecast=True)

    # CREATE: omit None values so we don't initialize cells as null on the
    # first write — blank is the correct first-run state when no value has
    # been computed yet.
    for k, v in nullable_values.items():
        if v is not None:
            payload[k] = v
    return table.create(payload, typecast=True)


# ---------------------------------------------------------------------------
# State table I/O — Step 6 change detection
# ---------------------------------------------------------------------------
#
# Keyed by Asana Task GID (NOT Contract Name) so a rename in Asana
# self-corrects rather than orphaning the prior State row.


def _find_state_by_gid(base, asana_task_gid: str) -> dict | None:
    table = base.table(airtable_schema.table_spec("State")["name"])
    return table.first(
        formula=f"{{Asana Task GID}}={_formula_literal(asana_task_gid)}"
    )


def load_state_priors(base) -> dict[str, Any]:
    """Return {asana_task_gid: StatePrior} from the State table.

    Empty State (first-run base) returns an empty dict — every contract
    surfaces as `first_run` in the diff. Rows missing an Asana Task GID
    are skipped with a logged warning (likely manually-typed rows or
    rows from before the GID column was added).
    """
    # Imported lazily to avoid an import cycle (engine.state imports from
    # engine.compute, and main → state → airtable_client → state would
    # otherwise loop).
    from engine.state import StatePrior

    table = base.table(airtable_schema.table_spec("State")["name"])
    out: dict[str, Any] = {}
    for record in table.all():
        fields = record.get("fields") or {}
        gid = (fields.get("Asana Task GID") or "").strip()
        if not gid:
            # Could be legacy data from before GID was added — skip with a
            # log so the operator can see the orphan and clean it manually.
            name = (fields.get("Contract Name") or "").strip() or "<unnamed>"
            log.warning(
                "State row for %r has no Asana Task GID; skipping. Delete "
                "or backfill the row to clean up.", name,
            )
            continue
        name = (fields.get("Contract Name") or "").strip()
        last_updated_raw = fields.get("Last Updated At")
        last_updated_parsed: date | None = None
        if last_updated_raw:
            try:
                last_updated_parsed = date.fromisoformat(str(last_updated_raw)[:10])
            except ValueError:
                log.warning(
                    "State row %r has malformed Last Updated At %r; treating "
                    "as missing.", name or gid, last_updated_raw,
                )
        out[gid] = StatePrior(
            contract_name=name,
            asana_task_gid=gid,
            prior_spent=fields.get("Prior Spent"),
            prior_pct_spent=fields.get("Prior % Spent"),
            prior_spending_rate=fields.get("Prior Spending Rate"),
            # `or None` coerces an empty-string singleSelect (rare;
            # Airtable usually omits the key entirely for cleared cells)
            # so the StatePrior dataclass stays clean for downstream None
            # checks.
            prior_spending_rate_alarm=(fields.get("Prior Spending Rate Alarm") or None),
            prior_alarms=(fields.get("Prior Alarms") or None),
            last_processed_hash=(fields.get("Last Processed Hash") or "") or None,
            last_updated_at=last_updated_parsed,
        )
    return out


_STATE_SPENDING_RATE_ALARM_VALUES: frozenset[str] = frozenset(
    settings.ASANA_SPENDING_RATE_ALARM_OPTIONS
)
_STATE_ALARMS_VALUES: frozenset[str] = frozenset(settings.ASANA_ALARMS_OPTIONS)


def upsert_state_for_contract(
    base,
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

    Same client-side singleSelect validation + PATCH-merge nullable
    handling as Dashboard upsert: on UPDATE we explicitly send None to
    clear cells (so a Prior Spending Rate Alarm that drops from "75%"
    to blank doesn't stay stale).
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

    payload: dict[str, Any] = {
        "Contract Name": contract_name,
        "Asana Task GID": asana_task_gid,
        "Prior Spent": round(spent, 2),
        "Prior Alarms": alarms,
        "Last Processed Hash": last_processed_hash,
        "Last Updated At": last_updated_iso_date,
    }
    nullable_values: dict[str, Any] = {
        "Prior % Spent": round(pct_spent, 2) if pct_spent is not None else None,
        "Prior Spending Rate":
            round(spending_rate, 2) if spending_rate is not None else None,
        "Prior Spending Rate Alarm": spending_rate_alarm,
    }

    table = base.table(airtable_schema.table_spec("State")["name"])
    existing = _find_state_by_gid(base, asana_task_gid)

    if existing:
        # UPDATE — explicit None to clear, matching the Dashboard pattern.
        payload.update(nullable_values)
        return table.update(existing["id"], payload, typecast=True)

    # CREATE — omit None to avoid initializing cells as null.
    for k, v in nullable_values.items():
        if v is not None:
            payload[k] = v
    return table.create(payload, typecast=True)


def cleanup_stale_state(base, *, live_asana_task_gids: set[str]) -> int:
    """Delete State rows whose Asana Task GID is no longer in the live set
    (contract archived in Asana, or operator removed the task).

    Mirrors cleanup_stale_needs_tagging — engine-owned table, operator
    doesn't hand-edit State, so unconditional cleanup is safe. Returns the
    count of deleted rows.
    """
    table = base.table(airtable_schema.table_spec("State")["name"])
    deleted = 0
    for record in table.all():
        fields = record.get("fields") or {}
        gid = (fields.get("Asana Task GID") or "").strip()
        if gid and gid not in live_asana_task_gids:
            try:
                table.delete(record["id"])
                deleted += 1
            except Exception as exc:  # noqa: BLE001
                log.info(
                    "stale State delete on %s returned %s; continuing.",
                    record["id"], type(exc).__name__,
                )
    if deleted:
        log.info("cleaned up %d stale State row(s)", deleted)
    return deleted


def cleanup_stale_needs_tagging(base, *, live_group_keys: set[str]) -> int:
    """Delete Needs Tagging rows whose Group Key is NOT in live_group_keys
    AND whose Assign Contract is empty.

    A group that was ambiguous on a prior run but is now auto-attributed
    (operator added a Vendor Alias, etc.) leaves a stale "please tag me"
    row behind. Without this cleanup the operator sees stale review work.

    Filled rows (operator answers in flight) are NEVER deleted by this
    path — they are the promotion queue's responsibility.

    Returns the count of deleted rows."""
    table = base.table(airtable_schema.table_spec("Needs Tagging")["name"])
    candidates = table.all(formula="{Assign Contract}=''")
    deleted = 0
    for record in candidates:
        fields = record.get("fields") or {}
        gk = (fields.get("Group Key") or "").strip()
        if gk and gk not in live_group_keys:
            try:
                table.delete(record["id"])
                deleted += 1
            except Exception as exc:  # noqa: BLE001
                log.info(
                    "stale Needs Tagging delete on %s returned %s; continuing.",
                    record["id"], type(exc).__name__,
                )
    if deleted:
        log.info("cleaned up %d stale Needs Tagging row(s)", deleted)
    return deleted


__all__ = [
    "SchemaPlan",
    "InboxRecord",
    "Promotion",
    "get_api_and_base",
    "ensure_schema",
    "get_unprocessed_inbox",
    "get_newest_unprocessed_inbox",
    "file_hash_already_processed",
    "download_attachment_bytes",
    "sha256_hex",
    "mark_inbox_processed",
    "append_run_log",
    "load_vendor_aliases",
    "load_campus_map_overrides",
    "load_learned_mappings",
    "upsert_needs_tagging_group",
    "promote_filled_needs_tagging",
    "cleanup_stale_needs_tagging",
    "upsert_dashboard_row",
    "load_state_priors",
    "upsert_state_for_contract",
    "cleanup_stale_state",
]
