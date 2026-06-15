"""Runtime Tableau→Asana campus crosswalk.

The engine's view of the spec §5 crosswalk: which Asana Campus option name(s)
should match a given Tableau campus code, plus the codes that should be
dropped entirely (e.g. INT), plus the Asana option names that act as
wildcards (e.g. "All Campuses").

Built at startup by merging:
1. config/campus_map.py defaults (the spec §5 special cases — CEN→{CEN,CEN/EDM},
   YVN→same, INT drop, ***NOR/***TUL reverse-overrides, All Campuses wildcard).
2. The Airtable Campus Map table (operator-editable overrides).

Per-code overrides REPLACE the default for that code; unspecified codes fall
back to the implicit identity mapping ("DAL" → {"DAL"}). Drop codes are taken
from Airtable when any are present; otherwise the config defaults apply.

The lookup is pure data — no Airtable I/O happens inside CampusCrosswalk.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping

from config import campus_map as _defaults


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CampusCrosswalk:
    """Runtime crosswalk between Tableau campus codes and Asana option names."""

    # Tableau code → frozenset of Asana option names it should match.
    # Codes NOT in this dict fall back to implicit identity ({tableau_code}).
    tableau_to_asana: Mapping[str, frozenset[str]]
    # Tableau codes to drop from attribution entirely.
    drop_codes: frozenset[str]
    # Asana option names that match any Tableau campus (wildcard).
    wildcard_options: frozenset[str]

    def is_drop_code(self, tableau_code: str) -> bool:
        return tableau_code in self.drop_codes

    def lookup(self, tableau_code: str) -> frozenset[str]:
        """Return the set of Asana option names this Tableau code matches.

        - Drop code → empty set (row should be dropped, not attributed).
        - Explicit entry → that set.
        - Unlisted code → implicit identity ({tableau_code}).
        """
        if tableau_code in self.drop_codes:
            return frozenset()
        if tableau_code in self.tableau_to_asana:
            return self.tableau_to_asana[tableau_code]
        return frozenset({tableau_code})

    def contract_matches_tableau_campus(
        self,
        contract_campus_options: frozenset[str],
        tableau_code: str,
    ) -> bool:
        """True if this contract's Campus options should match a transaction
        with this Tableau campus code.

        Match conditions:
        1. The contract has a wildcard option (e.g. "All Campuses") — matches
           any Tableau campus regardless of code.
        2. Otherwise, the Tableau code's crosswalk yields a set that
           intersects the contract's Campus options.
        Drop codes never match (a drop code's lookup is the empty set, and
        even wildcards do not rescue them — drop semantically means
        "don't attribute at all").
        """
        if tableau_code in self.drop_codes:
            return False
        if contract_campus_options & self.wildcard_options:
            return True
        return bool(contract_campus_options & self.lookup(tableau_code))


def build(
    forward_overrides: Mapping[str, frozenset[str]] | None = None,
    drop_override: frozenset[str] | None = None,
) -> CampusCrosswalk:
    """Construct a CampusCrosswalk by merging config defaults with optional
    Campus Map overrides loaded from SQLite.

    Per-Tableau-code overrides REPLACE the config default for that code,
    EXCEPT that reverse-encoded ***NOR/***TUL specials are always merged
    back in. Without that preservation, an operator who adds a Campus Map
    row for OMH would silently wipe the "***NOR (contract is for OMH)"
    reverse-encoding and break attribution for ***NOR contracts.
    """
    # Start from explicit Tableau->Asana defaults (CEN, YVN).
    forward: dict[str, frozenset[str]] = {
        tc: frozenset(opts) for tc, opts in _defaults.TABLEAU_TO_ASANA.items()
    }

    # Reverse-encode Asana-side overrides INTO A SEPARATE DICT so they can
    # always be unioned back in after overrides are applied. E.g.
    # "***NOR (contract is for OMH)" -> adds to the Asana-option set for OMH.
    reverse_encoded: dict[str, frozenset[str]] = {}
    for asana_option, tableau_codes in _defaults.ASANA_OVERRIDE_TO_TABLEAU.items():
        for tc in tableau_codes:
            reverse_encoded[tc] = reverse_encoded.get(tc, frozenset()) | {asana_option}

    # Apply reverse-encoded entries to forward via union.
    for tc, opts in reverse_encoded.items():
        forward[tc] = forward.get(tc, frozenset({tc})) | opts

    drop_codes = frozenset(_defaults.TABLEAU_DROP_CODES)
    wildcard = frozenset(_defaults.ASANA_WILDCARD_OPTIONS)

    if forward_overrides:
        # Per-code REPLACE -- operator-supplied options win for the explicit
        # set, but any reverse-encoded specials (***NOR, ***TUL) are still
        # unioned in so the operator does not accidentally break attribution
        # for those contracts by listing OMH/SBA without re-listing the
        # starred name.
        for tc, opts in forward_overrides.items():
            preserved = reverse_encoded.get(tc, frozenset())
            forward[tc] = frozenset(opts) | preserved

    if drop_override is not None:
        # Explicit operator drop set takes precedence in full. None means
        # "use config defaults"; an empty frozenset means "operator
        # deliberately turned off all drops".
        drop_codes = frozenset(drop_override)

    return CampusCrosswalk(
        tableau_to_asana=forward,
        drop_codes=drop_codes,
        wildcard_options=wildcard,
    )


__all__ = ["CampusCrosswalk", "build"]
