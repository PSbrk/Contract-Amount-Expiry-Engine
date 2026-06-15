"""Declarative storage schema for the eight engine tables (SQLite).

The engine's ensure_schema() reads this at startup and creates any missing
tables / columns in data/engine.db. Edits here are the right place to evolve
the schema -- the SQLite client is idempotent and never drops a column
silently (a rename is treated as "missing"; add the new name and remove
the old by hand if you really want that).

Field type strings are kept in their original declarative form
(singleLineText, multilineText, singleSelect, date, checkbox, number);
sqlite_column_type() below translates them to SQLite column-type clauses.
This indirection keeps the declarations human-readable and gives a single
choke point for storage-type translation.

singleSelect choices are declared as [{"name": "..."}] dicts. Names mirror
the Asana option names exactly so the dashboard's mental model lines up
with what an operator sees in Asana.
"""

from __future__ import annotations

from typing import Final


# Single-select options -- kept centralized so Dashboard, State, and the
# sqlite_client validators stay in lock-step with the Asana option names.
# Order is the natural display order.
_SPENDING_RATE_ALARM_CHOICES: Final = [
    {"name": "75%"},
    {"name": "90%"},
    {"name": "100%"},
    {"name": "Over"},
]
_ALARMS_CHOICES: Final = [
    {"name": "Clear"},
    {"name": "ALARM"},
]
_RUN_MODE_CHOICES: Final = [
    {"name": "ingest"},
    {"name": "provision"},
    {"name": "audit"},
    {"name": "compute"},
    {"name": "write"},
]
_RUN_OUTCOME_CHOICES: Final = [
    {"name": "ok"},
    {"name": "no_new_data"},
    {"name": "partial"},
    {"name": "error"},
]


