"""Per-row transaction attribution.

The spec §6 algorithm, condensed:
1. Drop transactions whose Campus is a configured drop-code (e.g. INT).
2. Consult Learned Mappings — operator's prior answer wins outright.
3. Vendor fuzzy match (rapidfuzz WRatio + Vendor Aliases) → candidate contracts.
4. Campus narrow via the crosswalk (All Campuses wildcard, identity, ***NOR /
   ***TUL overrides).
5. Date narrow: keep candidates whose [start, due_on] contains the row's Date.
6. Crossover tiebreaker: prefer the contract with the EARLIEST start date.
   Rationale: at renewal time PMs create the new contract task while the old
   one is still active. We want to spend the OLD contract's budget down first;
   the NEW contract only catches transactions once the OLD is past_due (which
   the date narrow at step 5 enforces naturally).
7. Exactly-one candidate after narrowing → auto-attribute to that contract GID.
   Multiple candidates surviving the tiebreaker → "ambiguous" → Vendor Conflicts /
   Needs Tagging review (operator picks).
   Zero candidates after vendor match → "unmatched" → Needs Tagging.
   Zero candidates after campus narrow → "unmatched" with vendor-only hints.

This module is per-row, not per-group. That matters for the crossover case: a
group of transactions for vendor X across August–November can attribute
August rows to the Old contract (due Sept 30) and October–November rows to the
New contract (started Sept 1), automatically. The group is fully attributed
but to TWO contracts — captured as status="split" on the group result, with
both contracts' splits in the splits tuple.

Group-level results are still produced for the Needs Tagging UI: a group is
"ambiguous" if ANY of its rows couldn't be narrowed to a single contract.

This module is pure logic — no Asana, no Airtable. The caller fetches contracts /
aliases / crosswalk / learned mappings and passes them in.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Iterable

import pandas as pd
from dateutil.relativedelta import relativedelta
from rapidfuzz import fuzz, utils as fuzz_utils

from config import settings
from engine.asana_contracts import Contract
from engine.campus_map import CampusCrosswalk


log = logging.getLogger(__name__)


# Vendor names are operator-curated in Asana, so the fuzz threshold is for
# format variations (case, punctuation, "Inc." vs ", Inc.") — NOT for finding
# semantically-similar vendors. A tight threshold reduces false positives.
#
# 92 is chosen empirically over 90 to filter out WRatio's partial-ratio
# cliff: "Acme" vs "Acme Sports Inc" scores exactly 90 via partial-ratio
# (one is a prefix of the other), which would falsely auto-attribute any
# vendor starting with the contract name. 92 still passes the canonical
# format variations ("Acme, Inc." vs "Acme Inc" scores 94 after
# default_process normalization).
DEFAULT_FUZZY_THRESHOLD: int = 92


@dataclass(frozen=True)
class AttributionResult:
    """Per-group attribution outcome.

    rows + amount are aggregated over every transaction in the group.

    contract_gid + contract_name populated when the WHOLE group attributes to a
    single contract (status="auto" / "learned").

    splits is populated for status="split" — the crossover case where the
    group's transactions divide across multiple contracts by date. Tuple of
    (gid, name, rows, amount) per contract that took some of the spend.

    candidate_names / candidate_gids carry the surviving multi-candidate list
    for status="ambiguous" (operator must choose) and the vendor-only hints
    for status="unmatched" (no campus-narrowing contract for this vendor).

    distinct_descriptions is populated for ambiguous groups so the Vendor
    Conflicts UI can render a per-description dropdown picker. Tuple of
    (description, rows, amount, min_date, max_date) per distinct Record
    Description in the group, sorted by descending amount so the
    highest-impact descriptions are shown first. min_date / max_date are
    ISO YYYY-MM-DD strings ("" if no parsable dates in the bucket) and
    let the UI filter date-incompatible candidates out of the auto-suggest
    -- e.g. a "Snow/ice 3/2025" bucket should not auto-pick a contract
    whose term starts 2025-09-30, even if the reason text matches.
    """
    group_key: str           # "Campus|Dept|Account No|Vendor"
    campus: str
    dept: str
    account_no: str
    vendor: str
    status: str              # "auto" | "learned" | "split" | "ambiguous" | "unmatched" | "dropped"
    contract_name: str | None
    contract_gid: str | None
    candidate_names: tuple[str, ...]
    candidate_gids: tuple[str, ...]
    rows: int
    amount: float
    sample_description: str
    # ISO date strings (YYYY-MM-DD) bounding the transaction set in this group.
    first_date: str = ""
    last_date: str = ""
    # For status="split": per-contract breakdown of how spend was divided.
    # Empty tuple otherwise. Each entry is (gid, name, rows, amount).
    splits: tuple[tuple[str, str, int, float], ...] = ()
    # For status="ambiguous": per-distinct-description breakdown of how the
    # group's rows are distributed by Record Description. Each entry is
    # (description, rows, amount, min_date_iso, max_date_iso), sorted by
    # descending amount. min/max date are "" when no row in that bucket
    # has a parsable date (degraded gracefully — UI falls back to no
    # date filtering for that description).
    distinct_descriptions: tuple[tuple[str, int, float, str, str], ...] = ()
    # Phase 14a: True iff every (candidate, row_date) pair fails the date
    # check -- i.e., the group's ambiguity is purely "no contract covers
    # these dates". Routes the row to Vendor Conflicts even when there's
    # only one candidate, so the operator can resolve via the new
    # "Unassigned - Pre-dates Asana Record" picker.
    all_out_of_term: bool = False

    @property
    def needs_tagging(self) -> bool:
        return self.status in ("ambiguous", "unmatched")


@dataclass(frozen=True)
class AttributionRun:
    results: tuple[AttributionResult, ...]
    # Per-transaction-row contract gid, POSITIONAL — one entry per in-scope
    # df row, in the SAME order as the df passed to attribute(). None for any
    # row whose GROUP did not attribute cleanly (ambiguous / unmatched /
    # dropped) AND for individually-unattributed rows inside a split group.
    #
    # Positional (not keyed by Record No) for two correctness reasons:
    #   1. Record No is NOT unique in the Tableau export (multi-line invoices,
    #      charge+reversal pairs), so a dict keyed by it silently dropped
    #      rows (last-write-wins) and misattributed split spend.
    #   2. A row's gid is only recorded when its group's FINAL status is
    #      cleanly attributed, so an "ambiguous" group's already-attributed
    #      rows no longer leak spend onto the Dashboard before the operator
    #      resolves the conflict.
    # compute.annotate_with_contract assigns this straight onto the df by
    # position (the same kept_df object), so alignment is exact.
    row_gids: tuple[str | None, ...] = ()

    def by_status(self, status: str) -> list[AttributionResult]:
        return [r for r in self.results if r.status == status]

    @property
    def auto(self) -> list[AttributionResult]: return self.by_status("auto")
    @property
    def learned(self) -> list[AttributionResult]: return self.by_status("learned")
    @property
    def split(self) -> list[AttributionResult]: return self.by_status("split")
    @property
    def ambiguous(self) -> list[AttributionResult]: return self.by_status("ambiguous")
    @property
    def unmatched(self) -> list[AttributionResult]: return self.by_status("unmatched")
    @property
    def dropped(self) -> list[AttributionResult]: return self.by_status("dropped")

    @property
    def needs_tagging_groups(self) -> list[AttributionResult]:
        return [r for r in self.results if r.needs_tagging]

    def summary_dict(self) -> dict[str, int]:
        return {
            "total_groups": len(self.results),
            "auto": len(self.auto),
            "learned": len(self.learned),
            "split": len(self.split),
            "ambiguous": len(self.ambiguous),
            "unmatched": len(self.unmatched),
            "dropped": len(self.dropped),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_str(v: object) -> str:
    """Coerce a pandas cell to a clean str; pd.NA and NaN become ''."""
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return ""
    return str(v)


def _ts_to_iso_date(ts) -> str:
    """pd.Timestamp -> 'YYYY-MM-DD'. Empty string on missing dates."""
    if ts is None or pd.isna(ts):
        return ""
    if hasattr(ts, "strftime"):
        return ts.strftime("%Y-%m-%d")
    return str(ts)[:10]


def _first_non_empty(s: pd.Series) -> str:
    for x in s:
        if isinstance(x, str) and x.strip():
            return x
    return ""


def _contract_start_for_narrowing(c: Contract) -> date | None:
    """Same start-date rule as engine/compute.py:compute_start — Target Start
    Date if set, else due_on - 12 months. Kept in lock-step here so the date
    narrow in attribution and the term-window filter in compute agree on what
    "the contract's start date" means."""
    if c.target_start is not None:
        return c.target_start
    if c.due_on is not None:
        return c.due_on - relativedelta(months=12)
    return None


def _date_contains(c: Contract, row_date: date | None) -> bool:
    """True iff row_date falls within [start, due_on] for contract c.

    A contract with no due_on is treated as "open-ended forward" — passes any
    row_date >= its start. A contract with no start derivable also passes
    (can't narrow on something we don't have).
    """
    if row_date is None:
        return True
    start = _contract_start_for_narrowing(c)
    if start is not None and row_date < start:
        return False
    if c.due_on is not None and row_date > c.due_on:
        return False
    return True


def _row_date_to_pydate(v) -> date | None:
    """Coerce a pandas / numpy cell into a Python date for window comparison.

    Iterating via DataFrame.values on a datetime64 column yields
    numpy.datetime64, not pd.Timestamp — both need a conversion path.
    """
    if v is None:
        return None
    # pd.NaT and numpy NaT both compare unequal to themselves.
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, date):
        return v
    if hasattr(v, "to_pydatetime"):
        try:
            return v.to_pydatetime().date()
        except Exception:
            return None
    # numpy.datetime64 path — round-trip through pd.Timestamp.
    try:
        return pd.Timestamp(v).to_pydatetime().date()
    except (ValueError, TypeError):
        pass
    if isinstance(v, str) and v:
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


# Stop-words and tokenizer shared by the description-vs-reason narrowing
# (#12 meaningful-token guard) and the Learned-Mapping pattern matcher
# (#7 volatile-token normalization). Kept here in the engine core so the
# attribution decision and the UI's pre-select suggestion reason about the
# same notion of "meaningful word". The UI's _tokens() in routes.py mirrors
# this list.
_NARROW_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "and", "any", "are", "as", "at", "be", "but", "by", "for",
    "from", "has", "have", "he", "her", "his", "i", "in", "is", "it", "its",
    "of", "on", "or", "she", "that", "the", "their", "them", "they", "this",
    "to", "was", "we", "were", "will", "with", "you", "your",
    "additional", "also", "amount", "bill", "contract", "costs", "cover",
    "covers", "include", "includes", "needed", "new", "operator", "reversed",
    "service", "services", "submit", "task", "txn", "txns",
})
_NARROW_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")


def _meaningful_tokens(text: str | None) -> set[str]:
    """Lowercase alpha tokens of length ≥ 3, minus stop-words and any token
    containing a digit (invoice numbers, dates — the volatile bits that don't
    recur verbatim). The signal-bearing words of a description or reason."""
    if not text:
        return set()
    out: set[str] = set()
    for m in _NARROW_TOKEN_RE.finditer(text):
        t = m.group(0).lower()
        if len(t) < 3 or t in _NARROW_STOPWORDS:
            continue
        if any(ch.isdigit() for ch in t):
            continue
        out.add(t)
    return out


def normalize_lm_pattern(description: str | None) -> str:
    """Reduce a Tableau Record Description to a stable, recurring stem for
    storage as a Learned-Mapping Description Pattern (#7).

    Strips volatile tokens (invoice numbers, dates, anything containing a
    digit) and stop-words, keeping the meaningful words in their original
    order. 'Snow plowing INV-4471 2/2025' -> 'snow plowing'. The matcher
    (_lm_pattern_matches) normalizes BOTH the stored pattern and each future
    description the same way and checks token-subset, so the pattern keeps
    matching even though the invoice number changes every month.

    Falls back to the trimmed original when normalization would empty it
    (e.g. a description that is only digits/stop-words) so the operator's
    pick still records *something* rather than a universal-match blank.
    """
    desc = (description or "").strip()
    if not desc:
        return ""
    kept = [
        m.group(0).lower()
        for m in _NARROW_TOKEN_RE.finditer(desc)
        if len(m.group(0)) >= 3
        and m.group(0).lower() not in _NARROW_STOPWORDS
        and not any(ch.isdigit() for ch in m.group(0))
    ]
    return " ".join(kept) if kept else desc.lower()


def _lm_pattern_matches(pattern: str | None, description: str) -> bool:
    """True iff the (possibly legacy-literal) Description Pattern applies to
    `description`. Both sides are reduced to meaningful tokens via
    _meaningful_tokens and the pattern's tokens must ALL appear in the
    description's tokens (token-subset). This tolerates volatile invoice
    numbers/dates in either string — old literal patterns and new normalized
    stems both match correctly. A pattern that normalizes to no tokens never
    matches (avoids a degenerate universal match)."""
    pat_tokens = _meaningful_tokens(pattern)
    if not pat_tokens:
        return False
    return pat_tokens <= _meaningful_tokens(description)


def _match_vendor(
    vendor: str,
    searchable: list[tuple[str, Contract]],
    fuzzy_threshold: int,
) -> list[Contract]:
    """Return unique contracts whose name or any alias scores ≥ threshold
    against the Tableau Vendor string. Dedup by contract.gid."""
    if not vendor or not vendor.strip():
        return []
    matches: dict[str, Contract] = {}
    for name_or_alias, contract in searchable:
        score = fuzz.WRatio(vendor, name_or_alias, processor=fuzz_utils.default_process)
        if score >= fuzzy_threshold:
            matches.setdefault(contract.gid, contract)
    return list(matches.values())


# Minimum partial_token_set_ratio for a Tableau Record Description ↔ Asana
# Contract Reason Text to count as a viable scope match. partial_token_set
# treats the short description as a target to find inside the longer reason
# text — "Snow removal" inside "Snow plowing and salting services" scores
# 100, while "Snow removal" against "Lawn care and landscape maintenance"
# scores ~30. A 70 floor means at least the description's keywords are
# present in the reason text.
DESCRIPTION_MATCH_MIN_SCORE: int = 70
# Margin the top-scoring candidate must beat its closest rival by to be
# declared the winner. 25 points with partial_token_set is enough that
# "snow" matching the snow-removal reason (100) decisively beats it
# matching the landscape-maintenance reason (~30). When two contracts
# overlap in scope (both ~60-70), the small margin keeps it ambiguous and
# falls through to the earliest-start tiebreak / operator review.
DESCRIPTION_MATCH_DECISIVE_MARGIN: int = 25


def _narrow_by_description(
    candidates: list[Contract],
    record_description: str,
) -> Contract | None:
    """Fuzzy-match a Tableau Record Description against each candidate's
    Contract Reason Text. Return the single winner if one candidate's score
    clears DESCRIPTION_MATCH_MIN_SCORE AND beats the runner-up by at least
    DESCRIPTION_MATCH_DECISIVE_MARGIN; else None (still ambiguous).

    partial_token_set_ratio is used because the description is short
    (invoice-line) and the reason text is long (operator sentence). It
    answers "does this description's bag-of-words appear inside this
    reason's bag-of-words?" rather than "are the two strings overall
    similar," which is the right semantic for picking scope.
    """
    desc = (record_description or "").strip()
    if not desc:
        return None
    # Skip candidates whose reason text is empty (older tasks pre-dating the
    # field) — they get score 0 and would always lose the narrowing, which
    # is fine; but we don't want to ELIMINATE them either if the row has no
    # description. Without scorable candidates, we can't narrow.
    scored: list[tuple[float, Contract]] = []
    for c in candidates:
        reason = (c.contract_reason_text or "").strip()
        if not reason:
            continue
        score = fuzz.partial_token_set_ratio(
            desc, reason, processor=fuzz_utils.default_process,
        )
        scored.append((score, c))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    top_score, top = scored[0]
    if top_score < DESCRIPTION_MATCH_MIN_SCORE:
        return None
    runner_up_score = scored[1][0] if len(scored) > 1 else 0
    if top_score - runner_up_score < DESCRIPTION_MATCH_DECISIVE_MARGIN:
        return None
    # #12: partial_token_set_ratio treats the short description as a subset to
    # find inside the long reason text, so a single SHARED GENERIC token (e.g.
    # "services", "contract") can score ~100 and decisively win even when the
    # match is noise. Require the winner to share at least one MEANINGFUL
    # (non-stop-word, non-numeric) token with the description, and to share
    # strictly more meaningful tokens than the runner-up. If the only overlap
    # is filler, fall through to ambiguous instead of auto-attributing money.
    desc_meaningful = _meaningful_tokens(desc)
    top_overlap = len(desc_meaningful & _meaningful_tokens(top.contract_reason_text))
    runner_overlap = 0
    if len(scored) > 1:
        runner_overlap = len(
            desc_meaningful & _meaningful_tokens(scored[1][1].contract_reason_text)
        )
    if top_overlap < 1 or top_overlap <= runner_overlap:
        return None
    return top


def _narrow_and_pick(
    candidates: list[Contract],
    *,
    campus: str,
    row_date: date | None,
    record_description: str,
    crosswalk: CampusCrosswalk,
) -> tuple[Contract | None, list[Contract]]:
    """Apply campus + date + description + earliest-start narrowing.

    Returns (winner, surviving). winner is the single contract attribution
    landed on (None if 0 or >1 survived). surviving is the candidates left
    after all narrowing — the "ambiguous" set when winner is None and len > 1,
    the "vendor-only hints" set when winner is None and len == 0 (caller can
    rebuild from candidates).
    """
    # Campus narrow.
    after_campus = [
        c for c in candidates
        if crosswalk.contract_matches_tableau_campus(c.campus_options, campus)
    ]
    if not after_campus:
        return None, []

    # Phase 14a: Even when only one contract matches by campus, the date
    # check is REQUIRED. Pre-Phase-14 this fast-pathed straight to attribution,
    # which silently swept pre-contract-start payments into the contract's
    # "Spent so far" (audit found $722k of out-of-term attribution from this
    # path alone). When the date fails, return the single candidate as the
    # ambiguity set so the operator sees it in Vendor Conflicts with a
    # "outside term" marker and can resolve it (extend the term in Asana,
    # or mark as pre-dates).
    if len(after_campus) == 1:
        only = after_campus[0]
        if _date_contains(only, row_date):
            return only, [only]
        return None, [only]

    # Date narrow — only contracts whose window contains the row's date.
    after_date = [c for c in after_campus if _date_contains(c, row_date)]
    if len(after_date) == 1:
        return after_date[0], [after_date[0]]

    if not after_date:
        # All campus-matching contracts have windows that don't cover this
        # date. Surface them as ambiguous candidates — operator will need to
        # either fix the date data, the contract dates, or assign manually.
        return None, after_campus

    # Description narrow: one vendor often holds two contracts in different
    # scopes (landscaping + snow removal). Match Tableau's Record Description
    # against each candidate's Contract Reason Text; a decisive top score
    # wins. Inserted BEFORE the earliest-start tiebreak because scope match
    # is more semantically meaningful than calendar age when contracts cover
    # different work.
    desc_winner = _narrow_by_description(after_date, record_description)
    if desc_winner is not None:
        return desc_winner, [desc_winner]

    # Crossover tiebreak: prefer the contract with the earliest start date.
    # "Use up old contract's budget first; new contract only takes over once
    # old is past_due" — date narrow already excluded past-due-for-this-row
    # contracts, so among the survivors the earliest-started one is the
    # OLDER (currently-running) of an overlapping pair.
    def _start_key(c: Contract) -> date:
        s = _contract_start_for_narrowing(c)
        # Contracts with no derivable start sort last (least preferred).
        return s if s is not None else date.max

    sorted_by_start = sorted(after_date, key=_start_key)
    earliest = sorted_by_start[0]
    earliest_start = _start_key(earliest)

    # If exactly one contract has the earliest start, it wins.
    tied = [c for c in sorted_by_start if _start_key(c) == earliest_start]
    if len(tied) == 1:
        return earliest, after_date

    # Still tied → genuinely ambiguous (same start date, same campus, same
    # vendor, no decisive description match — operator must disambiguate).
    return None, tied


# ---------------------------------------------------------------------------
# Per-row attribution
# ---------------------------------------------------------------------------

def _attribute_row(
    *,
    campus: str,
    dept: str,
    account_no: str,
    vendor: str,
    row_date: date | None,
    record_description: str,
    searchable: list[tuple[str, Contract]],
    crosswalk: CampusCrosswalk,
    learned_mappings: Mapping[
        tuple[str, str, str, str],
        list[tuple[str, str | None, str | None]],
    ],
    contracts_by_name: Mapping[str, list[Contract]],
    fuzzy_threshold: int,
    stale_learned_seen: set[tuple[str, str, str, str]],
) -> tuple[str, str | None, tuple[Contract, ...], tuple[Contract, ...]]:
    """Attribute a single row.

    Returns (status, gid, narrowed_candidates, vendor_only_candidates).
      - status: "auto" | "learned" | "ambiguous" | "unmatched" | "dropped"
      - gid: contract gid when status is auto/learned, else None
      - narrowed_candidates: contracts surviving narrowing (used for
        "ambiguous" surfacing)
      - vendor_only_candidates: contracts that matched by vendor before
        campus narrowing wiped them (used for "unmatched" hints)
    """
    key = (campus, dept, account_no, vendor)

    # 1. Drop-code short-circuit.
    if crosswalk.is_drop_code(campus):
        return "dropped", None, (), ()

    # 2. Learned Mappings take precedence over fuzzy matching when the name
    # resolves UNAMBIGUOUSLY. If the operator's learned name has multiple
    # open contracts (the same-name multi-task case), fall through to fuzzy
    # match + narrowing — the engine still needs to pick which task.
    if key in learned_mappings:
        # learned_mappings[key] is a LIST of LM entries — each a 3-tuple
        # (contract_name, contract_gid_or_None, description_pattern_or_None).
        # The list shape supports per-description-pattern splits within one
        # group key (e.g. one vendor that holds a landscaping AND a snow
        # contract → operator pins each by description pattern).
        lms = learned_mappings[key]
        # Pick the best-matching LM: a pattern-bearing LM whose pattern is
        # contained in the row description wins over the plain (no-pattern)
        # group-level LM. If multiple patterns match, the LONGEST pattern
        # wins (more specific). Plain LM is the fallback.
        best_pattern_match: tuple | None = None
        best_pattern_specificity = -1
        plain_lm: tuple | None = None
        for lm in lms:
            cn_i, gid_i, pat_i = lm
            if pat_i:
                # #7: match by normalized meaningful-token subset, not raw
                # substring, so a pattern survives volatile invoice numbers /
                # dates in future descriptions. Most-specific (most meaningful
                # tokens) pattern wins when several match.
                if _lm_pattern_matches(pat_i, record_description):
                    specificity = len(_meaningful_tokens(pat_i))
                    if specificity > best_pattern_specificity:
                        best_pattern_specificity = specificity
                        best_pattern_match = (cn_i, gid_i, pat_i)
            else:
                # Take the FIRST plain LM seen (operator shouldn't author
                # duplicates; the schema doesn't enforce uniqueness here
                # because the key shape changed).
                if plain_lm is None:
                    plain_lm = (cn_i, gid_i, None)
        chosen = best_pattern_match or plain_lm
        if chosen is not None:
            cn, pinned_gid, _ = chosen
            same_name_contracts = list(contracts_by_name.get(cn, []))
            # Gid-pinned (Vendor Conflicts panel): operator chose a specific
            # task. Only honor the pin if the task is still in the open
            # contracts set.
            if pinned_gid:
                pinned_match = next(
                    (c for c in same_name_contracts if c.gid == pinned_gid), None,
                )
                if pinned_match is not None:
                    # Phase 14a: pinned LM still has to pass the date check.
                    # Operator pinned this contract once; the engine must NOT
                    # silently apply that pin to rows whose dates pre-date the
                    # contract's term. Surface as ambiguous so the operator
                    # can either fix the term in Asana or mark as pre-dates.
                    if _date_contains(pinned_match, row_date):
                        return "learned", pinned_gid, (pinned_match,), ()
                    return "ambiguous", None, (pinned_match,), (pinned_match,)
                # Pinned gid no longer exists — fall through to name resolution.
            if len(same_name_contracts) == 1:
                # Phase 14a: even when the learned name resolves to exactly
                # one open contract, enforce the date check. Same rationale
                # as the pinned-gid path -- a learned name match doesn't
                # license attribution outside the contract's term.
                only = same_name_contracts[0]
                if _date_contains(only, row_date):
                    return "learned", only.gid, (only,), ()
                return "ambiguous", None, (only,), (only,)
            if len(same_name_contracts) > 1:
                # Treat learned-name as the candidate set and run normal narrowing.
                winner, surviving = _narrow_and_pick(
                    same_name_contracts, campus=campus, row_date=row_date,
                    record_description=record_description, crosswalk=crosswalk,
                )
                if winner is not None:
                    return "learned", winner.gid, tuple(surviving), ()
                # Couldn't narrow even within the learned name's tasks → ambiguous.
                return "ambiguous", None, tuple(surviving), tuple(same_name_contracts)
            # Stale: learned name no longer maps to any open contract — fall
            # through to fuzzy match, log once per group_key+stale-name pair.
            if key not in stale_learned_seen:
                stale_learned_seen.add(key)
                log.warning(
                    "stale Learned Mapping for group %s -> %r: that contract is no "
                    "longer in the open Asana contracts list. Falling through to "
                    "fuzzy match. Edit or delete the Learned Mappings row to clean up.",
                    "|".join(key), cn,
                )

    # 3. Vendor fuzzy match (with aliases). Candidates may be empty.
    vendor_candidates = _match_vendor(vendor, searchable, fuzzy_threshold)
    if not vendor_candidates:
        return "unmatched", None, (), ()

    # 4-7. Campus + date + description + tiebreak.
    winner, surviving = _narrow_and_pick(
        vendor_candidates, campus=campus, row_date=row_date,
        record_description=record_description, crosswalk=crosswalk,
    )

    if winner is not None:
        return "auto", winner.gid, tuple(surviving), tuple(vendor_candidates)

    if not surviving:
        # Campus narrow zeroed the candidate list — vendor matched but no
        # contract covers this campus. Surface as "unmatched" with the
        # vendor-only hints.
        return "unmatched", None, (), tuple(vendor_candidates)

    # surviving > 1 → ambiguous.
    return "ambiguous", None, tuple(surviving), tuple(vendor_candidates)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def attribute(
    df: pd.DataFrame,
    contracts: list[Contract],
    aliases: Mapping[str, list[str]],
    crosswalk: CampusCrosswalk,
    learned_mappings: Mapping[
        tuple[str, str, str, str],
        list[tuple[str, str | None, str | None]],
    ],
    *,
    fuzzy_threshold: int = DEFAULT_FUZZY_THRESHOLD,
) -> AttributionRun:
    """Run attribution against an in-scope DataFrame.

    df must already be in-scope (ACCOUNTS_IN_SCOPE × DEPTS_IN_SCOPE filtered)
    and have Amount as a signed float, plus a Date column (datetime / Timestamp).
    """
    # Build the search corpus once: (string, contract) pairs covering contract
    # names AND every alias. Same contract may appear multiple times so we
    # dedup by contract.gid when collecting matches.
    searchable: list[tuple[str, Contract]] = []
    for c in contracts:
        if c.name:
            searchable.append((c.name, c))
        for alias in aliases.get(c.name, []):
            if alias:
                searchable.append((alias, c))

    # name → [Contract, ...] (multiple tasks may share a name, intentionally).
    contracts_by_name: dict[str, list[Contract]] = {}
    for c in contracts:
        if c.name:
            contracts_by_name.setdefault(c.name, []).append(c)
    # gid → Contract — used for surfacing section_name on auto attrs and
    # rebuilding contract details from row gids.
    contracts_by_gid: dict[str, Contract] = {c.gid: c for c in contracts}

    # ------------------------------------------------------------------
    # Pass 1: per-row attribution. Build a parallel array of gids + a
    # diagnostic dict (group_key -> aggregated row outcomes) so the group
    # summary in pass 2 doesn't have to recompute.
    # ------------------------------------------------------------------
    # Per-row attributed gid, POSITIONAL (df row order). The FINAL spend map
    # (run.row_gids) is derived from this AFTER Pass 2 decides each group's
    # status, so only cleanly-attributed groups contribute spend (#3) and
    # duplicate / blank Record No can't collapse rows (#2).
    row_raw_gids: list[str | None] = []
    # Parallel group key per row, so the post-pass-2 gating can map each row
    # back to its group's final status.
    row_keys: list[tuple[str, str, str, str]] = []
    # For each group key, collect: rows count, amount, per-row results,
    # candidate sets (union across rows).
    group_state: dict[tuple[str, str, str, str], dict] = {}
    # Dedup the stale-learned-mapping warning to one per group key.
    stale_learned_seen: set[tuple[str, str, str, str]] = set()

    if len(df) == 0:
        # Empty input → empty result. Keep summary_dict numerics sane.
        return AttributionRun(results=(), row_gids=())

    # Iterate rows ONCE. Use the underlying numpy arrays for speed (the df
    # can hit 18k rows on the live data) — direct column access via .values
    # is ~5x faster than .iterrows() and avoids itertuples' name-mangling of
    # columns with spaces ("Account No" → "Account_No" / "_3").
    campus_arr = df["Campus"].values
    dept_arr = df["Dept"].values
    account_arr = df["Account No"].values
    vendor_arr = df["Vendor"].values
    date_arr = df["Date"].values
    amount_arr = df["Amount"].values
    # Record Description is the operator-readable invoice line — used by the
    # description-vs-reason narrowing step for same-vendor multi-task
    # ambiguity. Optional: an export missing this column degrades to no
    # description narrowing (engine falls through to earliest-start tiebreak).
    desc_arr = df["Record Description"].values if "Record Description" in df.columns else None

    for i in range(len(df)):
        campus = _safe_str(campus_arr[i])
        dept = _safe_str(dept_arr[i])
        account_no = _safe_str(account_arr[i])
        vendor = _safe_str(vendor_arr[i])
        row_date = _row_date_to_pydate(date_arr[i])
        record_description = _safe_str(desc_arr[i]) if desc_arr is not None else ""

        status, gid, narrowed, vendor_only = _attribute_row(
            campus=campus, dept=dept, account_no=account_no, vendor=vendor,
            row_date=row_date, record_description=record_description,
            searchable=searchable, crosswalk=crosswalk,
            learned_mappings=learned_mappings, contracts_by_name=contracts_by_name,
            fuzzy_threshold=fuzzy_threshold,
            stale_learned_seen=stale_learned_seen,
        )
        row_raw_gids.append(gid)

        key = (campus, dept, account_no, vendor)
        row_keys.append(key)
        st = group_state.setdefault(key, {
            "rows": 0, "amount": 0.0,
            "row_statuses": [],            # parallel list of per-row statuses
            "row_gids": [],                # parallel list of per-row gids (None ok)
            "row_descriptions": [],        # parallel list of per-row descriptions
            "row_amounts": [],             # parallel list of per-row amounts
            "row_dates": [],               # parallel list of per-row pydate (or None)
            "narrowed_union": {},          # gid -> Contract (union across rows)
            "vendor_only_union": {},       # gid -> Contract (union across rows)
            "sample_description": "",
            "first_date_ts": None,
            "last_date_ts": None,
        })
        st["rows"] += 1
        st["row_statuses"].append(status)
        st["row_gids"].append(gid)
        st["row_descriptions"].append(record_description)
        # Track row amount alongside so distinct-description aggregation can
        # compute dollar weight per description without re-reading the df.
        st["row_amounts"].append(float(amount_arr[i]))
        # Track per-row date so distinct-description aggregation can compute
        # min/max date per description bucket. Phase 13: lets the Vendor
        # Conflicts UI reject auto-suggesting a candidate whose contract
        # term doesn't overlap the description's date range.
        st["row_dates"].append(row_date)
        for c in narrowed:
            st["narrowed_union"].setdefault(c.gid, c)
        for c in vendor_only:
            st["vendor_only_union"].setdefault(c.gid, c)

    # Pull aggregates from the df ONCE (faster than per-row math). Group by
    # the same key the row loop used so we can stitch state on.
    agg = (
        df.groupby(["Campus", "Dept", "Account No", "Vendor"], dropna=False, as_index=False)
        .agg(
            rows=("Record No", "count"),
            amount=("Amount", "sum"),
            sample_description=("Record Description", _first_non_empty),
            first_date=("Date", "min"),
            last_date=("Date", "max"),
        )
    )

    # ------------------------------------------------------------------
    # Pass 2: build one AttributionResult per group from the per-row data.
    # ------------------------------------------------------------------
    results: list[AttributionResult] = []
    for _, g in agg.iterrows():
        campus = _safe_str(g["Campus"])
        dept = _safe_str(g["Dept"])
        account_no = _safe_str(g["Account No"])
        vendor = _safe_str(g["Vendor"])
        key = (campus, dept, account_no, vendor)
        st = group_state.get(key)
        if st is None:
            # Defensive: shouldn't happen but build a minimal result.
            st = {"row_statuses": [], "row_gids": [], "narrowed_union": {},
                  "vendor_only_union": {}}

        group_key_str = "|".join(key)
        rows_count = int(g["rows"])
        amount = float(g["amount"])
        sample_description = _safe_str(g["sample_description"])
        first_date = _ts_to_iso_date(g["first_date"])
        last_date = _ts_to_iso_date(g["last_date"])

        row_statuses = st["row_statuses"]
        row_gids_list = st["row_gids"]

        base = dict(
            group_key=group_key_str,
            campus=campus, dept=dept, account_no=account_no, vendor=vendor,
            rows=rows_count, amount=amount,
            sample_description=sample_description,
            first_date=first_date, last_date=last_date,
        )

        # Whole group is dropped if every row is dropped.
        if row_statuses and all(s == "dropped" for s in row_statuses):
            results.append(AttributionResult(
                **base, status="dropped",
                contract_name=None, contract_gid=None,
                candidate_names=(), candidate_gids=(),
            ))
            continue

        # Mixed outcomes within a group: derive a group status from the row
        # outcomes. The order of checks matters — a single ambiguous row
        # makes the WHOLE group ambiguous (operator must clear it).
        any_ambiguous = any(s == "ambiguous" for s in row_statuses)
        any_unmatched = any(s == "unmatched" for s in row_statuses)
        attributed_gids = [gid for gid in row_gids_list if gid is not None]
        unique_gids = list(dict.fromkeys(attributed_gids))   # preserves order, dedup

        if any_ambiguous:
            # Surface the union of surviving candidates across rows.
            cands = list(st["narrowed_union"].values())
            # Aggregate per-distinct-description for the Vendor Conflicts UI
            # per-description picker. Empty-string description is treated as
            # its own bucket so the operator can see how much spend has no
            # description (still pickable, just labeled as "no description").
            # Phase 13: also track min/max date per bucket so the UI can
            # reject auto-suggesting a contract whose term doesn't overlap.
            # None dates are skipped — a bucket with no parsable dates ends
            # up with min/max = None and the UI falls back to text-only.
            distinct: dict[str, dict] = {}
            for desc, amt, dt in zip(
                st["row_descriptions"], st["row_amounts"], st["row_dates"],
            ):
                key_d = (desc or "").strip()
                bucket = distinct.setdefault(
                    key_d, {"rows": 0, "amount": 0.0, "min_dt": None,
                            "max_dt": None, "dates": []},
                )
                bucket["rows"] += 1
                bucket["amount"] += amt
                if dt is not None:
                    bucket["dates"].append(dt)
                    if bucket["min_dt"] is None or dt < bucket["min_dt"]:
                        bucket["min_dt"] = dt
                    if bucket["max_dt"] is None or dt > bucket["max_dt"]:
                        bucket["max_dt"] = dt
            distinct_descriptions = tuple(
                (
                    desc,
                    b["rows"],
                    round(b["amount"], 2),
                    b["min_dt"].isoformat() if b["min_dt"] is not None else "",
                    b["max_dt"].isoformat() if b["max_dt"] is not None else "",
                )
                for desc, b in sorted(
                    distinct.items(), key=lambda kv: kv[1]["amount"], reverse=True,
                )
            )
            # Phase 14a + #5: flag the group "out of term" when there is at
            # least one DESCRIPTION BUCKET whose every dated row falls outside
            # every candidate's [start, due]. Computed PER BUCKET (not via a
            # single any() over the union of all rows × all candidates) so a
            # MIXED group — some buckets in-term, some pre-dating the contract
            # — is still flagged, routing its out-of-term bucket to Vendor
            # Conflicts instead of stranding it on Needs Tagging Open. Uses
            # the actual per-row dates in each bucket, so it's precise.
            all_out_of_term = False
            if cands:
                for b in distinct.values():
                    bucket_dates = b["dates"]
                    if not bucket_dates:
                        continue
                    covered = any(
                        _date_contains(c, dt)
                        for c in cands for dt in bucket_dates
                    )
                    if not covered:
                        all_out_of_term = True
                        break
            results.append(AttributionResult(
                **base, status="ambiguous",
                contract_name=None, contract_gid=None,
                candidate_names=tuple(c.name for c in cands),
                candidate_gids=tuple(c.gid for c in cands),
                distinct_descriptions=distinct_descriptions,
                all_out_of_term=all_out_of_term,
            ))
            continue

        if any_unmatched and not attributed_gids:
            # No rows attributed AND any unmatched → genuine unmatched.
            # Show the vendor-only matches as hints.
            hints = list(st["vendor_only_union"].values())
            results.append(AttributionResult(
                **base, status="unmatched",
                contract_name=None, contract_gid=None,
                candidate_names=tuple(c.name for c in hints),
                candidate_gids=tuple(c.gid for c in hints),
            ))
            continue

        if any_unmatched and attributed_gids:
            # Some rows attributed, others didn't → mixed; promote to ambiguous
            # so the operator decides. Show the attributed gids as candidates
            # plus any vendor-only hints from the unmatched rows.
            survivors_by_gid: dict[str, Contract] = {}
            for gid in unique_gids:
                c = contracts_by_gid.get(gid)
                if c is not None:
                    survivors_by_gid[gid] = c
            for c in st["vendor_only_union"].values():
                survivors_by_gid.setdefault(c.gid, c)
            cands = list(survivors_by_gid.values())
            results.append(AttributionResult(
                **base, status="ambiguous",
                contract_name=None, contract_gid=None,
                candidate_names=tuple(c.name for c in cands),
                candidate_gids=tuple(c.gid for c in cands),
            ))
            continue

        # Every row attributed cleanly.
        if len(unique_gids) == 1:
            gid = unique_gids[0]
            c = contracts_by_gid.get(gid)
            # Status preference: "learned" if EVERY row's status was "learned",
            # else "auto". Mixed learned+auto on the same gid → "auto" (the
            # operator's mapping is still respected; we just don't claim the
            # whole group was learned).
            group_status = "learned" if all(s == "learned" for s in row_statuses) else "auto"
            _warn_if_non_live(c, group_key_str, source=group_status)
            results.append(AttributionResult(
                **base, status=group_status,
                contract_name=c.name if c else None,
                contract_gid=gid,
                candidate_names=(c.name,) if c else (),
                candidate_gids=(gid,),
            ))
            continue

        # Crossover split: multiple distinct gids in one group, NO ambiguous /
        # unmatched rows. Build splits = ((gid, name, rows, amount), ...).
        gid_to_rows: dict[str, int] = {}
        gid_to_amount: dict[str, float] = {}
        # We need the per-row amounts indexed by row → gid. Recompute by
        # zipping the original df rows to row_gids_list. Slow path but only
        # runs for split groups (rare in practice).
        # Pull just this group's rows + amounts:
        mask = (
            (df["Campus"] == campus) & (df["Dept"] == dept)
            & (df["Account No"] == account_no) & (df["Vendor"] == vendor)
        )
        group_amounts = df.loc[mask, "Amount"].tolist()
        # If lengths differ (NA-coercion drift), fall back to even distribution.
        if len(group_amounts) == len(row_gids_list):
            for gid, amt in zip(row_gids_list, group_amounts):
                if gid is None:
                    continue
                gid_to_rows[gid] = gid_to_rows.get(gid, 0) + 1
                gid_to_amount[gid] = gid_to_amount.get(gid, 0.0) + float(amt)
        else:
            # Defensive: pure rows distribution; amount left empty.
            for gid in row_gids_list:
                if gid is None: continue
                gid_to_rows[gid] = gid_to_rows.get(gid, 0) + 1

        splits = tuple(
            (
                gid,
                contracts_by_gid[gid].name if gid in contracts_by_gid else "",
                gid_to_rows.get(gid, 0),
                round(gid_to_amount.get(gid, 0.0), 2),
            )
            for gid in unique_gids
        )
        cand_names = tuple(s[1] for s in splits)
        cand_gids = tuple(s[0] for s in splits)
        results.append(AttributionResult(
            **base, status="split",
            contract_name=None, contract_gid=None,
            candidate_names=cand_names, candidate_gids=cand_gids,
            splits=splits,
        ))

    # Derive the FINAL positional spend map (#2/#3). A row's gid is counted
    # toward Dashboard spend ONLY when its group attributed cleanly
    # (auto / learned / split). Ambiguous / unmatched / dropped groups
    # contribute None, so their spend never lands on a contract before the
    # operator resolves the conflict. For split groups each row keeps its own
    # gid (the crossover breakdown). Built positionally so duplicate / blank
    # Record No can't collapse rows.
    _CLEAN = {"auto", "learned", "split"}
    group_final_status = {
        (r.campus, r.dept, r.account_no, r.vendor): r.status
        for r in results
    }
    final_row_gids: list[str | None] = [
        raw if group_final_status.get(k) in _CLEAN else None
        for raw, k in zip(row_raw_gids, row_keys)
    ]

    run = AttributionRun(
        results=tuple(results), row_gids=tuple(final_row_gids),
    )
    log.info("attribution: %s", run.summary_dict())
    return run


# ---------------------------------------------------------------------------
# Misc helpers (kept for backward-compat with importers)
# ---------------------------------------------------------------------------

def _warn_if_non_live(contract: Contract | None, group_key: str, *, source: str) -> None:
    """Log a warning when attribution lands on a non-write-gate contract.

    The Step 5 writer will skip these (Pending Onboarding contracts are
    excluded from writes per spec section 7). Without this signal an
    operator inspecting the attribution summary wouldn't know that the
    auto-attribution to a Pending Onboarding contract has no downstream
    effect.
    """
    if contract is None or contract.section_name is None:
        return
    if contract.section_name == settings.ASANA_WRITE_GATE_SECTION:
        return
    log.warning(
        "%s attribution lands on a non-write-gate contract: group %s -> %r "
        "in section %r. Step 5 writer will skip this contract until the "
        "section is changed to %r.",
        source, group_key, contract.name, contract.section_name,
        settings.ASANA_WRITE_GATE_SECTION,
    )


__all__ = [
    "DEFAULT_FUZZY_THRESHOLD",
    "AttributionResult",
    "AttributionRun",
    "attribute",
]
