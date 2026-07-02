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

# Asana option NAME → Tableau code that should hit it (for non-identity aliases).
# The inverse direction: an Asana option tied to a non-matching Tableau code.
# EMPTY by design (2026-07-02): the old ***NOR→OMH / ***TUL→SBA entries were
# EXCEPTIONS wrongly encoded as always-on GLOBAL rules — they cross-attributed
# every OMH/SBA transaction for those vendors regardless of the operator's
# intent. Cross-campus is now NEVER automatic: it happens only via a per-case,
# operator-confirmed Learned Mapping carrying the "Cross-Campus Exception" flag.
# ***NOR/***TUL-tagged contracts whose spend used to ride this override now fall
# to Needs Tagging, where the operator confirms each once (see engine.attribution
# flagged-exception path). ponytail: re-add here ONLY for a genuine always-true
# non-identity alias — a real one-off belongs in a flagged LM, not this map.
ASANA_OVERRIDE_TO_TABLEAU: Final = {}

# Asana options that exist but have no Tableau equivalent (yet). Left here for
# documentation; the matcher just won't find transactions for these.
ASANA_NO_TABLEAU_EQUIVALENT: Final = frozenset({"DEN", "KC"})
