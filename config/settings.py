"""Non-secret configuration. Single source of truth for IDs, filters, thresholds.

Secrets (ASANA_PAT, AIRTABLE_PAT, AIRTABLE_BASE_ID) come from environment
variables — see .env.example and SETUP.md. CI reads them from GitHub Actions
secrets directly; local runs load a .env file via python-dotenv.
"""

from __future__ import annotations

import os
from typing import Final


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
ASANA_FIELD_CREATED_DATE: Final = "1213417707609925"     # date (fallback only; see spec §7)

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
    "Created Date": {"gid": ASANA_FIELD_CREATED_DATE, "type": "date"},
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
# Airtable
# ---------------------------------------------------------------------------

# Table names — the engine creates any that are missing, so these names are the
# stable contract. Field-level schema lives in engine.airtable (Step 2).
AIRTABLE_TABLE_INBOX: Final = "Inbox"
AIRTABLE_TABLE_DASHBOARD: Final = "Dashboard"
AIRTABLE_TABLE_NEEDS_TAGGING: Final = "Needs Tagging"
AIRTABLE_TABLE_VENDOR_ALIASES: Final = "Vendor Aliases"
AIRTABLE_TABLE_CAMPUS_MAP: Final = "Campus Map"
AIRTABLE_TABLE_LEARNED_MAPPINGS: Final = "Learned Mappings"
AIRTABLE_TABLE_STATE: Final = "State"
AIRTABLE_TABLE_RUN_LOG: Final = "Run Log"

AIRTABLE_TABLES: Final = (
    AIRTABLE_TABLE_INBOX,
    AIRTABLE_TABLE_DASHBOARD,
    AIRTABLE_TABLE_NEEDS_TAGGING,
    AIRTABLE_TABLE_VENDOR_ALIASES,
    AIRTABLE_TABLE_CAMPUS_MAP,
    AIRTABLE_TABLE_LEARNED_MAPPINGS,
    AIRTABLE_TABLE_STATE,
    AIRTABLE_TABLE_RUN_LOG,
)


# ---------------------------------------------------------------------------
# Tableau ingestion filters (enforced in code, not trusted from the export)
# ---------------------------------------------------------------------------

# Account numbers in scope. Strings to preserve any leading zeros and to make
# membership checks robust against pandas dtype inference.
ACCOUNTS_IN_SCOPE: Final = frozenset({"63015", "63020", "63040", "63080", "63090"})

# Departments in scope. Strings — "000" must keep its zero-padding.
DEPTS_IN_SCOPE: Final = frozenset({"000", "107"})


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
# Run-mode overrides from env (so dry-run can be toggled in CI/locally without
# code change)
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


DRY_RUN_ASANA: Final = _env_bool("DRY_RUN_ASANA", True)  # safe default during build
WRITE_TEST_CONTRACT: Final = os.environ.get("WRITE_TEST_CONTRACT", "").strip() or None
