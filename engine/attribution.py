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
    status: str              # "auto" | "learned" | "split" | "ambiguous" | "unmatched" | "miscoded" | "dropped"
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
        return self.status in ("ambiguous", "unmatched", "miscoded")


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
    def miscoded(self) -> list[AttributionResult]: return self.by_status("miscoded")
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
            "miscoded": len(self.miscoded),
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
    "covers", "include", "includes", "needed", "new", "operator",
    # ponytail: generic service-ACTION words — the subject noun (snow/ice vs
    # tree/debris) is the real discriminator, so "removal" alone must not link
    # a tree-removal contract to a snow record. Add a sibling action word here
    # if the same cross-talk shows up (e.g. "management", "maintenance").
    "removal", "reversed",
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


def stamp_pcard_links(df: pd.DataFrame, links: list[dict]) -> int:
    """Operator P-Card links → pre-attribution vendor stamp. MUTATES df.

    For each link {campus, dept, account_no, name, pattern}, stamp Vendor=name
    onto every BLANK-vendor row in that (Campus, Dept, Account No) whose Record
    Description matches the pattern (token-subset). Those rows then split into
    their own clean vendor group and attribute to the contract via the normal
    fuzzy path — instead of being one vendor in a big blank-vendor group, which
    the group-status logic would poison to 'ambiguous'. The unrelated
    blank-vendor rows (Amazon, Lowes, …) keep their blank Vendor and stay on
    the P-Card tab. Only touches rows whose Vendor is currently blank, so it
    can never overwrite a real Tableau vendor. Returns the rows stamped.
    """
    if not links or df.empty or "Vendor" not in df.columns:
        return 0
    desc_col = "Record Description" if "Record Description" in df.columns else None
    vendor = df["Vendor"].astype("string")
    blank = vendor.isna() | (vendor.str.strip() == "")
    stamped = 0
    for link in links:
        grp = (
            blank
            & (df["Campus"].astype("string").str.strip() == link["campus"])
            & (df["Dept"].astype("string").str.strip() == link["dept"])
            & (df["Account No"].astype("string").str.strip() == link["account_no"])
        )
        if not grp.any():
            continue
        for idx in df.index[grp]:
            desc = _safe_str(df.at[idx, desc_col]) if desc_col else ""
            if _lm_pattern_matches(link["pattern"], desc):
                df.at[idx, "Vendor"] = link["name"]
                stamped += 1
    return stamped


def _distinct_buckets(st: dict) -> dict[str, dict]:
    """Per-distinct-description aggregation for a group: description → {rows,
    amount, min_dt, max_dt, dates}. Empty description is its own bucket. Shared
    by the ambiguous and unmatched branches — the unmatched (P-Card) case needs
    it so the P-Card tab's name matcher can see EVERY description, not just the
    one Sample Record Description (which is often unrelated noise)."""
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
    return distinct


