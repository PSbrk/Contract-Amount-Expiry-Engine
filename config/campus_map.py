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
# EXACT-MATCH (2026-06-24): Asana now mirrors Tableau campus codes, so matching
# is pure identity ("CEN" → {"CEN"}) handled implicitly by the matcher. The old
# forward guesses (CEN→{CEN,CEN/EDM}, YVN→{CEN,EDM}) are retired: YVN, ZNR, and
# any future campus with no Asana option fall to Needs-Tagging for the operator
# rather than being auto-attributed to a guessed campus. As the operator codes
# the campus in Asana, the identity match starts working with zero config change.
# ponytail: empty by design — re-add a code here only for a genuine non-identity alias.
TABLEAU_TO_ASANA: Final = {}

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
