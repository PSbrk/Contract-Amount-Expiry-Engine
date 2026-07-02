"""Non-secret configuration. Single source of truth for IDs, filters, thresholds.

Secrets (ASANA_PAT, optionally ONEDRIVE_BACKUP_PATH) come from environment
variables -- see .env.example. The bundle prefers config/secrets.env over a
default .env at CWD; engine.main._load_dotenv covers both.
"""

from __future__ import annotations

import logging
import os
from typing import Final


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Asana
# ---------------------------------------------------------------------------

ASANA_WORKSPACE_GID: Final = "85161544293495"
ASANA_PROJECT_GID: Final = "1213439050420971"  # Contractor Database

# Read-only custom fields. Vendor identity = task name itself (not a field).
ASANA_FIELD_CAMPUS: Final = "1213439050420974"           # multi_enum (match by option NAME)
ASANA_FIELD_CONTRACT_AMOUNT: Final = "1213458589272270"  # number — budget denominator
ASANA_FIELD_TARGET_START: Final = "1215032621216432"     # date — start fallback when set
ASANA_FIELD_CONTRACT_STATUS: Final = "1213557820319978"  # enum
ASANA_OPTION_STATUS_ACTIVE: Final = "1213557820319979"
ASANA_FIELD_EXPIRE_COUNTDOWN: Final = "1213492973877653"  # enum
ASANA_OPTION_EXPIRE_EXPIRED: Final = "1213492973877658"
ASANA_FIELD_PM_EMAIL: Final = "1213557820320006"         # text — used by the user's Asana
                                                          # automation rule for per-PM recipients
# text — operator-authored description of what the contract covers. Used as
# a tie-breaker when same-vendor multi-task ambiguity survives campus + date
# + earliest-start narrowing (e.g. one vendor's landscaping vs snow-removal
# contracts). Compared against Tableau's Record Description via token-set
# fuzzy match; the candidate with the clearly-best score wins.
ASANA_FIELD_CONTRACT_REASON_TEXT: Final = "1213527792866809"
ASANA_FIELD_CREATED_DATE: Final = "1213417707609925"     # date (fallback only; see spec §7)

# Coding fields mirrored from Tableau (added to Asana 2026-06). The deterministic
# join keys: Campus (above) + Dept + Acc narrow the opex candidate set; CapEx ID
# is the exact key for 63015 (== Tableau Project ID, after strip().upper()).
ASANA_FIELD_DEPT: Final = "1216012622947824"             # text  — GL department (e.g. "000")
ASANA_FIELD_ACC: Final = "1215966596882527"              # number — GL account (e.g. 63015)
ASANA_FIELD_CAPEX_ID: Final = "1215966596882529"         # text  — CapEx project id == Tableau Project ID

# Writable custom fields — ONLY these five may have their VALUES written.
# Structural changes to any field (rename / delete / option-edit) are forbidden
# regardless of guardrail mode.
ASANA_FIELD_SPENT_SO_FAR: Final = "1215629256175944"     # number
ASANA_FIELD_PCT_SPENT: Final = "1215629256175946"        # number (percentage, 2 decimals)
ASANA_FIELD_SPENDING_RATE: Final = "1215629256175948"    # number (pace ratio, 2 decimals)
ASANA_FIELD_SPENDING_RATE_ALARM: Final = "1215629256175950"  # enum — granular band detail
ASANA_FIELD_ALARMS: Final = "1215681548746113"           # enum — binary Clear/ALARM trigger

ASANA_SPENDING_RATE_ALARM_OPTIONS: Final = {
    "75%": "1215629256175951",
    "90%": "1215629256175952",
    "100%": "1215629256175953",
    "Over": "1215629256175954",
}

ASANA_ALARMS_OPTIONS: Final = {
    "Clear": "1215681548746114",
    "ALARM": "1215681548746115",
}