def _distinct_descriptions_tuple(distinct: dict[str, dict]) -> tuple:
    """Serialize _distinct_buckets to the (desc, rows, amount, min, max) tuples
    the Needs Tagging row persists (Distinct Descriptions JSON), sorted by
    descending dollar weight."""
    return tuple(
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


def _dept_set(raw: str | None) -> frozenset[str]:
    """Asana's Dept is free text and may list MULTIPLE accepted codes
    ('000, 107' means a row coded to either 000 or 107 belongs here). Split on
    commas/whitespace into the set of accepted codes. Empty when uncoded."""
    if not raw:
        return frozenset()
    return frozenset(t for t in re.split(r"[,\s]+", raw.strip()) if t)


def _coding_compatible(c: Contract, dept: str, account_no: str) -> bool:
    """True unless the contract's Dept/Acc CONTRADICTS the row's coding.

    The opex coding-narrow (2026-06): Asana now carries Dept + Acc, so a vendor
    fuzzy-match to a contract in a DIFFERENT account/dept is a false positive
    and gets excluded. Leniency is reserved for UNCODED contracts (dept/acc
    None) — a wildcard while the operator is mid-rollout coding Asana, so
    nothing regresses. A coded-but-mismatched contract is excluded (hard).

    Dept may be multi-valued ('000, 107') — the row matches if its single
    Tableau dept is in that set. Acc is an Asana number field (single value)."""
    dept_set = _dept_set(c.dept)
    if dept_set and dept and dept not in dept_set:
        return False
    if c.acc is not None and account_no and c.acc != account_no:
        return False
    return True


def _strip_campus_suffix(vendor: str, campus_codes: frozenset[str]) -> str:
    """Drop a trailing ' - <CAMPUS>(-<CAMPUS>)*' suffix from a Tableau vendor.

    Tableau sometimes bakes the campus list into the vendor name itself, e.g.
    'Corporate Cleaning Group Inc - LNX-OPK-WWK-DRB'. Campus already lives in
    its own column, so that suffix is pure noise — and because it's long, it
    drags the WRatio below threshold (90 vs the real 95), so the contract never
    matches. Strips it ONLY when EVERY token after the last ' - ' is a known
    campus code, so a vendor legitimately named 'X - Downtown Branch' is left
    untouched. Returns the vendor unchanged when there is no campus suffix.
    """
    if not campus_codes or " - " not in vendor:
        return vendor
    head, tail = vendor.rsplit(" - ", 1)
    tokens = tail.split("-")
    if tokens and all(t in campus_codes for t in tokens):
        return head.strip()
    return vendor


def _match_vendor(
    vendor: str,
    searchable: list[tuple[str, Contract]],
    fuzzy_threshold: int,
    campus_codes: frozenset[str] = frozenset(),
) -> list[Contract]:
    """Return unique contracts whose name or any alias scores ≥ threshold
    against the Tableau Vendor string. Dedup by contract.gid.

    Also scores a campus-suffix-stripped variant of the vendor (see
    _strip_campus_suffix) so a 'Vendor - CAMPUS-LIST' Tableau name still
    matches its contract. The stripped variant only differs for suffix-bearing
    vendors, so the common case stays a single pass."""
    if not vendor or not vendor.strip():
        return []
    queries = [vendor]
    stripped = _strip_campus_suffix(vendor, campus_codes)
    if stripped and stripped != vendor:
        queries.append(stripped)
    matches: dict[str, Contract] = {}
    for q in queries:
        for name_or_alias, contract in searchable:
            score = fuzz.WRatio(q, name_or_alias, processor=fuzz_utils.default_process)
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


def _pick_within_term(
    pool: list[Contract],
    *,
    row_date: date | None,
    record_description: str,
) -> tuple[Contract | None, list[Contract]]:
    """Date + description + earliest-start narrowing over an ALREADY
    campus-filtered pool. Split out of _narrow_and_pick so the campus-narrow
    can first prefer specific contracts over the All-Campuses wildcard.

    Returns (winner, surviving) — winner is None when 0 or >1 survive.
    """
    if not pool:
        return None, []

    # Phase 14a: Even when only one contract matches by campus, the date
    # check is REQUIRED. Pre-Phase-14 this fast-pathed straight to attribution,
    # which silently swept pre-contract-start payments into the contract's
    # "Spent so far" (audit found $722k of out-of-term attribution from this
    # path alone). When the date fails, return the single candidate as the
    # ambiguity set so the operator sees it in Vendor Conflicts with a
    # "outside term" marker and can resolve it (extend the term in Asana,
    # or mark as pre-dates).
    if len(pool) == 1:
        only = pool[0]
        if _date_contains(only, row_date):
            return only, [only]
        return None, [only]

    # Date narrow — only contracts whose window contains the row's date.
    after_date = [c for c in pool if _date_contains(c, row_date)]
    if len(after_date) == 1:
        return after_date[0], [after_date[0]]

    if not after_date:
        # All campus-matching contracts have windows that don't cover this
        # date. Surface them as ambiguous candidates — operator will need to
        # either fix the date data, the contract dates, or assign manually.
        return None, pool

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


def _narrow_and_pick(
    candidates: list[Contract],
    *,
    campus: str,
    row_date: date | None,
    record_description: str,
    crosswalk: CampusCrosswalk,
    ask_on_wildcard: bool = True,
) -> tuple[Contract | None, list[Contract]]:
    """Apply campus + date + description + earliest-start narrowing.

    Returns (winner, surviving). winner is the single contract attribution
    landed on (None if 0 or >1 survived). surviving is the candidates left
    after all narrowing — the "ambiguous" set when winner is None and len > 1,
    the "vendor-only hints" set when winner is None and len == 0 (caller can
    rebuild from candidates).

    "All Campuses" (wildcard) handling:
      - PREFER SPECIFIC: if any candidate matches this campus specifically, the
        wildcard-only candidates are dropped — a real per-campus contract always
        beats the org-wide magnet (prevents e.g. an All-Campuses DH Pace task
        from swallowing a campus's spend away from its own DH Pace contract).
      - ASK ON WILDCARD-ONLY: when the ONLY matches are via the wildcard, don't
        auto-attribute — surface the pick as ambiguous so the operator confirms
        once (the confirm becomes a Learned Mapping; future ingests attribute
        directly). Suppressed (ask_on_wildcard=False) when the caller already
        has an operator-confirmed answer (the multi-task Learned Mapping path).
    """
    # Campus narrow.
    after_campus = [
        c for c in candidates
        if crosswalk.contract_matches_tableau_campus(c.campus_options, campus)
    ]
    if not after_campus:
        return None, []

    # Prefer campus-SPECIFIC over the All-Campuses wildcard.
    specific = [
        c for c in after_campus
        if crosswalk.contract_matches_specific(c.campus_options, campus)
    ]
    wildcard_only = not specific
    pool = specific if specific else after_campus

    winner, surviving = _pick_within_term(
        pool, row_date=row_date, record_description=record_description,
    )

    # Ask-on-wildcard-only: never silently auto-attribute a match that exists
    # solely via the All-Campuses wildcard. Surface the winner (single-candidate
    # ambiguous) so the operator confirms once.
    if wildcard_only and ask_on_wildcard and winner is not None:
        return None, [winner]
    return winner, surviving


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
        list[tuple[str, str | None, str | None, bool]],
    ],
    contracts_by_name: Mapping[str, list[Contract]],
    fuzzy_threshold: int,
    stale_learned_seen: set[tuple[str, str, str, str]],
    campus_codes: frozenset[str] = frozenset(),
    ignore_rules: frozenset[tuple[str, str, str]] = frozenset(),
) -> tuple[str, str | None, tuple[Contract, ...], tuple[Contract, ...]]:
    """Attribute a single row.

    Returns (status, gid, narrowed_candidates, vendor_only_candidates).
      - status: "auto" | "learned" | "ambiguous" | "unmatched" | "miscoded" | "dropped"
        ("miscoded" = vendor+campus+term align, only Dept/Acct differ;
        narrowed_candidates holds the would-attribute-except-coding contract(s))
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

    # 1b. Ignore-rule short-circuit: operator-configured (Campus, Dept, Account
    # No) triples another team owns (e.g. CEN/000/63080). Dropped BEFORE the LM
    # and fuzzy paths so their spend never attributes and any stale LM keyed to
    # the triple is inert. Exact-triple match only — the same account attributes
    # normally at any other campus/dept, and 63015 (CapEx) is never in the set.
    if (campus, dept, account_no) in ignore_rules:
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
            # LM entries are 4-tuples: (name, gid, pattern, cross_campus_exc).
            # ponytail: tolerate legacy 3-tuples (default flag False) so
            # hand-authored / older callers don't need the 4th element.
            cn_i, gid_i, pat_i, xc_i = lm if len(lm) == 4 else (*lm, False)
            if pat_i:
                # #7: match by normalized meaningful-token subset, not raw
                # substring, so a pattern survives volatile invoice numbers /
                # dates in future descriptions. Most-specific (most meaningful
                # tokens) pattern wins when several match.
                if _lm_pattern_matches(pat_i, record_description):
                    specificity = len(_meaningful_tokens(pat_i))
                    if specificity > best_pattern_specificity:
                        best_pattern_specificity = specificity
                        best_pattern_match = (cn_i, gid_i, pat_i, xc_i)
            else:
                # Take the FIRST plain LM seen (operator shouldn't author
                # duplicates; the schema doesn't enforce uniqueness here
                # because the key shape changed).
                if plain_lm is None:
                    plain_lm = (cn_i, gid_i, None, xc_i)
        chosen = best_pattern_match or plain_lm
        if chosen is not None:
            cn, pinned_gid, _, is_exc = chosen
            same_name_contracts = list(contracts_by_name.get(cn, []))
            # Flagged CROSS-CAMPUS EXCEPTION -- handled BEFORE same-campus
            # resolution so an old exception can't silently switch to (or be
            # overridden by) a newly-valid same-campus contract. The operator
            # DELIBERATELY mapped this key to a contract on another campus
            # (e.g. WAR-coded spend billed to a CEN contract). Resolve the
            # intended target; if it's genuinely cross-campus, honor it --
            # UNLESS the row's own campus now has a live home for this vendor,
            # in which case re-ask (surface both). "Play it safe" per operator.
            if is_exc and same_name_contracts:
                target = None
                if pinned_gid:
                    target = next(
                        (c for c in same_name_contracts if c.gid == pinned_gid), None,
                    )
                if target is None and len(same_name_contracts) == 1:
                    target = same_name_contracts[0]
                if target is not None and not crosswalk.contract_matches_tableau_campus(
                    target.campus_options, campus,
                ):
                    same_campus_home = [
                        c for c in _match_vendor(
                            vendor, searchable, fuzzy_threshold, campus_codes,
                        )
                        if c.gid != target.gid
                        and crosswalk.contract_matches_tableau_campus(
                            c.campus_options, campus,
                        )
                        and _coding_compatible(c, dept, account_no)
                        and _date_contains(c, row_date)
                    ]
                    if same_campus_home:
                        cands = tuple(
                            {c.gid: c for c in (target, *same_campus_home)}.values()
                        )
                        return "ambiguous", None, cands, cands
                    # No same-campus home → honor the exception (date-gated,
                    # same current-term-only drop rule as the pinned path).
                    if _date_contains(target, row_date):
                        return "learned", target.gid, (target,), ()
                    return "dropped", None, (), ()
                # target unresolved / actually same-campus → fall through to
                # normal same-campus resolution below.
            # A Learned Mapping must still respect the campus crosswalk. The LM
            # key includes campus, so a pin only applies to contracts that serve
            # THIS row's campus. Resolving against the campus-compatible tasks
            # only stops an operator pick (or a same-name default) that crossed
            # campus from leaking spend onto a wrong-campus contract -- e.g. an
            # "EDM|...|Oklahoma Chiller" pin onto a CEN-only contract. When no
            # same-name task serves this campus the LM doesn't apply: fall
            # through to vendor match + campus narrow, which routes the row to
            # the right-campus contract or parks it (Needs Tagging / No-Home).
            campus_ok = [
                c for c in same_name_contracts
                if crosswalk.contract_matches_tableau_campus(c.campus_options, campus)
            ]
            # Gid-pinned (Vendor Conflicts panel): operator chose a specific
            # task. Only honor the pin if that task is open AND serves this campus.
            if pinned_gid:
                pinned_match = next(
                    (c for c in campus_ok if c.gid == pinned_gid), None,
                )
                if pinned_match is not None:
                    # Pinned + campus-OK still has to pass the date check. But the
                    # operator pinned THIS contract, so an out-of-term row is
                    # not an ambiguity — under the current-term-only model it's
                    # pre-term spend that belongs to a prior contract. Drop it
                    # (excluded from spend, gid None) rather than flagging the
                    # row ambiguous, which would poison the whole group and
                    # force the operator to re-mark pre-dates every ingest (the
                    # export carries full history). ponytail: drop is the
                    # operator's stated intent for spend outside the term.
                    if _date_contains(pinned_match, row_date):
                        return "learned", pinned_gid, (pinned_match,), ()
                    return "dropped", None, (), ()
                # Pinned gid is stale OR cross-campus — fall through to name
                # resolution below (over the campus-compatible set).
            if len(campus_ok) == 1:
                # The learned name resolves to exactly one campus-compatible open
                # contract; enforce the date check. Out-of-term → pre-term spend
                # → drop (same rationale as the pinned-gid path above).
                only = campus_ok[0]
                if _date_contains(only, row_date):
                    return "learned", only.gid, (only,), ()
                return "dropped", None, (), ()
            if len(campus_ok) > 1:
                # Out-of-term for EVERY campus-compatible task under the learned
                # name → pre-term spend → drop, consistent with the single-name
                # and pinned paths. (Only an in-term-but-undecidable row stays
                # ambiguous below.)
                if not any(_date_contains(c, row_date) for c in campus_ok):
                    return "dropped", None, (), ()
                # Narrow within the learned name's campus-compatible tasks.
                # ask_on_wildcard=False: the operator already confirmed this
                # name via the Learned Mapping, so a wildcard match here needs
                # no second confirm.
                winner, surviving = _narrow_and_pick(
                    campus_ok, campus=campus, row_date=row_date,
                    record_description=record_description, crosswalk=crosswalk,
                    ask_on_wildcard=False,
                )
                if winner is not None:
                    return "learned", winner.gid, tuple(surviving), ()
                # Couldn't narrow even within them → ambiguous.
                return "ambiguous", None, tuple(surviving), tuple(campus_ok)
            # No campus-compatible open task carries the learned name. Either the
            # LM is stale (name maps to no open contract) or it's an UNFLAGGED
            # cross-campus pin (contract exists but serves another campus, and
            # the operator did NOT mark it a Cross-Campus Exception → treated as
            # an accidental leak). Log once per group key and fall through to
            # fuzzy match + campus narrow, which handles both correctly.
            if key not in stale_learned_seen:
                stale_learned_seen.add(key)
                if same_name_contracts:
                    log.warning(
                        "cross-campus Learned Mapping for group %s -> %r: that "
                        "name's open contract(s) do not serve campus %r. Ignoring "
                        "the pin and falling through to vendor match. Fix or delete "
                        "the Learned Mappings row.",
                        "|".join(key), cn, campus,
                    )
                else:
                    log.warning(
                        "stale Learned Mapping for group %s -> %r: that contract is no "
                        "longer in the open Asana contracts list. Falling through to "
                        "fuzzy match. Edit or delete the Learned Mappings row to clean up.",
                        "|".join(key), cn,
                    )

    # 3. Vendor fuzzy match (with aliases). Candidates may be empty.
    vendor_candidates = _match_vendor(vendor, searchable, fuzzy_threshold, campus_codes)
    if not vendor_candidates:
        return "unmatched", None, (), ()

    # 3b. Coding-narrow: drop vendor matches whose Dept/Acc contradict the row.
    coding_candidates = [
        c for c in vendor_candidates if _coding_compatible(c, dept, account_no)
    ]
    if not coding_candidates:
        # Vendor matched, but every match is coded to a different Dept/Acc.
        # If a vendor match STILL aligns on campus + term (the only thing
        # failing is the Dept/Acc coding), this is a "miscoded" near-miss for
        # the /miscoded tab — narrow the vendor matches by campus + date
        # (bypassing coding) to find the would-attribute-except-for-coding
        # contract(s). If NONE align on campus/term, it's a plain coding/campus
        # miss → unmatched, with the vendor hits as hints.
        #
        # EXCLUDE CapEx-account (63015) charges: those belong to the CapEx tier
        # (matched by Project ID, not vendor). Surfacing a 63015 charge whose
        # vendor happens to have an OPEX contract would invite the operator to
        # pull CapEx-project dollars into an opex contract — and double-count if
        # that same row also matched a CapEx project. The opex /miscoded tab is
        # for opex-vs-opex coding only. A 63015 charge stays "unmatched" here;
        # the CapEx tier and the cross-tier hint own the CapEx concerns.
        if account_no != settings.CAPEX_ACCOUNT_NO:
            mc_winner, mc_surviving = _narrow_and_pick(
                vendor_candidates, campus=campus, row_date=row_date,
                record_description=record_description, crosswalk=crosswalk,
            )
            miscoded_candidates = (
                (mc_winner,) if mc_winner is not None else tuple(mc_surviving)
            )
            if miscoded_candidates:
                return "miscoded", None, miscoded_candidates, tuple(vendor_candidates)
        return "unmatched", None, (), tuple(vendor_candidates)

    # 4-7. Campus + date + description + tiebreak.
    winner, surviving = _narrow_and_pick(
        coding_candidates, campus=campus, row_date=row_date,
        record_description=record_description, crosswalk=crosswalk,
    )

    if winner is not None:
        return "auto", winner.gid, tuple(surviving), tuple(coding_candidates)

    if not surviving:
        # Campus narrow zeroed the candidate list — vendor+coding matched but no
        # contract covers this campus. Surface as "unmatched" with hints.
        return "unmatched", None, (), tuple(coding_candidates)

    # surviving > 1 → ambiguous.
    return "ambiguous", None, tuple(surviving), tuple(coding_candidates)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def find_contracts_matching_ignore_rules(
    contracts: Iterable[Contract],
    ignore_rules: frozenset[tuple[str, str, str]],
    crosswalk: CampusCrosswalk,
) -> list[tuple[Contract, tuple[str, str, str]]]:
    """Read-only detector: open contracts coded to an ignore-rule triple.

    Once the engine drops a (Campus, Dept, Account No) triple's spend, any
    contract coded to that triple reads 0% forever — surface it so the operator
    can retire/recode it in Asana. A contract matches a rule when its Acc equals
    the rule account, its Dept set contains the rule dept, and its Campus options
    map (crosswalk, specific — wildcard excluded) to the rule's Tableau code.
    Returns (contract, rule) pairs, one per matched contract.

    Only LIVE contracts are flagged — an Inactive/archived contract already
    reads 0% by design, so surfacing it is noise. "Live" mirrors passes_live_gate's
    active test (section == write-gate OR status Active), minus the term window
    the detector doesn't need.
    """
    write_gate = settings.ASANA_WRITE_GATE_SECTION
    hits: list[tuple[Contract, tuple[str, str, str]]] = []
    for c in contracts:
        if not (c.section_name == write_gate or c.status == "Active"):
            continue
        for rule in ignore_rules:
            r_campus, r_dept, r_acc = rule
            if (c.acc or "") != r_acc:
                continue
            if r_dept not in _dept_set(c.dept):
                continue
            if crosswalk.contract_matches_specific(c.campus_options, r_campus):
                hits.append((c, rule))
                break  # one flag per contract is enough
    return hits


def attribute(
    df: pd.DataFrame,
    contracts: list[Contract],
    aliases: Mapping[str, list[str]],
    crosswalk: CampusCrosswalk,
    learned_mappings: Mapping[
        tuple[str, str, str, str],
        list[tuple[str, str | None, str | None, bool]],
    ],
    *,
    fuzzy_threshold: int = DEFAULT_FUZZY_THRESHOLD,
    ignore_rules: frozenset[tuple[str, str, str]] = frozenset(),
) -> AttributionRun:
    """Run attribution against an in-scope DataFrame.

    df must already be in-scope (ACCOUNTS_IN_SCOPE × DEPTS_IN_SCOPE filtered)
    and have Amount as a signed float, plus a Date column (datetime / Timestamp).

    ignore_rules: (Campus, Dept, Account No) triples to drop outright (another
    team's spend). See _attribute_row step 1b.
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

    # Known campus codes (this export's distinct Campus values) — used to safely
    # strip a 'Vendor - CAMPUS-LIST' suffix off Tableau vendor names at match
    # time without touching vendors legitimately named 'X - Something'.
    campus_codes = frozenset(c for c in (_safe_str(x) for x in pd.unique(campus_arr)) if c)

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
            campus_codes=campus_codes,
            ignore_rules=ignore_rules,
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
        any_miscoded = any(s == "miscoded" for s in row_statuses)
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
            distinct = _distinct_buckets(st)
            distinct_descriptions = _distinct_descriptions_tuple(distinct)
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

        if any_miscoded and not attributed_gids:
            # Vendor matches a live contract that aligns on campus + term;
            # only the Dept/Acct coding differs. Route to the /miscoded tab.
            # narrowed_union holds the campus+term-aligned candidate(s); the
            # operator decides "Miscoded" (attribute anyway) or "Correctly
            # coded". Beats plain "unmatched" — there IS a contract behind it.
            cands = list(st["narrowed_union"].values())
            results.append(AttributionResult(
                **base, status="miscoded",
                contract_name=None, contract_gid=None,
                candidate_names=tuple(c.name for c in cands),
                candidate_gids=tuple(c.gid for c in cands),
            ))
            continue

        if any_unmatched and not attributed_gids:
            # No rows attributed AND any unmatched → genuine unmatched.
            # Show the vendor-only matches as hints. Persist the distinct
            # descriptions too: a blank-vendor (P-Card) group's vendor is named
            # only in the line-item text, so the P-Card tab's name matcher and
            # the "Attribute to X" action need EVERY description, not just the
            # one (often unrelated) Sample Record Description.
            hints = list(st["vendor_only_union"].values())
            results.append(AttributionResult(
                **base, status="unmatched",
                contract_name=None, contract_gid=None,
                candidate_names=tuple(c.name for c in hints),
                candidate_gids=tuple(c.gid for c in hints),
                distinct_descriptions=_distinct_descriptions_tuple(
                    _distinct_buckets(st)
                ),
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
            # Status preference: "learned" if every NON-DROPPED row was
            # "learned", else "auto". Dropped (out-of-term, current-term-only)
            # rows are excluded from this vote so a pinned group whose pre-term
            # rows were dropped still reads "learned", not "auto". Mixed
            # learned+auto on the same gid → "auto".
            _voting = [s for s in row_statuses if s != "dropped"]
            group_status = "learned" if _voting and all(s == "learned" for s in _voting) else "auto"
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
