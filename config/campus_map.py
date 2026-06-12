"""Campus crosswalk: Tableau campus codes ↔ Asana `Campus` option names.

Editable defaults live here. The Airtable "Campus Map" table is the runtime
source of truth — values from there override these defaults at run-time. This
file just bootstraps the table on first run.
"""

from __future__ import annotations

from typing import Final


# Tableau campus codes that should be dropped from ingestion entirely.
TABLEAU_DROP_CODES: Final = frozenset({"INT"})

# Asana option NAMES that act as wildcards — match every Tableau campus.
ASANA_WILDCARD_OPTIONS: Final = frozenset({"All Campuses"})

# Tableau code → set of Asana option names it should match.
# Identity mapping for codes shared by both systems is implicit (handled in the
# matcher) — you only list overrides here.
TABLEAU_TO_ASANA: Final = {
    # CEN matches both CEN and CEN/EDM (presently equivalent; will diverge later).
    "CEN": frozenset({"CEN", "CEN/EDM"}),
    # YVN currently treated as CEN/CEN-EDM. Configurable — flip when it gets its own contracts.
    "YVN": frozenset({"CEN", "CEN/EDM"}),
}

# Asana option NAME → Tableau code that should hit it (for the starred overrides).
# These are the inverse direction: a Tableau code can match many Asana options;
# and an Asana option can be tied to a non-matching Tableau code via these overrides.
ASANA_OVERRIDE_TO_TABLEAU: Final = {
    "***NOR (contract is for OMH)": frozenset({"OMH"}),
    "***TUL (contract is for SBA)": frozenset({"SBA"}),
}

# Asana options that exist but have no Tableau equivalent (yet). Left here for
# documentation; the matcher just won't find transactions for these.
ASANA_NO_TABLEAU_EQUIVALENT: Final = frozenset({"DEN", "KC"})