# Live gate (spec §7): a contract qualifies for writes only when
#   (section == ASANA_WRITE_GATE_SECTION  OR  Contract Status == Active)
#   AND  start <= today
# Pending Onboarding contracts are explicitly EXCLUDED — they get no writes
# until they advance to Active - Compliant or the Contract Status field flips
# to Active. The OR is critical: a contract may be promoted to "Active" before
# its section is moved.
# Past `due_on` or Expire countdown == EXPIRED! → freeze last values; that's
# enforced in the compute layer, not by this gate.
ASANA_WRITE_GATE_SECTION: Final = "Active - Compliant"

# Documented for audit-output clarity. These sections exist on the project
# but explicitly do NOT receive writes. Informational only — the gate above
# is what actually controls behavior.
ASANA_NON_WRITE_SECTIONS_INFO: Final = ("Pending Onboarding",)


# Expected-schema bundles — consumed by engine.audit to verify the project's
# field layout still matches what the engine is coded against. Updating these
# is the right place to ratify a deliberate schema change.

ASANA_EXPECTED_READ_FIELDS: Final = {
    "Campus": {"gid": ASANA_FIELD_CAMPUS, "type": "multi_enum"},
    "Contract Amount": {"gid": ASANA_FIELD_CONTRACT_AMOUNT, "type": "number"},
    "Target Start Date": {"gid": ASANA_FIELD_TARGET_START, "type": "date"},
    "Contract Status": {
        "gid": ASANA_FIELD_CONTRACT_STATUS,
        "type": "enum",
        "expected_options": {"Active": ASANA_OPTION_STATUS_ACTIVE},
    },
    "Expire countdown": {
        "gid": ASANA_FIELD_EXPIRE_COUNTDOWN,
        "type": "enum",
        "expected_options": {"EXPIRED!": ASANA_OPTION_EXPIRE_EXPIRED},
    },
    "PM Email": {"gid": ASANA_FIELD_PM_EMAIL, "type": "text"},
    "Contract Reason Text": {"gid": ASANA_FIELD_CONTRACT_REASON_TEXT, "type": "text"},
    "Created Date": {"gid": ASANA_FIELD_CREATED_DATE, "type": "date"},
    "Dept": {"gid": ASANA_FIELD_DEPT, "type": "text"},
    "Acc": {"gid": ASANA_FIELD_ACC, "type": "number"},
    "CapEx ID": {"gid": ASANA_FIELD_CAPEX_ID, "type": "text"},
}

ASANA_EXPECTED_WRITE_FIELDS: Final = {
    "Spent so far": {"gid": ASANA_FIELD_SPENT_SO_FAR, "type": "number"},
    "% Spent": {"gid": ASANA_FIELD_PCT_SPENT, "type": "number"},
    "Spending Rate": {"gid": ASANA_FIELD_SPENDING_RATE, "type": "number"},
    "Spending Rate Alarm": {
        "gid": ASANA_FIELD_SPENDING_RATE_ALARM,
        "type": "enum",
        "expected_options": dict(ASANA_SPENDING_RATE_ALARM_OPTIONS),
    },
    "Alarms": {
        "gid": ASANA_FIELD_ALARMS,
        "type": "enum",
        "expected_options": dict(ASANA_ALARMS_OPTIONS),
    },
}


# ---------------------------------------------------------------------------
# Tableau ingestion filters (enforced in code, not trusted from the export)
# ---------------------------------------------------------------------------

# Account numbers in scope. Strings to preserve any leading zeros and to make
# membership checks robust against pandas dtype inference.
#
# 63015 (Capital Projects) is back IN scope as of 2026-06-24 — it is the CapEx
# tier, matched by CapEx ID instead of fuzzy vendor (see CAPEX_ACCOUNT_NO).
# 63020 was REMOVED the same day (no longer tracked).
ACCOUNTS_IN_SCOPE: Final = frozenset({"63015", "63040", "63080", "63090"})

# Departments in scope. Strings — "000" must keep its zero-padding.
DEPTS_IN_SCOPE: Final = frozenset({"000", "107", "110"})

