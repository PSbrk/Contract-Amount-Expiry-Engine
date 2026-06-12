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
from dataclasses import dataclass
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
    safe = sha256_hex.replace("'", "\\'")
    table = base.table(airtable_schema.table_spec("Inbox")["name"])
    hit = table.first(formula=f"AND({{File Hash}}='{safe}', {{Processed}})")
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


__all__ = [
    "SchemaPlan",
    "InboxRecord",
    "get_api_and_base",
    "ensure_schema",
    "get_unprocessed_inbox",
    "get_newest_unprocessed_inbox",
    "file_hash_already_processed",
    "download_attachment_bytes",
    "sha256_hex",
    "mark_inbox_processed",
    "append_run_log",
]
