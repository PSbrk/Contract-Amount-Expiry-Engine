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
    {"name": "Previously Alarmed"},
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
            {"name": "Contract Reason Text", "type": "multilineText",
             "description": (
                 "Operator-authored description of what this contract "
                 "covers. Surfaced in the Vendor Conflicts UI so the "
                 "operator can compare it against the Tableau Sample "
                 "Record Description when picking which task a same-vendor "
                 "ambiguous group belongs to."
             )},
            {"name": "Last Updated", "type": "date"},
        ],
    },
    {
        "name": "Needs Tagging",
        "description": (
            "Ambiguous / unmatched attribution groupings. Operator sets "
            "Assign Contract once in the web UI; engine promotes filled rows "
            "into Learned Mappings on the next run. Operator can also "
            "Dismiss a row as Irrelevant -- engine then leaves it alone "
            "on future runs (no re-detect loop, no clutter)."
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
            {"name": "First Date", "type": "date",
             "description": "Earliest transaction Date in the group -- helps the operator look up source rows."},
            {"name": "Last Date", "type": "date",
             "description": "Latest transaction Date in the group."},
            {"name": "Assign Contract", "type": "singleLineText",
             "description": (
                 "Contract name (Asana task name) the operator wants this "
                 "grouping attributed to. Leave blank if unmatched."
             )},
            {"name": "Dismissed", "type": "checkbox",
             "description": (
                 "Operator-set: 1 = irrelevant (other department's spend, "
                 "etc.). Engine skips re-upserting dismissed rows and "
                 "cleanup_stale never deletes them."
             )},
            {"name": "Once Off", "type": "checkbox",
             "description": (
                 "Operator-set: 1 = the current transactions in this group "
                 "are valid one-offs (no ongoing relationship) and should "
                 "be hidden for now. UNLIKE Dismissed, the engine will "
                 "RE-SURFACE the row on a future ingest IF new transactions "
                 "arrive in the group — i.e. if the group's Last Date "
                 "advances past the Once Off Anchor. cleanup_stale never "
                 "deletes Once Off rows; the anchor would be lost."
             )},
            {"name": "Once Off Anchor", "type": "date",
             "description": (
                 "Engine-managed: the group's Last Date at the moment the "
                 "operator marked it Once Off. The resurface check compares "
                 "the incoming export's Last Date against this anchor — any "
                 "transaction dated after the anchor is 'new activity' and "
                 "trips the once-off flag back to 0."
             )},
            {"name": "Conflict Other", "type": "checkbox",
             "description": (
                 "Operator-set: 1 = none of the engine's vendor candidates "
                 "fit this group (e.g. payment is for a campus none of the "
                 "candidate Asana tasks cover). Hides the row from the "
                 "Vendor Conflicts review panel while leaving it in the "
                 "Open Needs Tagging queue so the operator can resolve it "
                 "via Assign Contract. Operator-owned: never touched by "
                 "the engine upsert."
             )},
            {"name": "Is P-Card", "type": "checkbox",
             "description": (
                 "Engine-set: 1 = row is a likely purchasing-card / journal "
                 "transaction (blank Vendor, description doesn't fit the "
                 "'Bill - <Vendor>:' AP pattern). Hides the row from Needs "
                 "Tagging Open and Vendor Conflicts; surfaces it on the "
                 "/p-card-spend audit page instead. Recomputed by the "
                 "engine on every upsert based on current Vendor + "
                 "description, so the classifier can evolve without an "
                 "operator-driven flip."
             )},
            {"name": "P-Card Ignored", "type": "checkbox",
             "description": (
                 "Operator-set: 1 = row has been 'ignored once' from the "
                 "P-Card Spend view (operator has eyeballed it and wants "
                 "it off the active list). Restorable. Operator-owned: "
                 "never touched by the engine upsert."
             )},
            {"name": "Out Of Term", "type": "checkbox",
             "description": (
                 "Engine-set: 1 = group's ambiguity is purely date-driven "
                 "(every candidate's [Start, Due] excludes the row dates). "
                 "Phase 14a uses this to route the row to Vendor Conflicts "
                 "even when there's only one candidate, so the operator "
                 "can pick the new 'Unassigned - Pre-dates Asana Record' "
                 "option or extend the contract's term in Asana. "
                 "Recomputed by the engine on every upsert."
             )},
            {"name": "Coding Mismatch", "type": "checkbox",
             "description": (
                 "Engine-set: 1 = the vendor matches a LIVE contract that "
                 "aligns on campus + term, and the ONLY difference is "
                 "Dept/Acct coding. Routes the row to the /miscoded tab "
                 "instead of Needs Tagging Open / Vendor Conflicts, where "
                 "the operator decides 'Miscoded' (attribute anyway, via a "
                 "coding-bypassing pinned Learned Mapping) or 'Correctly "
                 "coded' (Coding Confirmed below). Engine Candidate Gids "
                 "holds the campus+term-aligned candidate(s). Recomputed "
                 "by the engine on every upsert."
             )},
            {"name": "Coding Confirmed", "type": "checkbox",
             "description": (
                 "Operator-set: 1 = operator reviewed the Coding Mismatch "
                 "and confirmed the Dept/Acct difference is legitimate — "
                 "these dollars genuinely don't belong to that contract. "
                 "Stays unattributed, moves to the 'Confirmed correct' view "
                 "of /miscoded, and is genuinely-separate spend (shows in "
                 "the no-contract export). Operator-owned: never touched by "
                 "the engine upsert."
             )},
            {"name": "Created At", "type": "date"},
            {"name": "Engine Candidates", "type": "multilineText",
             "description": (
                 "Engine-managed: vendor fuzzy-match candidates from the "
                 "last run. Rewritten every upsert."
             )},
            {"name": "Engine Candidate Gids", "type": "multilineText",
             "description": (
                 "Engine-managed: newline-separated Asana task GIDs for the "
                 "candidate contracts (parallel to Engine Candidates). "
                 "Drives the Vendor Conflicts review panel — lets the UI "
                 "show same-name candidates as distinct picker rows."
             )},
            {"name": "Distinct Descriptions JSON", "type": "multilineText",
             "description": (
                 "Engine-managed JSON: list of {description, rows, amount} "
                 "for every UNIQUE Record Description in this conflict "
                 "group. Powers the per-description dropdown picker in the "
                 "Vendor Conflicts UI — one row per distinct description, "
                 "with row count and total dollar amount, so the operator "
                 "knows the weight of each pick. Empty for non-conflict "
                 "groups (unmatched rows have no candidates to choose between)."
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
             "description": (
                 "Asana task name. NOT unique: the operator may split a "
                 "contract's alias list across multiple rows for readability."
             )},
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
            {"name": "Contract Gid", "type": "singleLineText",
             "description": (
                 "Optional Asana task GID — pinned by the Vendor Conflicts "
                 "review panel when multiple open tasks share a contract "
                 "name. When set, attribution prefers this exact task over "
                 "the name-based resolution (campus + date narrowing). "
                 "Blank = legacy name-only learned mapping."
             )},
            {"name": "Description Pattern", "type": "singleLineText",
             "description": (
                 "Optional case-insensitive substring of the Tableau Record "
                 "Description. When set, this Learned Mapping only applies "
                 "to rows whose description CONTAINS the pattern — letting "
                 "the operator split a single (Campus, Dept, Acct, Vendor) "
                 "group across multiple Asana tasks by line-item scope "
                 "(e.g. 'Groundskeeping' → landscaping contract; 'Snow' → "
                 "snow contract). Blank = applies to the whole group "
                 "(legacy behavior). Pattern-bearing LMs are matched FIRST; "
                 "if none hit, the group-level LM (if any) is used."
             )},
            {"name": "Ignore Coding", "type": "checkbox",
             "description": (
                 "1 = this mapping was created from the Miscoded? tab "
                 "('Accept as miscoded'): the operator declared that the "
                 "Dept/Acct coding differs but the spend belongs to this "
                 "contract anyway. The gid-pinned learned path already "
                 "bypasses the coding-narrow, so this flag is the MARKER "
                 "that lets the Miscoded? 'Accepted' view list these "
                 "coding-overrides distinctly from ordinary name/conflict "
                 "mappings. Blank for all normal Learned Mappings."
             )},
            {"name": "Cross-Campus Exception", "type": "checkbox",
             "description": (
                 "1 = the operator DELIBERATELY assigned this row's spend to a "
                 "contract whose campus differs from the row's Tableau campus "
                 "(e.g. WAR-coded spend billed to a CEN contract). Only a "
                 "mapping with this flag set may attribute across campus; "
                 "unflagged cross-campus mappings are treated as accidental "
                 "leaks and blocked. Set automatically at promotion time when "
                 "the assigned contract does not serve the row's campus. "
                 "Replaces the old blanket ***NOR/***TUL crosswalk overrides."
             )},
            {"name": "Learned At", "type": "date"},
            {"name": "Notes", "type": "multilineText"},
        ],
    },
    {
        "name": "CapEx Budgets",
        "description": (
            "Operator-entered total budget per CapEx ID (Project ID). The "
            "budget lives in a Google Doc; the operator types it once here "
            "(bulk paste-grid or per-ID prompt) and it's the denominator for "
            "that project's % Spent across EVERY contract carrying the ID. "
            "CapEx is cumulative-to-date with no term window — see engine.capex."
        ),
        "fields": [
            {"name": "CapEx ID", "type": "singleLineText",
             "description": (
                 "Normalized project id (strip + upper), matches Asana CapEx "
                 "ID and Tableau Project ID. UNIQUE — upsert key."
             )},
            {"name": "Budget", "type": "number", "options": {"precision": 2},
             "description": "Total project budget in dollars."},
            {"name": "Entered At", "type": "date"},
            {"name": "Notes", "type": "multilineText",
             "description": "Operator-editable (e.g. which Google Doc / approval)."},
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
        "name": "Amendment Links",
        "description": (
            "Operator-declared 'this task is an amendment of that task' links "
            "between Asana contract tasks. Created from the Vendor Conflicts "
            "UI when the operator recognizes that two candidate tasks for the "
            "same vendor are actually one logical contract (the engine's "
            "name-only matching can't infer that). The Dashboard renders "
            "linked rows with a cross-reference so the operator sees the "
            "full picture (parent budget + amendment budget = combined "
            "commitment, parent spent + amendment spent = combined activity)."
        ),
        "fields": [
            {"name": "Parent Gid", "type": "singleLineText",
             "description": "Asana task GID of the 'parent' contract -- the prior contract that the amendment extends."},
            {"name": "Amendment Gid", "type": "singleLineText",
             "description": (
                 "Asana task GID of the amendment task. UNIQUE: one task can "
                 "be an amendment of at most one parent. (1:N is supported on "
                 "the other side -- one parent may have multiple amendments.)"
             )},
            {"name": "Parent Name", "type": "singleLineText",
             "description": "Snapshot of the parent's Asana task name at link time, for display when Dashboard is empty."},
            {"name": "Amendment Name", "type": "singleLineText",
             "description": "Snapshot of the amendment's Asana task name at link time."},
            {"name": "Linked At", "type": "date"},
            {"name": "Notes", "type": "multilineText"},
        ],
    },
    {
        "name": "Resolved Contracts",
        "description": (
            "Operator-set: contracts the operator has acknowledged and no "
            "longer wants alarm churn for. While a contract's GID is here, "
            "Step 5 writes ONLY the numeric fields (Spent so far, % Spent, "
            "Spending Rate) and SUPPRESSES the two alarm enums (Alarms, "
            "Spending Rate Alarm), so the operator's email-on-ALARM Asana rule "
            "stops firing. Re-arm: if a later ingest computes a Spending Rate "
            "Alarm band WORSE than 'Baseline Band' (the band at resolve time, "
            "raised each time it re-fires), the engine lets the alarm fields "
            "write once and bumps the baseline so it goes quiet again at the "
            "new level. Operator-owned: delete the row (un-resolve) to resume "
            "normal alarm writes. The engine never writes here."
        ),
        "fields": [
            {"name": "Asana Task GID", "type": "singleLineText",
             "description": "Contract the operator resolved. UNIQUE -- upsert key."},
            {"name": "Contract Name", "type": "singleLineText",
             "description": "Snapshot of the task name at resolve time, for display."},
            {"name": "Baseline Band", "type": "singleLineText",
             "description": (
                 "Spending Rate Alarm band at resolve time (one of 75%/90%/"
                 "100%/Over, or blank for none) -- raised to the worst band "
                 "seen since. The engine re-arms only when a new ingest's band "
                 "exceeds this."
             )},
            {"name": "Resolved At", "type": "date"},
            {"name": "Notes", "type": "multilineText",
             "description": "Operator-editable."},
        ],
    },
    {
        "name": "Alarm Rearm",
        "description": (
            "Engine-owned per-contract high-water for the AUTOMATIC per-band "
            "alarm re-arm (distinct from operator-set Resolved Contracts). "
            "Stores the % Spent band at which a contract last fired ALARM. The "
            "binary Alarms field re-fires (a fresh email) only when the current "
            "band climbs ABOVE this; between bands it self-mutes to 'Previously "
            "Alarmed'; at 'Over' it stays ALARM. Cleared when the contract drops "
            "below 75% so a later climb fires fresh. See engine/alarm_rearm.py."
        ),
        "fields": [
            {"name": "Asana Task GID", "type": "singleLineText",
             "description": "Contract. UNIQUE -- upsert key."},
            {"name": "Alarmed Band", "type": "singleLineText",
             "description": "Highest % Spent band fired so far (75%/90%/100%/Over)."},
            {"name": "Updated At", "type": "date"},
        ],
    },
    {
        "name": "Attributed Lines",
        "description": (
            "One row per Tableau transaction the LAST ingest attributed to a "
            "specific Asana contract (by GID). Powers the Dashboard drill-down: "
            "click a contract name to see exactly which Tableau entries landed "
            "on it. Rewritten wholesale on every --ingest (a snapshot of the "
            "latest run, NOT a rolling audit log). Only attributed rows are "
            "stored — unmatched spend lives in Needs Tagging. 'In Term' marks "
            "whether the row counted toward Spent so far (opex term window); "
            "CapEx lines are always in-term and broadcast to every contract "
            "sharing the CapEx ID."
        ),
        "fields": [
            {"name": "Asana Task GID", "type": "singleLineText",
             "description": "Contract the row attributed to. Drill-down lookup key (not unique)."},
            {"name": "Date", "type": "date"},
            {"name": "Campus", "type": "singleLineText"},
            {"name": "Account No", "type": "singleLineText"},
            {"name": "Vendor", "type": "singleLineText"},
            {"name": "Record Description", "type": "multilineText"},
            {"name": "Reference", "type": "singleLineText"},
            {"name": "Amount", "type": "number", "options": {"precision": 2}},
            {"name": "In Term", "type": "checkbox",
             "description": "1 = counted toward Spent so far; 0 = attributed but excluded by the term window."},
            {"name": "Tier", "type": "singleLineText",
             "description": "'opex' or 'capex'."},
        ],
    },
    {
        "name": "Unlinked CapEx",
        "description": (
            "One row per CapEx project (Project ID) the LAST ingest found "
            "Tableau spend for but NO live Asana contract carries that CapEx ID "
            "(engine.capex spend_no_contract). Enriched with campuses + the "
            "distinct Record Descriptions so the /unlinked-capex surface can name "
            "the likely owner (the vendor is often blank but named in the "
            "description). Rewritten wholesale on every --ingest (a snapshot, "
            "not an audit log). Advisory only — the operator sets the CapEx ID "
            "on the matched contract in Asana; the engine never writes here."
        ),
        "fields": [
            {"name": "CapEx ID", "type": "singleLineText",
             "description": "Normalized Project ID with parked spend and no live contract."},
            {"name": "Spend", "type": "number", "options": {"precision": 2},
             "description": "Total Tableau spend for this project (cumulative, no term window)."},
            {"name": "Campuses", "type": "singleLineText",
             "description": "Comma-joined campuses the project's charges hit."},
            {"name": "Descriptions", "type": "multilineText",
             "description": "Distinct Record Descriptions (newline-joined) — the name-match haystack + operator display."},
            {"name": "Rows", "type": "number", "options": {"precision": 0}},
            {"name": "Last Updated", "type": "date"},
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