# The CapEx account. Rows coded to this account attribute by CapEx ID ↔ Tableau
# Project ID (not fuzzy vendor), aggregate per project, and compute against an
# operator-entered budget with NO term window. Everything else is the opex tier.
CAPEX_ACCOUNT_NO: Final = "63015"


# ---------------------------------------------------------------------------
# Per-contract computation
# ---------------------------------------------------------------------------

DEFAULT_TERM_MONTHS: Final = 12  # used when due_on missing AND target_start missing
PACE_GUARD_DAYS: Final = 30      # leave Spending Rate blank while elapsed < this many days


# ---------------------------------------------------------------------------
# Alarm bands & thresholds
# ---------------------------------------------------------------------------

# Budget-% bands → Spending Rate Alarm option name. Tuples of (lower_inclusive,
# option_name). Anything < 75 → blank. >100 → BUDGET_OVER_LABEL ("Over").
BUDGET_BANDS: Final = (
    (75.0, "75%"),
    (90.0, "90%"),
    (100.0, "100%"),
)
BUDGET_OVER_LABEL: Final = "Over"

# Runaway-pace alert: pace ratio (>1 = ahead of schedule) at or above this
# trips the Alarms field, independent of the budget band. Subject to
# PACE_GUARD_DAYS and MIN_SPEND_FLOOR (so brand-new / trivial contracts
# can't trip it).
RUNAWAY_PACE: Final = 2.0
MIN_SPEND_FLOOR: Final = 1000.0


# ---------------------------------------------------------------------------
# Step 6 change detection thresholds (spec §10)
# ---------------------------------------------------------------------------

# A swing in Spent so far at or above this dollar amount flags the contract
# into Run Log review_flags. Decreases always flag regardless of magnitude
# (spec §10: "especially a decrease or large swing") because in a net-signed
# sum, a fall in spent indicates a correction the operator should eyeball.
# 10k is a reasonable starting point on the engine's scale (contracts run
# $5k to $1M+ per term); adjust if it becomes noisy.
REVIEW_LARGE_DELTA_DOLLARS: Final = 10000.0


# ---------------------------------------------------------------------------
# Step 8 Run Log retention
# ---------------------------------------------------------------------------

# Run Log rows older than this many days are pruned at the end of each
# --ingest run. Set to 0 to disable the prune (operator keeps Run Log
# forever; manual cleanup in the Airtable UI is the fallback).
#
# 365 keeps a full year of run history visible by default — enough to
# correlate seasonal anomalies — without unbounded growth. One row per
# day for the daily cron + a handful of manual runs averages ~400 rows
# at any time, well under Airtable's per-base limits.
RUN_LOG_RETENTION_DAYS: Final = int(os.environ.get("RUN_LOG_RETENTION_DAYS", "365").strip() or "365")


# Ingest sanity gate: a wrong / partial / differently-scoped Tableau export
# dropped in the inbox would otherwise be attributed and written to Asana
# unattended, silently cratering spend (2026-07-01: a 14,815-row export
# replaced the 17,231-row one and dropped ~$566k of attribution to Asana). If
# an ingest's in-scope row count OR in-scope dollar total drops by more than
# this fraction vs the last OK ingest, OR its out-of-scope ratio exceeds
# INGEST_SANITY_OOS_PCT, the file is HELD (quarantined to data/held/, not
# written to Asana) for operator confirmation. ponytail: two knobs; widen only
# if legitimate exports keep tripping it.
INGEST_SANITY_DROP_PCT: Final = float(os.environ.get("INGEST_SANITY_DROP_PCT", "0.05").strip() or "0.05")
INGEST_SANITY_OOS_PCT: Final = float(os.environ.get("INGEST_SANITY_OOS_PCT", "0.05").strip() or "0.05")


# ---------------------------------------------------------------------------
# Run-mode overrides from env (so dry-run can be toggled in CI/locally without
# code change)
# ---------------------------------------------------------------------------

