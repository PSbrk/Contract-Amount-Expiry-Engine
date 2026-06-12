"""Non-secret configuration. Single source of truth for IDs, filters, thresholds.

Secrets (Asana PAT, Google service-account JSON, n8n webhook URL) come from
environment variables — see .env.example and SETUP.md.
"""

from __future__ import annotations

import os
from typing import Final


# ---------------------------------------------------------------------------
# Asana
# ---------------------------------------------------------------------------

ASANA_WORKSPACE_GID: Final = "85161544293495"
ASANA_PROJECT_GID: Final = "1213439050420971"  # Contractor Database

# Read fields (vendor name = task name itself).
ASANA_FIELD_CAMPUS: Final = "1213439050420974"          # multi-select (match by option NAME)
ASANA_FIELD_CONTRACT_AMOUNT: Final = "1213458589272270"  # number — budget denominator
ASANA_FIELD_TARGET_START: Final = "1215032621216432"     # date — start fallback when set
ASANA_FIELD_CONTRACT_STATUS: Final = "1213557820319978"  # enum
ASANA_OPTION_STATUS_ACTIVE: Final = "1213557820319979"
ASANA_FIELD_EXPIRE_COUNTDOWN: Final = "1213492973877653"  # enum
ASANA_OPTION_EXPIRE_EXPIRED: Final = "1213492973877658"
ASANA_FIELD_PM_EMAIL: Final = "1213557820320006"         # text
ASANA_FIELD_CREATED_DATE: Final = "1213417707609925"     # date (fallback only)

# Write fields — ONLY these four may have their VALUES written.
ASANA_FIELD_SPENT_SO_FAR: Final = "1215629256175944"     # number
ASANA_FIELD_PCT_SPENT: Final = "1215629256175946"        # number (percentage, 2 decimals)
ASANA_FIELD_SPENDING_RATE: Final = "1215629256175948"    # number (ratio, 2 decimals)
ASANA_FIELD_SPENDING_RATE_ALARM: Final = "1215629256175950"  # single-select

ASANA_ALARM_OPTIONS: Final = {
    "75%": "1215629256175951",
    "90%": "1215629256175952",
    "100%": "1215629256175953",
    "Over": "1215629256175954",
}

# Section names that mark a contract as live (in addition to status==Active).
ASANA_LIVE_SECTIONS: Final = ("Pending Onboarding", "Active - Compliant")
# Of those, contracts must be in this section OR status==Active to qualify for writes.
# (Live gate is documented in Section 8 of the spec — section_name in this set OR
# Contract Status == Active, AND start <= today.)
ASANA_WRITE_GATE_SECTIONS: Final = ("Active - Compliant",)


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------

GOOGLE_DASHBOARD_SHEET_ID: Final = "1FHWwbqrOrXvwj2Elec47vT6gv7-4tOIOAvFpTGX5HCo"
GOOGLE_DRIVE_INBOX_FOLDER_ID: Final = "1CLwDCuwCyTi8P45SvxZpEi7M4r-77ksi"
GOOGLE_CAPITAL_BREAKDOWN_SHEET_ID: Final = "1HTX7NVQYso56CL25g4TE1yxY7Nl5luSkMosyhfK7iRo"

# Sheet tabs the engine reads/writes. Tab names are stable contracts — changing
# them is a breaking change.
SHEET_TAB_DASHBOARD: Final = "Dashboard"
SHEET_TAB_NEEDS_TAGGING: Final = "Needs Tagging"
SHEET_TAB_VENDOR_ALIASES: Final = "Vendor Aliases"
SHEET_TAB_CAMPUS_MAP: Final = "Campus Map"
SHEET_TAB_LEARNED_MAPPINGS: Final = "Learned Mappings"
SHEET_TAB_STATE: Final = "State"
SHEET_TAB_RUN_LOG: Final = "Run Log"
SHEET_TAB_REVIEW: Final = "Review"


# ---------------------------------------------------------------------------
# Tableau ingestion filters (enforced in code, not trusted from the export)
# ---------------------------------------------------------------------------

# Account numbers in scope. Strings to preserve any leading zeros.
ACCOUNTS_IN_SCOPE: Final = frozenset({"63015", "63020", "63040", "63080", "63090"})

# Departments in scope. Strings — "000" and "107" must keep their zero-padding.
DEPTS_IN_SCOPE: Final = frozenset({"000", "107"})


# ---------------------------------------------------------------------------
# Per-contract computation
# ---------------------------------------------------------------------------

DEFAULT_TERM_MONTHS: Final = 12  # used when due_on missing AND target_start missing
PACE_GUARD_DAYS: Final = 30  # leave Spending Rate blank while elapsed < this many days


# ---------------------------------------------------------------------------
# Alarm bands & thresholds
# ---------------------------------------------------------------------------

# Budget-% bands. Tuples of (lower_inclusive, label).
# Anything < 75 is "clear" (blank).
BUDGET_BANDS: Final = (
    (75.0, "75%"),
    (90.0, "90%"),
    (100.0, "100%"),
)
# Anything strictly above 100 maps to "Over".
BUDGET_OVER_LABEL: Final = "Over"

# Runaway-pace alert: pace ratio (>1 = ahead of schedule) above this triggers an
# email, independent of the budget band. Subject to PACE_GUARD_DAYS and
# MIN_SPEND_FLOOR.
RUNAWAY_PACE: Final = 2.0
MIN_SPEND_FLOOR: Final = 1000.0  # $ floor before runaway-pace can fire


# ---------------------------------------------------------------------------
# Email recipients
# ---------------------------------------------------------------------------

# Base recipient list — every alert goes to at least these.
ALERT_RECIPIENTS: Final = ("philip.seabrook@life.church",)

# When True, the contract's own Asana `PM Email` is appended to the recipient
# list for that contract's alerts. Default off — flip when ready to expand
# notifications per-project.
INCLUDE_PM_EMAIL: Final = False

# Sender is the Gmail OAuth account configured on the n8n Gmail node
# (philip.seabrook@life.church). The engine doesn't choose the sender — n8n does.


# ---------------------------------------------------------------------------
# Run-mode overrides from env (so a dry-run is set in CI/locally without code change)
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


DRY_RUN_ASANA: Final = _env_bool("DRY_RUN_ASANA", True)  # safe default during build
WRITE_TEST_CONTRACT: Final = os.environ.get("WRITE_TEST_CONTRACT", "").strip() or None