# Each table is {name, description?, fields: [...]}.
# Field is {name, type, options?, description?}.
TABLES_SCHEMA: Final = [
    {
        "name": "Inbox",
        "description": (
            "One row per Tableau export the engine has processed. Acts as the "
            "dedup audit log -- file_hash is unique and the upsert path raises "
            "DuplicateTransactionsError on a hit. The actual export files live "
            "on disk (data/inbox/ before ingest, data/processed/ after)."
        ),
        "fields": [
            {"name": "Name", "type": "singleLineText",
             "description": "Human label -- typically the filename."},
            {"name": "File Hash", "type": "singleLineText",
             "description": "SHA-256 of the file bytes. UNIQUE -- drives dedup."},
            {"name": "Processed", "type": "checkbox",
             "description": "Always 1 in the SQLite era (a row exists only after processing)."},
            {"name": "Processed At", "type": "date",
             "description": "ISO date the engine processed this file (UTC)."},
            {"name": "Rows In Scope", "type": "number", "options": {"precision": 0}},
            {"name": "Total In Scope", "type": "number", "options": {"precision": 2}},
            {"name": "Notes", "type": "multilineText"},
        ],
    },
    {
        "name": "Dashboard",
        "description": (
            "One row per live contract. Mirrors the five Asana write fields "
            "plus context (Campus, Start, Due, Status, PM). Populated by the "
            "compute step on every successful --ingest."
        ),
        "fields": [
            {"name": "Contract", "type": "singleLineText",
             "description": "Asana task name."},
            {"name": "Asana Task GID", "type": "singleLineText",
             "description": "Stable identity. UNIQUE -- upsert key."},
            {"name": "Campus Set", "type": "singleLineText",
             "description": "Comma-joined Asana Campus option names for the contract."},
            {"name": "Contract Amount", "type": "number", "options": {"precision": 2}},
            {"name": "Spent so far", "type": "number", "options": {"precision": 2}},
            {"name": "% Spent", "type": "number", "options": {"precision": 2},
             "description": "Stored as a percentage number (75.00 == 75%) to match Asana's field."},
            {"name": "Spending Rate", "type": "number", "options": {"precision": 2}},
            {"name": "Spending Rate Alarm", "type": "singleSelect",
             "options": {"choices": _SPENDING_RATE_ALARM_CHOICES}},
            {"name": "Alarms", "type": "singleSelect",
             "options": {"choices": _ALARMS_CHOICES}},
            {"name": "Start", "type": "date"},
            {"name": "Due", "type": "date"},
            {"name": "Status", "type": "singleLineText",
             "description": "Asana Contract Status option name (Active, etc.)."},
            {"name": "PM Email", "type": "singleLineText"},
            {"name": "Last Updated", "type": "date"},
        ],
    },
    {
        "name": "Needs Tagging",
        "description": (
            "Ambiguous / unmatched attribution groupings. Operator sets "
            "Assign Contract once in the web UI; engine promotes filled rows "
            "into Learned Mappings on the next run."
        ),
        "fields": [
            {"name": "Group Key", "type": "singleLineText",
             "description": "Synthetic primary: 'Campus|Dept|AccountNo|Vendor'. UNIQUE."},
            {"name": "Campus", "type": "singleLineText"},
            {"name": "Dept", "type": "singleLineText"},
            {"name": "Account No", "type": "singleLineText"},
            {"name": "Vendor", "type": "singleLineText"},
            {"name": "Sample Record Description", "type": "multilineText"},
            {"name": "$ in group", "type": "number", "options": {"precision": 2}},
            {"name": "Assign Contract", "type": "singleLineText",
             "description": (
                 "Contract name (Asana task name) the operator wants this "
                 "grouping attributed to. Leave blank if unmatched."
             )},
            {"name": "Created At", "type": "date"},
            {"name": "Engine Candidates", "type": "multilineText",
             "description": (
                 "Engine-managed: vendor fuzzy-match candidates from the "
                 "last run. Rewritten every upsert."
             )},
            {"name": "Notes", "type": "multilineText",
             "description": "Operator-editable. The engine never writes here."},
        ],
    },
    {
        "name": "Vendor Aliases",
        "description": (
            "Asana contract task name -> Tableau Vendor formatting variants. "
            "Aliases is a multiline list (newline- or comma-separated)."
        ),
        "fields": [
            {"name": "Contract Name", "type": "singleLineText",
             "description": "Asana task name. UNIQUE."},
            {"name": "Aliases", "type": "multilineText",
             "description": "Tableau Vendor strings that should match this contract."},
            {"name": "Notes", "type": "multilineText"},
        ],
    },
    {
        "name": "Campus Map",
        "description": (
            "Tableau campus code -> Asana Campus option names. Engine reads "
            "at startup and overrides config/campus_map.py defaults with any "
            "rows found here. Drop=1 removes a Tableau code from ingestion entirely."
        ),
        "fields": [
            {"name": "Tableau Code", "type": "singleLineText",
             "description": "Tableau campus code (CEN, OMH, etc.). UNIQUE."},
            {"name": "Asana Option Names", "type": "multilineText",
             "description": (
                 "Comma- or newline-separated Asana Campus option names this "
                 "Tableau code maps to. Empty when Drop is true."
             )},
            {"name": "Drop", "type": "checkbox",
             "description": "True drops all transactions with this Tableau code."},
            {"name": "Notes", "type": "multilineText"},
        ],
    },
    {
        "name": "Learned Mappings",
        "description": (
            "(Campus, Dept, Account No, Vendor) -> Contract attribution, "
            "persisted from operator answers in Needs Tagging."
        ),
        "fields": [
            {"name": "Key", "type": "singleLineText",
             "description": "Synthetic primary: 'Campus|Dept|AccountNo|Vendor'. UNIQUE."},
            {"name": "Campus", "type": "singleLineText"},
            {"name": "Dept", "type": "singleLineText"},
            {"name": "Account No", "type": "singleLineText"},
            {"name": "Vendor", "type": "singleLineText"},
            {"name": "Contract Name", "type": "singleLineText"},
            {"name": "Learned At", "type": "date"},
            {"name": "Notes", "type": "multilineText"},
        ],
    },
    {
        "name": "State",
        "description": (
            "Per-contract prior totals + prior alarm state, for change "
            "detection on each run. Populated by the compute step. Keyed by "
            "Asana Task GID (NOT Contract Name) so a contract rename in "
            "Asana self-corrects rather than orphaning the prior State row."
        ),
        "fields": [
            {"name": "Contract Name", "type": "singleLineText",
             "description": "Human label, for at-a-glance scan."},
            {"name": "Asana Task GID", "type": "singleLineText",
             "description": "Stable identity. UNIQUE -- upsert key."},
            {"name": "Prior Spent", "type": "number", "options": {"precision": 2}},
            {"name": "Prior % Spent", "type": "number", "options": {"precision": 2}},
            {"name": "Prior Spending Rate", "type": "number", "options": {"precision": 2}},
            {"name": "Prior Spending Rate Alarm", "type": "singleSelect",
             "options": {"choices": _SPENDING_RATE_ALARM_CHOICES}},
            {"name": "Prior Alarms", "type": "singleSelect",
             "options": {"choices": _ALARMS_CHOICES}},
            {"name": "Last Processed Hash", "type": "singleLineText",
             "description": "File hash whose run wrote these prior totals."},
            {"name": "Last Updated At", "type": "date"},
        ],
    },
    {
        "name": "Run Log",
        "description": (
            "One row per engine run. Rolling-window pruned to "
            "RUN_LOG_RETENTION_DAYS (default 365) at the end of every run."
        ),
        "fields": [
            {"name": "Run ID", "type": "singleLineText",
             "description": "ISO timestamp of run start."},
            {"name": "Mode", "type": "singleSelect",
             "options": {"choices": _RUN_MODE_CHOICES}},
            {"name": "Outcome", "type": "singleSelect",
             "options": {"choices": _RUN_OUTCOME_CHOICES}},
            {"name": "File Name", "type": "singleLineText"},
            {"name": "File Hash", "type": "singleLineText"},
            {"name": "Rows In Scope", "type": "number", "options": {"precision": 0}},
            {"name": "Rows Out Of Scope", "type": "number", "options": {"precision": 0}},
            {"name": "Total In Scope", "type": "number", "options": {"precision": 2}},
            {"name": "Total Out Of Scope", "type": "number", "options": {"precision": 2}},
            {"name": "Anomalies", "type": "multilineText",
             "description": "Free-text anomalies the engine spotted."},
            {"name": "Review Flags", "type": "multilineText",
             "description": "Contracts whose total changed and warrant a human eyeball."},
            {"name": "Notes", "type": "multilineText"},
        ],
    },
]