_BOOL_TRUE_ALIASES: Final = frozenset({"1", "true", "yes", "on"})
_BOOL_FALSE_ALIASES: Final = frozenset({"0", "false", "no", "off", ""})


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean env var with a SAFE-by-default policy: an unrecognized
    value falls back to `default` (with a warning), rather than collapsing
    to False.

    Critical for DRY_RUN_ASANA. The prior implementation returned False for
    any value not in the truthy list — meaning a typo like
    `DRY_RUN_ASANA=tru` (or an accidentally-blanked value mid-edit) would
    silently flip the engine into live writes against Asana. With this
    semantic, only an EXPLICIT false-alias turns dry-run off; anything
    else stays at the safe default.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    cleaned = raw.strip().lower()
    if cleaned in _BOOL_TRUE_ALIASES:
        return True
    if cleaned in _BOOL_FALSE_ALIASES:
        return False
    log.warning(
        "%s=%r is not a recognized boolean (true: 1/true/yes/on; "
        "false: 0/false/no/off/<empty>). Falling back to default=%s.",
        name, raw, default,
    )
    return default


DRY_RUN_ASANA: Final = _env_bool("DRY_RUN_ASANA", True)  # safe default during build
WRITE_TEST_CONTRACT: Final = os.environ.get("WRITE_TEST_CONTRACT", "").strip() or None


# ---------------------------------------------------------------------------
# Step 7: transaction source selector
# ---------------------------------------------------------------------------

# Which TransactionSource the engine pulls from on --ingest.
#
# `local_inbox` is the default and the only production-ready path: the
# engine scans data/inbox/ for files dropped there by the operator.
# `tableau_rest` is a stub for the eventual Tableau REST pull (see
# engine.ingest.TableauRestSource); selecting it today raises
# NotImplementedError on first call.
_VALID_SOURCES: Final = frozenset({"local_inbox", "tableau_rest"})

_raw_source = os.environ.get("TRANSACTION_SOURCE", "").strip().lower()
if _raw_source and _raw_source not in _VALID_SOURCES:
    log.warning(
        "TRANSACTION_SOURCE=%r is not recognized (valid: %s). "
        "Falling back to default='local_inbox'.",
        _raw_source, sorted(_VALID_SOURCES),
    )
    _raw_source = ""
TRANSACTION_SOURCE: Final = _raw_source or "local_inbox"

# Tableau Cloud REST endpoint parameters — consumed by TableauRestSource once
# it lands. Defaults come from operator notes: site `lifechurch` on
# us-west-2b. View ID + PAT name/secret have no safe default and must be
# supplied via env when the switch is flipped to `tableau_rest`.
TABLEAU_SERVER_URL: Final = (
    os.environ.get("TABLEAU_SERVER_URL", "").strip()
    or "https://us-west-2b.online.tableau.com"
)
TABLEAU_SITE_NAME: Final = (
    os.environ.get("TABLEAU_SITE_NAME", "").strip() or "lifechurch"
)
TABLEAU_API_VERSION: Final = (
    os.environ.get("TABLEAU_API_VERSION", "").strip() or "3.22"
)
TABLEAU_VIEW_ID: Final = os.environ.get("TABLEAU_VIEW_ID", "").strip() or None
TABLEAU_PAT_NAME: Final = os.environ.get("TABLEAU_PAT_NAME", "").strip() or None
TABLEAU_PAT_SECRET: Final = os.environ.get("TABLEAU_PAT_SECRET", "").strip() or None


# ---------------------------------------------------------------------------
# Phase 4 — OneDrive backup
# ---------------------------------------------------------------------------

# Destination path the engine copies data/engine.db to after every successful
# --ingest run. Intended for a OneDrive-synced folder so the cloud sync client
# handles the upload — no Microsoft Graph auth needed. Pointing at a plain
# local path also works (it just becomes a second on-disk copy).
#
# Unset → backup is skipped. Backup failures NEVER fail the ingest run; the
# local data/engine.db remains the source of truth.
ONEDRIVE_BACKUP_PATH: Final = os.environ.get("ONEDRIVE_BACKUP_PATH", "").strip() or None
