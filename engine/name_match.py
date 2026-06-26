"""Match a live contract NAME against free-text Tableau descriptions.

Used to spot blank-vendor / parked spend whose owner is identifiable only by the
Record Description (the Vendor field is blank — e.g. a CapEx deposit "carpet
replacement, EMPIRE TODAY HS …", or a project charge with the vendor buried in
free text). A contract "appears in" a description when ALL of its DISTINCTIVE
name tokens are present in ONE description.

Two deliberate guards, both learned from a prototype against real data:
  - Match per INDIVIDUAL description, never a pool of a project's rows — pooling
    lets "team" from one row and "landscape" from another coincidentally satisfy
    "A-Team Landscape".
  - DISTINCTIVE tokens only — drop generic industry words. Otherwise a name like
    "P&E Building Services" reduces to {building} and matches any description
    mentioning a building.

Pure logic — no I/O. Reused by the Unlinked-CapEx surface and the P-card opex
blank-vendor hints.
"""

from __future__ import annotations

import re


# Generic words that don't distinguish one vendor from another. A name whose
# tokens are ALL generic yields no distinctive tokens and never matches.
_GENERIC: frozenset[str] = frozenset({
    "llc", "inc", "incorporated", "co", "company", "corp", "corporation",
    "the", "and", "for", "of", "ltd", "group", "groups",
    "services", "service", "building", "buildings", "construction",
    "electric", "electrical", "plumbing", "mechanical", "landscape",
    "landscaping", "maintenance", "solutions", "contractors", "contracting",
    "systems", "supply", "supplies", "commercial", "industries", "enterprises",
    "management", "properties", "property", "cleaning", "janitorial",
})

_WORD = re.compile(r"[^a-z0-9 ]")


def _tokenize(text: str) -> list[str]:
    return _WORD.sub(" ", (text or "").lower()).split()


def distinctive_tokens(name: str) -> set[str]:
    """The meaningful, distinguishing tokens of a contract name: length >= 3 and
    not a generic industry word. Empty when a name is only generic words."""
    return {t for t in _tokenize(name) if len(t) >= 3 and t not in _GENERIC}


def name_in_description(name_tokens: set[str], description: str) -> bool:
    """True when every distinctive name token appears in this ONE description.

    Requires real signal: >= 2 distinctive tokens, OR a single distinctive token
    of length >= 5 (so a lone short token like 'abc' can't match broadly)."""
    if not name_tokens:
        return False
    if not name_tokens.issubset(set(_tokenize(description))):
        return False
    if len(name_tokens) >= 2:
        return True
    (only,) = tuple(name_tokens)
    return len(only) >= 5


def match_unlinked(
    descriptions: list[str],
    campuses: set[str],
    contracts: list[tuple[str, str, set[str]]],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Find live contracts named in any of `descriptions`.

    contracts: iterable of (name, gid, campus_options). Returns
    (confident, cross_campus) — each a list of (name, gid). A name match whose
    campus overlaps the spend's campus is CONFIDENT; a name match on a different
    campus is CROSS_CAMPUS (still useful — a contract coded to a blank/other
    campus). Multi-vendor projects nominate EVERY matched contract. De-duped by
    gid; a contract lands in exactly one bucket per call.
    """
    confident: list[tuple[str, str]] = []
    cross: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, gid, campus_options in contracts:
        if gid in seen:
            continue
        nt = distinctive_tokens(name)
        if not nt:
            continue
        if any(name_in_description(nt, d) for d in descriptions):
            seen.add(gid)
            if campuses & campus_options:
                confident.append((name, gid))
            else:
                cross.append((name, gid))
    return confident, cross


__all__ = ["distinctive_tokens", "name_in_description", "match_unlinked"]
