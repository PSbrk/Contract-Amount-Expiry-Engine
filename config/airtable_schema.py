"""Declarative Airtable schema for the eight tables.

The engine reads this at startup and creates missing tables / adds missing
fields against the base specified by AIRTABLE_BASE_ID. Edits here are the
right place to evolve the schema — engine.airtable_client.ensure_schema is
idempotent and renames are not attempted (rename a field manually in the
Airtable UI if needed; the engine logs a notice but does not destroy data).

Constraints baked in:
- The FIRST field in each table is the PRIMARY field. Airtable does not let
  the primary field be deleted later, only renamed. Pick a stable identifier.
- Field type strings match Airtable's create-field API exactly. Common gotchas:
  attachment is "multipleAttachments" (not "attachment"); long text is
  "multilineText" (not "longText").
- singleSelect choices are declared as [{"name": "..."}] dicts. Names mirror
  Asana option names exactly so the dashboard's mental model lines up with
  what an operator sees in Asana.
- Cross-table links (Assign Contract → Dashboard, etc.) are held as plain
  singleLineText for Step 2. multipleRecordLinks promotion can come later
  without losing data; the values stay readable in either shape.
"""

from __future__ import annotations

from typing import Final


# Single-select options — kept centralized so Dashboard, State, and the
# audit stay in lock-step with the Asana option names. NOTE: order is the
# display order in the Airtable UI dropdown.
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
            "Tableau exports dropped here as attachments. Engine picks the "
            "newest unprocessed record, hashes the file, parses it, and marks "
            "Processed=true with the result."
        ),
        "fields": [
            {"name": "Name", "type": "singleLineText",
             "description": "Human label — typically the filename. Primary field."},
            {"name": "Attachment", "type": "multipleAttachments",
             "description": "Drop one .csv or .xlsx export here per record."},
            {"name": "File Hash", "type": "singleLineText",
             "description": "SHA-256 of the attachment bytes. Set by engine."},
            {"name": "Processed", "type": "checkbox",
             "options": {"icon": "check", "color": "greenBright"},
             "description": "True once engine has finished processing this record."},
            {"name": "Processed At", "type": "date",
             "options": {"dateFormat": {"name": "iso", "format": "YYYY-MM-DD"}},
             "description": "Date the engine processed this record (UTC)."},
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
            "compute step (Step 4); empty at Step 2."
        ),
        "fields": [
            {"name": "Contract", "type": "singleLineText",
             "description": "Asana task name — primary field."},
            {"name": "Asana Task GID", "type": "singleLineText"},
            # DEFERRED (track to Step 4): spec §3 frames this as "Campus set",
            # a multi-value field. Step 2 keeps it as comma-joined text to avoid
            # binding Airtable's multipleSelects choices to the Asana Campus
            # option list (which is dynamic). Promote to multipleSelects when
            # Step 4 starts writing Dashboard rows and the option set is
            # known stable.
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
            {"name": "Start", "type": "date",
             "options": {"dateFormat": {"name": "iso", "format": "YYYY-MM-DD"}}},
            {"name": "Due", "type": "date",
             "options": {"dateFormat": {"name": "iso", "format": "YYYY-MM-DD"}}},
            {"name": "Status", "type": "singleLineText",
             "description": "Asana Contract Status option name (Active, etc.)."},
            {"name": "PM Email", "type": "singleLineText"},
            {"name": "Last Updated", "type": "date",
             "options": {"dateFormat": {"name": "iso", "format": "YYYY-MM-DD"}}},
        ],
    },
    {
        "name": "Needs Tagging",
        "description": (
            "Ambiguous attribution groupings. Operator sets Assign Contract "
            "once; engine reads confirmed rows into Learned Mappings. "
            "DEFERRED (Step 4): Assign Contract is singleLineText for now; "
            "promote to multipleRecordLinks → Dashboard before Step 4 "
            "promotion logic goes live so typos don't silently fail attribution."
        ),
        "fields": [
            {"name": "Group Key", "type": "singleLineText",
             "description": "Synthetic primary: 'Campus|Dept|AccountNo|Vendor'."},
            {"name": "Campus", "type": "singleLineText"},
            {"name": "Dept", "type": "singleLineText"},
            {"name": "Account No", "type": "singleLineText"},
            {"name": "Vendor", "type": "singleLineText"},
            {"name": "Sample Record Description", "type": "multilineText"},
            {"name": "$ in group", "type": "number", "options": {"precision": 2}},
            {"name": "Assign Contract", "type": "singleLineText",
             "description": (
                 "Contract name (Asana task name) to attribute this grouping to. "
                 "Leave blank if unmatched. Engine promotes filled values into "
                 "Learned Mappings on the next run."
             )},
            {"name": "Created At", "type": "date",
             "options": {"dateFormat": {"name": "iso", "format": "YYYY-MM-DD"}}},
            {"name": "Engine Candidates", "type": "multilineText",
             "description": (
                 "Engine-managed: the candidate contract names the vendor "
                 "fuzzy-matched on the last run. Rewritten every upsert."
             )},
            {"name": "Notes", "type": "multilineText",
             "description": "Operator-editable. The engine never writes here."},
        ],
    },
    {
        "name": "Vendor Aliases",
        "description": (
            "Asana contract task name ↔ Tableau Vendor formatting variants. "
            "Aliases is a multiline list (newline- or comma-separated)."
        ),
        "fields": [
            {"name": "Contract Name", "type": "singleLineText",
             "description": "Asana task name. Primary field."},
            {"name": "Aliases", "type": "multilineText",
             "description": "Tableau Vendor strings that should match this contract."},
            {"name": "Notes", "type": "multilineText"},
        ],
    },
    {
        "name": "Campus Map",
        "description": (
            "Tableau campus code → Asana Campus option names. Engine reads at "
            "startup and overrides config/campus_map.py defaults with any rows "
            "found here. Drop=true removes a Tableau code from ingestion entirely."
        ),
        "fields": [
            {"name": "Tableau Code", "type": "singleLineText",
             "description": "Tableau campus code (CEN, OMH, etc.). Primary field."},
            {"name": "Asana Option Names", "type": "multilineText",
             "description": (
                 "Comma- or newline-separated Asana Campus option names this "
                 "Tableau code maps to. Empty when Drop is true."
             )},
            {"name": "Drop", "type": "checkbox",
             "options": {"icon": "xCircle", "color": "redBright"},
             "description": "True drops all transactions with this Tableau code."},
            {"name": "Notes", "type": "multilineText"},
        ],
    },
    {
        "name": "Learned Mappings",
        "description": (
            "(Campus, Dept, Account No, Vendor) → Contract attribution, "
            "persisted from operator answers in Needs Tagging."
        ),
        "fields": [
            {"name": "Key", "type": "singleLineText",
             "description": "Synthetic primary: 'Campus|Dept|AccountNo|Vendor'."},
            {"name": "Campus", "type": "singleLineText"},
            {"name": "Dept", "type": "singleLineText"},
            {"name": "Account No", "type": "singleLineText"},
            {"name": "Vendor", "type": "singleLineText"},
            {"name": "Contract Name", "type": "singleLineText"},
            {"name": "Learned At", "type": "date",
             "options": {"dateFormat": {"name": "iso", "format": "YYYY-MM-DD"}}},
            {"name": "Notes", "type": "multilineText"},
        ],
    },
    {
        "name": "State",
        "description": (
            "Per-contract prior totals and prior alarm state, for change "
            "detection on each run. Populated by the compute step (Step 4); "
            "empty at Step 2. Keyed by Asana Task GID (NOT Contract Name) "
            "so a contract rename in Asana self-corrects rather than "
            "orphaning the prior State row."
        ),
        "fields": [
            {"name": "Contract Name", "type": "singleLineText",
             "description": "Human label — primary field for at-a-glance scan."},
            {"name": "Asana Task GID", "type": "singleLineText",
             "description": "Stable identity. Engine looks up State rows by this field."},
            {"name": "Prior Spent", "type": "number", "options": {"precision": 2}},
            {"name": "Prior % Spent", "type": "number", "options": {"precision": 2}},
            {"name": "Prior Spending Rate", "type": "number", "options": {"precision": 2}},
            {"name": "Prior Spending Rate Alarm", "type": "singleSelect",
             "options": {"choices": _SPENDING_RATE_ALARM_CHOICES}},
            {"name": "Prior Alarms", "type": "singleSelect",
             "options": {"choices": _ALARMS_CHOICES}},
            {"name": "Last Processed Hash", "type": "singleLineText",
             "description": "File hash whose run wrote these prior totals."},
            {"name": "Last Updated At", "type": "date",
             "options": {"dateFormat": {"name": "iso", "format": "YYYY-MM-DD"}}},
        ],
    },
    {
        "name": "Run Log",
        "description": (
            "One row per engine run. Engine appends; operator can trim a "
            "rolling window in the Airtable UI. "
            "DEFERRED (Step 8): automated rolling-window prune as a post-run "
            "step in the GitHub Actions workflow."
        ),
        "fields": [
            {"name": "Run ID", "type": "singleLineText",
             "description": "ISO timestamp of run start. Primary field."},
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
             "description": "Free-text anomalies the engine spotted (large credits, missing in-scope accounts, etc.)."},
            {"name": "Review Flags", "type": "multilineText",
             "description": "Contracts whose total changed and warrant a human eyeball."},
            {"name": "Notes", "type": "multilineText"},
        ],
    },
]


# Convenience lookups derived from TABLES_SCHEMA.
TABLE_NAMES: Final = tuple(t["name"] for t in TABLES_SCHEMA)


def table_spec(name: str) -> dict:
    """Return the declared spec for a table by name, or raise KeyError."""
    for t in TABLES_SCHEMA:
        if t["name"] == name:
            return t
    raise KeyError(f"no declared schema for table {name!r}; "
                   f"known: {TABLE_NAMES}")


def field_spec(table_name: str, field_name: str) -> dict:
    """Return the declared spec for a field on a table, or raise KeyError."""
    t = table_spec(table_name)
    for f in t["fields"]:
        if f["name"] == field_name:
            return f
    raise KeyError(f"no declared field {field_name!r} on table {table_name!r}")