TABLE_NAMES: Final = tuple(t["name"] for t in TABLES_SCHEMA)


def table_spec(name: str) -> dict:
    """Return the declared spec for a table by name, or raise KeyError."""
    for t in TABLES_SCHEMA:
        if t["name"] == name:
            return t
    raise KeyError(
        f"no declared schema for table {name!r}; known: {TABLE_NAMES}"
    )


def field_spec(table_name: str, field_name: str) -> dict:
    """Return the declared spec for a field on a table, or raise KeyError."""
    t = table_spec(table_name)
    for f in t["fields"]:
        if f["name"] == field_name:
            return f
    raise KeyError(
        f"no declared field {field_name!r} on table {table_name!r}"
    )


def sqlite_column_type(field_decl: dict) -> str:
    """Map a declarative field type to a SQLite column-type clause.

    - singleLineText / multilineText / singleSelect / date  -> TEXT
        (dates are stored as ISO YYYY-MM-DD strings -- SQLite has no
        native date type, and string ordering matches calendar order
        for ISO form).
    - checkbox                                              -> INTEGER NOT NULL DEFAULT 0
        (so a missing value reads as falsy without a NULL check at
        every callsite).
    - number with precision == 0                            -> INTEGER
    - number with precision >= 1                            -> REAL
    """
    ft = field_decl["type"]
    if ft in ("singleLineText", "multilineText", "singleSelect", "date"):
        return "TEXT"
    if ft == "checkbox":
        return "INTEGER NOT NULL DEFAULT 0"
    if ft == "number":
        prec = (field_decl.get("options") or {}).get("precision", 0)
        return "INTEGER" if prec == 0 else "REAL"
    raise ValueError(f"unsupported field type {ft!r} for SQLite mapping")


__all__ = [
    "TABLES_SCHEMA",
    "TABLE_NAMES",
    "table_spec",
    "field_spec",
    "sqlite_column_type",
]
