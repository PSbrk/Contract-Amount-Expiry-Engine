"""HTTP routes for the local web UI.

Routes:
  GET  /                          Dashboard overview
  GET  /dashboard-detail/<gid>    Per-contract drill-in
  GET  /needs-tagging             List + inline edit form
  POST /needs-tagging/<id>        Save Assign Contract on one row
  GET  /vendor-aliases            CRUD list
  POST /vendor-aliases            Add
  POST /vendor-aliases/<id>       Update
  POST /vendor-aliases/<id>/delete Delete
  (same triple for /campus-map and /learned-mappings)
  GET  /state                     Read-only State table
  GET  /run-log                   Recent runs, newest first
  GET  /settings                  Read-only config + env state

All routes use the per-request flask.g.conn the app factory sets up.
No auth (localhost only). No JS framework — Pico.css + a few inline
<form> elements is enough for the operator's workflow.
"""

from __future__ import annotations

import os
import sqlite3

from flask import Flask, abort, flash, g, redirect, render_template, request, url_for

from engine import sqlite_client
from engine.attribution import normalize_lm_pattern


# ---------------------------------------------------------------------------
# Column specs for the shared admin_table.html template.
#
# Each spec describes one CRUD table: the display column names, the HTML
# form-input shape per column, and which column is the natural-key column
# whose duplicate-insert raises sqlite3.IntegrityError (so the route can
# turn that into a friendly flash message).
# ---------------------------------------------------------------------------

_ADMIN_VENDOR_ALIASES = {
    "title": "Vendor Aliases",
    "table_name": "Vendor Aliases",
    "save_endpoint": "vendor_aliases_save",
    "delete_endpoint": "vendor_aliases_delete",
    "add_endpoint": "vendor_aliases_add",
    "intro": (
        "Map an Asana contract task name to one or more Tableau Vendor "
        "spellings. Multiple aliases separated by newlines or commas."
    ),
    "columns": [
        {"name": "Contract Name", "form": "contract_name", "type": "text", "required": True},
        {"name": "Aliases", "form": "aliases", "type": "textarea"},
        {"name": "Notes", "form": "notes", "type": "textarea"},
    ],
    "unique_col": None,  # multi-row per contract allowed by design
}

_ADMIN_CAMPUS_MAP = {
    "title": "Campus Map",
    "table_name": "Campus Map",
    "save_endpoint": "campus_map_save",
    "delete_endpoint": "campus_map_delete",
    "add_endpoint": "campus_map_add",
    "intro": (
        "Override the Tableau-code → Asana-Campus-option-name "
        "crosswalk, or check Drop to exclude a code from ingestion entirely."
    ),
    "columns": [
        {"name": "Tableau Code", "form": "tableau_code", "type": "text", "required": True},
        {"name": "Asana Option Names", "form": "asana_option_names", "type": "textarea"},
        {"name": "Drop", "form": "drop", "type": "checkbox"},
        {"name": "Notes", "form": "notes", "type": "textarea"},
    ],
    "unique_col": "Tableau Code",
}

_ADMIN_LEARNED_MAPPINGS = {
    "title": "Learned Mappings",
    "table_name": "Learned Mappings",
    "save_endpoint": "learned_mappings_save",
    "delete_endpoint": "learned_mappings_delete",
    "add_endpoint": "learned_mappings_add",
    "intro": (
        "(Campus, Dept, Account No, Vendor) → Contract attribution. "
        "Normally written by promote_filled_needs_tagging after the "
        "operator answers a Needs Tagging row; hand-edit only for "
        "backfill or correction."
    ),
    "columns": [
        {"name": "Key", "form": "key", "type": "text", "required": True},
        {"name": "Campus", "form": "campus", "type": "text"},
        {"name": "Dept", "form": "dept", "type": "text"},
        {"name": "Account No", "form": "account_no", "type": "text"},
        {"name": "Vendor", "form": "vendor", "type": "text"},
        {"name": "Contract Name", "form": "contract_name", "type": "text"},
        {"name": "Learned At", "form": "learned_at", "type": "text"},
        {"name": "Notes", "form": "notes", "type": "textarea"},
    ],
    "unique_col": "Key",
}


# ---------------------------------------------------------------------------
# Vendor Conflicts: description ↔ Asana Contract Reason Text scoring
#
# Pre-selects the best-fitting Asana task in the per-description dropdown
# by token-overlap (Jaccard) between the Tableau Record Description and
# each candidate's Asana Contract Reason Text. Cuts the operator's work on
# split conflicts (e.g. Bear Claw Landscaping: snow-removal candidate vs.
# landscaping candidate — Snow/Ice descriptions auto-pick the snow task,
# Landscaping/Groundskeeping descriptions auto-pick the landscaping task).
# ---------------------------------------------------------------------------

import re as _re

# Words that carry no signal — present in almost every reason text and
# would dilute the Jaccard score. Lowercase, content-free grammar.
_TEXT_MATCH_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "and", "any", "are", "as", "at", "be", "but", "by", "for",
    "from", "has", "have", "he", "her", "his", "i", "in", "is", "it", "its",
    "of", "on", "or", "she", "that", "the", "their", "them", "they", "this",
    "to", "was", "we", "were", "will", "with", "you", "your",
    # Common engine-context noise:
    "additional", "also", "any", "applications", "amount", "amount.", "bill",
    "contract", "costs", "cover", "covers", "include", "includes", "includes.",
    "needed", "new", "operator", "operator-authored", "reversed", "service",
    "services", "submit", "task", "txn", "txns",
})

_TOKEN_RE = _re.compile(r"[A-Za-z][A-Za-z0-9]*")


def _tokens(text: str | None) -> set[str]:
    """Tokenize text for Jaccard scoring: lowercase alpha tokens of length
    ≥ 3, minus stop-words."""
    if not text:
        return set()
    return {
        t for t in (m.group(0).lower() for m in _TOKEN_RE.finditer(text))
        if len(t) >= 3 and t not in _TEXT_MATCH_STOPWORDS
    }


def _jaccard(a: set[str], b: set[str]) -> float:
    """|a∩b| / |a∪b|. 0.0 when both sides empty (no signal)."""
    if not a or not b:
        return 0.0
    inter = a & b
    union = a | b
    return len(inter) / len(union)


def _date_intervals_overlap(
    term_start: str, term_due: str, bucket_min: str, bucket_max: str,
) -> bool:
    """True iff a contract whose term is [term_start, term_due] could cover
    a description bucket spanning [bucket_min, bucket_max]. All args are ISO
    YYYY-MM-DD strings or "".

    Asymmetric, matching attribution._date_contains semantics (#6):
      - A blank term_due means OPEN-ENDED FORWARD (no upper bound) — it does
        NOT make the contract compatible with everything; a bucket entirely
        BEFORE term_start is still out of term.
      - A blank term_start means no lower bound.
      - Blank BUCKET dates (no parsable transaction dates) genuinely can't be
        judged, so they degrade to True (text-only fallback) — the operator
        still sees Jaccard's pick rather than a spurious "outside term".

    Previously this returned True whenever ANY of the four dates was blank,
    so an open-ended (blank-Due) contract was treated as date-compatible with
    every bucket — re-introducing the pre-contract-start suggestions Phase 14
    was built to stop.
    """
    # Unknown bucket dates → can't judge → fall back to text-only.
    if not bucket_min or not bucket_max:
        return True
    # No term info at all → can't judge → compatible.
    if not term_start and not term_due:
        return True
    # Lexicographic compare is valid for zero-padded ISO YYYY-MM-DD.
    if term_start and bucket_max < term_start:
        return False  # bucket entirely before the contract started
    if term_due and bucket_min > term_due:
        return False  # bucket entirely after the contract ended
    return True


def _pin_is_date_futile(conn, contract_gid: str, nt_row) -> bool:
    """True iff pinning `contract_gid` to this Needs Tagging group would
    attribute NOTHING because the contract's term covers none of the group's
    transaction dates (#8).

    Looks the candidate's [Start, Due] up in the Dashboard and tests it
    against the group's [First Date, Last Date] span via the same predicate
    the picker uses. Returns False (allow) whenever we can't judge — the
    candidate isn't in the Dashboard, or the group has no parsable dates —
    so this only ever BLOCKS a pin that is provably futile.
    """
    cand = conn.execute(
        'SELECT "Start", "Due" FROM "Dashboard" WHERE "Asana Task GID" = ?',
        (contract_gid,),
    ).fetchone()
    if cand is None:
        return False
    group_first = (nt_row["First Date"] or "").strip()
    group_last = (nt_row["Last Date"] or "").strip()
    if not group_first or not group_last:
        return False
    return not _date_intervals_overlap(
        (cand["Start"] or "").strip(), (cand["Due"] or "").strip(),
        group_first, group_last,
    )


def _suggest_candidate_per_description(
    distinct_descriptions: list[dict],
    candidates: list[dict],
    min_margin: float = 0.0001,
) -> list[dict]:
    """For each distinct description, pick the Asana candidate whose
    Contract Reason Text scores highest by Jaccard AND whose term overlaps
    the description's date range. Returns a new list of dicts with
    `suggested_gid` set (None when no clear winner survives both filters)
    and `date_compat_by_gid` -- a {gid: bool} map the template uses to
    mark options whose term doesn't fit the description's dates.

    Phase 13: date filter prevents auto-picking a contract whose term
    doesn't contain any of the description's transaction dates. Example:
    "Snow/ice management 3/2025" should NOT auto-pick a contract whose
    term starts 2025-09-30, even though Jaccard says the text matches --
    the operator would have to undo the link manually.

    A "clear winner" means the top date-compatible score > 0 AND beats the
    next date-compatible runner-up by more than min_margin. Ties stay on
    skip so the operator decides.
    """
    cand_tokens: list[set[str]] = [
        _tokens(c.get("Contract Reason Text", "")) for c in candidates
    ]
    out: list[dict] = []
    for d in distinct_descriptions:
        desc_tokens = _tokens(d.get("description", ""))
        desc_min = (d.get("min_date") or "").strip()
        desc_max = (d.get("max_date") or "").strip()
        # Per-candidate date compatibility. A candidate's term [Start, Due]
        # must overlap the description's [min_date, max_date]; missing
        # dates on either side degrade to "compatible" (text-only fallback).
        date_compat_by_gid: dict[str, bool] = {}
        for c in candidates:
            gid = c.get("Asana Task GID") or ""
            compat = _date_intervals_overlap(
                (c.get("Start") or "").strip(),
                (c.get("Due") or "").strip(),
                desc_min, desc_max,
            )
            date_compat_by_gid[gid] = compat
        # Score every candidate by text overlap, but ONLY consider
        # date-compatible ones for the suggestion. We still keep the full
        # candidate list visible in the dropdown -- operator may have
        # context (late-arriving invoice for service rendered before the
        # contract ended) that the engine doesn't.
        scores: list[tuple[float, dict]] = [
            (_jaccard(desc_tokens, ct), c)
            for ct, c in zip(cand_tokens, candidates)
            if date_compat_by_gid.get(c.get("Asana Task GID") or "", True)
        ]
        scores.sort(key=lambda s: s[0], reverse=True)
        top = scores[0] if scores else (0.0, None)
        runner = scores[1] if len(scores) > 1 else (0.0, None)
        suggested_gid = None
        if top[1] is not None and top[0] > 0 and top[0] - runner[0] > min_margin:
            suggested_gid = top[1].get("Asana Task GID")
        out.append({
            **d,
            "suggested_gid": suggested_gid,
            "date_compat_by_gid": date_compat_by_gid,
        })
    return out


def _form_kwargs(spec: dict) -> dict:
    """Pull the right kwargs out of request.form for a given admin spec.

    Returns a dict ready to splat into the matching sqlite_client helper
    (insert_* / update_*). Checkbox columns map to bool (HTML5 sends 'on'
    when checked, omits the key when not); everything else is a string,
    .strip()ed for trim-paste safety.
    """
    out: dict = {}
    for col in spec["columns"]:
        raw = request.form.get(col["form"])
        if col["type"] == "checkbox":
            out[col["form"]] = bool(raw)  # 'on' is truthy, None is falsy
        else:
            out[col["form"]] = (raw or "").strip()
    return out


def register_routes(app: Flask) -> None:
    # A secret key is required for flash() messages; for a single-user
    # localhost app there's no auth surface, so a fixed dev key is fine.
    # If we ever expose this beyond localhost, swap to a per-install
    # secret loaded from config/secrets.env.
    app.secret_key = app.config.get("SECRET_KEY") or "engine-ui-dev"

    @app.route("/")
    def dashboard():
        rows = g.conn.execute(
            '''SELECT * FROM "Dashboard"
               ORDER BY CASE "Alarms" WHEN 'ALARM' THEN 0 ELSE 1 END,
                        CASE "Spending Rate Alarm"
                          WHEN 'Over' THEN 0
                          WHEN '100%' THEN 1
                          WHEN '90%'  THEN 2
                          WHEN '75%'  THEN 3
                          ELSE 4 END,
                        COALESCE("% Spent", 0) DESC,
                        "Contract"'''
        ).fetchall()
        # Count quick stats for the page header.
        alarm_count = sum(1 for r in rows if r["Alarms"] == "ALARM")
        over_count = sum(1 for r in rows if r["Spending Rate Alarm"] == "Over")

        # Amendment-link cross-references. For each row, the template wants
        # to know:
        #   - is this row an amendment? (show "amendment of <parent>")
        #   - does this row have amendments? (show "+ amendment <name>")
        # parent_by_amendment[amendment_gid] = link dict
        # amendments_by_parent[parent_gid] = [link dict, ...]
        parent_by_amendment, amendments_by_parent = (
            sqlite_client.load_amendment_links(g.conn)
        )
        # Look up each linked partner's Dashboard row by gid for the
        # contextual $ amounts. Tasks no longer in Dashboard (closed in
        # Asana, etc.) fall back to the name+gid snapshot stored on the
        # link itself.
        dash_by_gid = {r["Asana Task GID"]: r for r in rows}
        amendment_xref: dict[str, dict] = {}
        for r in rows:
            gid = r["Asana Task GID"]
            entry = {"is_amendment_of": None, "has_amendments": []}
            parent_link = parent_by_amendment.get(gid)
            if parent_link:
                partner = dash_by_gid.get(parent_link["parent_gid"])
                entry["is_amendment_of"] = {
                    "gid": parent_link["parent_gid"],
                    "name": (partner["Contract"] if partner
                             else parent_link["parent_name"]) or "(unnamed)",
                    "contract_amount": (partner["Contract Amount"]
                                        if partner else None),
                    "spent_so_far": (partner["Spent so far"]
                                     if partner else None),
                    "in_dashboard": partner is not None,
                }
            for link in amendments_by_parent.get(gid, []):
                partner = dash_by_gid.get(link["amendment_gid"])
                entry["has_amendments"].append({
                    "gid": link["amendment_gid"],
                    "name": (partner["Contract"] if partner
                             else link["amendment_name"]) or "(unnamed)",
                    "contract_amount": (partner["Contract Amount"]
                                        if partner else None),
                    "spent_so_far": (partner["Spent so far"]
                                     if partner else None),
                    "in_dashboard": partner is not None,
                })
            if entry["is_amendment_of"] or entry["has_amendments"]:
                amendment_xref[gid] = entry

        return render_template(
            "dashboard.html",
            rows=rows,
            alarm_count=alarm_count,
            over_count=over_count,
            amendment_xref=amendment_xref,
        )

    @app.route("/needs-tagging")
    def needs_tagging():
        # ?show=
        #   open        — Dismissed=0 AND Once Off=0 (the actionable queue; default)
        #   once_off    — Once Off=1 (operator-snoozed; will resurface on new activity)
        #   dismissed   — Dismissed=1 (operator-killed; never resurfaces)
        #   all         — every row regardless of state
        show = request.args.get("show", "open").lower()
        # Phase 11: p-card rows live on /p-card-spend; never surface them
        # in Needs Tagging views (open/once_off/dismissed/all).
        p_card_filter = 'COALESCE("Is P-Card", 0) = 0'
        if show == "dismissed":
            where = f'WHERE COALESCE("Dismissed", 0) = 1 AND {p_card_filter}'
        elif show == "once_off":
            where = ('WHERE COALESCE("Once Off", 0) = 1 '
                     'AND COALESCE("Dismissed", 0) = 0 '
                     f'AND {p_card_filter}')
        elif show == "all":
            where = f'WHERE {p_card_filter}'
        else:
            where = ('WHERE COALESCE("Dismissed", 0) = 0 '
                     'AND COALESCE("Once Off", 0) = 0 '
                     f'AND {p_card_filter}')
        rows = g.conn.execute(
            f'''SELECT * FROM "Needs Tagging"
                {where}
                ORDER BY CASE WHEN "Assign Contract" IS NULL
                                   OR TRIM("Assign Contract") = ''
                              THEN 0 ELSE 1 END,
                         COALESCE("$ in group", 0) DESC,
                         "Group Key"'''
        ).fetchall()
        # The datalist of contract-name suggestions comes from the
        # Dashboard table — those are the contracts the most recent
        # --ingest run computed, so they're the canonical valid names
        # without a separate Asana call. Empty Dashboard → empty
        # datalist (operator can still type freely; validation happens
        # at promote_filled_needs_tagging time).
        contract_names = [
            r["Contract"]
            for r in g.conn.execute(
                'SELECT DISTINCT "Contract" FROM "Dashboard" '
                'WHERE "Contract" IS NOT NULL ORDER BY "Contract"'
            ).fetchall()
        ]
        unfilled = sum(
            1 for r in rows
            if not (r["Assign Contract"] or "").strip()
            and not (r["Dismissed"] or 0)
            and not (r["Once Off"] or 0)
        )
        # Per-state counts -- displayed as chips/nav so the operator can see
        # how many rows are parked in each state and switch views easily.
        # They MUST apply the same p-card filter as the views above, or the
        # badge over-counts relative to the rows the tab actually renders
        # (a dismissed/once-off p-card row would inflate the chip).
        dismissed_count = g.conn.execute(
            f'SELECT COUNT(*) FROM "Needs Tagging" '
            f'WHERE COALESCE("Dismissed", 0) = 1 AND {p_card_filter}'
        ).fetchone()[0]
        once_off_count = g.conn.execute(
            f'''SELECT COUNT(*) FROM "Needs Tagging"
               WHERE COALESCE("Once Off", 0) = 1
                 AND COALESCE("Dismissed", 0) = 0
                 AND {p_card_filter}'''
        ).fetchone()[0]
        return render_template(
            "needs_tagging.html",
            rows=rows,
            contract_names=contract_names,
            unfilled=unfilled,
            dismissed_count=dismissed_count,
            once_off_count=once_off_count,
            show=show,
        )

    @app.route("/needs-tagging/<int:record_id>", methods=["POST"])
    def needs_tagging_save(record_id: int):
        # Confirm the row exists; abort 404 if not (could happen if the
        # operator left a stale tab open and the row was cleaned up by
        # a --ingest run).
        existing = g.conn.execute(
            'SELECT 1 FROM "Needs Tagging" WHERE id = ?', (record_id,)
        ).fetchone()
        if existing is None:
            abort(404)
        contract_name = (request.form.get("assign_contract") or "").strip()
        sqlite_client.set_needs_tagging_assign_contract(
            g.conn, record_id=record_id, contract_name=contract_name,
        )
        flash(
            f"Saved. Next --ingest will promote this row to Learned Mappings."
            if contract_name
            else "Cleared Assign Contract on this row.",
            "success",
        )
        return redirect(url_for("needs_tagging"))

    @app.route("/needs-tagging/<int:record_id>/dismiss", methods=["POST"])
    def needs_tagging_dismiss(record_id: int):
        existing = g.conn.execute(
            'SELECT 1 FROM "Needs Tagging" WHERE id = ?', (record_id,)
        ).fetchone()
        if existing is None:
            abort(404)
        sqlite_client.set_needs_tagging_dismissed(
            g.conn, record_id=record_id, dismissed=True,
        )
        flash(
            "Marked as irrelevant. Engine will leave this group alone "
            "on future runs.",
            "success",
        )
        # Stay on the view the operator was looking at (open / dismissed / all).
        show = request.form.get("show", "open")
        return redirect(url_for("needs_tagging", show=show))

    @app.route("/needs-tagging/<int:record_id>/undismiss", methods=["POST"])
    def needs_tagging_undismiss(record_id: int):
        existing = g.conn.execute(
            'SELECT 1 FROM "Needs Tagging" WHERE id = ?', (record_id,)
        ).fetchone()
        if existing is None:
            abort(404)
        sqlite_client.set_needs_tagging_dismissed(
            g.conn, record_id=record_id, dismissed=False,
        )
        flash("Restored. Row is back on the open list.", "success")
        show = request.form.get("show", "dismissed")
        return redirect(url_for("needs_tagging", show=show))

    @app.route("/needs-tagging/<int:record_id>/mark-once-off", methods=["POST"])
    def needs_tagging_mark_once_off(record_id: int):
        existing = g.conn.execute(
            'SELECT 1 FROM "Needs Tagging" WHERE id = ?', (record_id,)
        ).fetchone()
        if existing is None:
            abort(404)
        sqlite_client.set_needs_tagging_once_off(
            g.conn, record_id=record_id, once_off=True,
        )
        flash(
            "Marked as once-off. The engine will hide this group until NEW "
            "transactions arrive in it (any row dated after today's Last Date).",
            "success",
        )
        show = request.form.get("show", "open")
        return redirect(url_for("needs_tagging", show=show))

    @app.route("/needs-tagging/<int:record_id>/unmark-once-off", methods=["POST"])
    def needs_tagging_unmark_once_off(record_id: int):
        existing = g.conn.execute(
            'SELECT 1 FROM "Needs Tagging" WHERE id = ?', (record_id,)
        ).fetchone()
        if existing is None:
            abort(404)
        sqlite_client.set_needs_tagging_once_off(
            g.conn, record_id=record_id, once_off=False,
        )
        flash("Restored. Row is back on the open list.", "success")
        show = request.form.get("show", "once_off")
        return redirect(url_for("needs_tagging", show=show))

    @app.route("/vendor-conflicts")
    def vendor_conflicts():
        """Specialized review panel for Needs Tagging rows where the engine
        had multiple candidate Asana tasks matching the same vendor and
        couldn't tiebreak (same name + same campus + overlapping terms).
        Operator picks a specific task gid; result is written to Learned
        Mappings with the gid pinned."""
        # Only consider open (non-dismissed) rows. Also exclude rows where
        # the operator has selected "Other" -- they declared none of the
        # engine's candidates fit, so the conflict picker is no longer
        # actionable here; the row stays on the Needs Tagging Open list
        # to be resolved via Assign Contract instead.
        rows = g.conn.execute(
            '''SELECT * FROM "Needs Tagging"
               WHERE COALESCE("Dismissed", 0) = 0
                 AND COALESCE("Conflict Other", 0) = 0
                 AND COALESCE("Is P-Card", 0) = 0
                 AND COALESCE("Engine Candidate Gids", '') != ''
               ORDER BY COALESCE("$ in group", 0) DESC, "Group Key"'''
        ).fetchall()
        # Build a candidate-by-gid lookup from the Dashboard so we can show
        # contract amount / term / campus side by side. Dashboard is the
        # canonical "what we know about each open Asana task right now"
        # surface, populated by the last --ingest run.
        dash_by_gid: dict[str, dict] = {}
        for d in g.conn.execute('SELECT * FROM "Dashboard"').fetchall():
            dash_by_gid[d["Asana Task GID"]] = dict(d)

        import json as _json
        conflicts: list[dict] = []
        for r in rows:
            cand_gids = [
                g for g in (r["Engine Candidate Gids"] or "").splitlines()
                if g.strip()
            ]
            # A conflict matters when there are 2+ candidates AND each
            # corresponds to a known open Asana task in the Dashboard.
            # Phase 14a: ALSO include single-candidate rows where the
            # engine flagged "Out Of Term" -- the operator needs the
            # per-description picker to mark the bucket as pre-dates or
            # to recognize the term needs extending in Asana. Without
            # this exception, single-candidate-out-of-term rows would be
            # stranded on Needs Tagging Open with no picker UI.
            candidates = [dash_by_gid[gid] for gid in cand_gids if gid in dash_by_gid]
            is_out_of_term = bool(r["Out Of Term"]) if "Out Of Term" in r.keys() else False
            if len(candidates) < 2 and not (is_out_of_term and len(candidates) == 1):
                continue
            # Distinct Descriptions JSON powers the per-description picker:
            # one dropdown per unique Tableau Record Description in the
            # conflict group, with row count + dollar weight so the operator
            # knows the impact of each pick.
            try:
                distinct = _json.loads(r["Distinct Descriptions JSON"] or "[]")
            except (TypeError, ValueError):
                distinct = []
            # Phase 11: score each distinct description against each
            # candidate's Asana Contract Reason Text and pre-select the
            # winner in the dropdown. Operator can still override.
            distinct = _suggest_candidate_per_description(distinct, candidates)
            # Phase 14c: precompute "does this group have any bucket where
            # ALL candidates are date-incompatible?". The bulk button shows
            # only when there's something to bulk-mark. Kept in the route
            # rather than the template because Jinja {% set %} inside
            # {% for %} doesn't propagate out of the loop scope.
            has_all_out_of_term_bucket = any(
                d.get("date_compat_by_gid")
                and not any(d["date_compat_by_gid"].values())
                for d in distinct
            )
            conflicts.append({
                "nt": dict(r),
                "candidates": candidates,
                "distinct_descriptions": distinct,
                "has_all_out_of_term_bucket": has_all_out_of_term_bucket,
            })

        return render_template(
            "vendor_conflicts.html",
            conflicts=conflicts,
        )

    @app.route("/vendor-conflicts/<int:record_id>/assign", methods=["POST"])
    def vendor_conflicts_assign(record_id: int):
        """Operator picked a specific Asana task gid for this conflict. Write
        a Learned Mapping with the gid pinned and delete the Needs Tagging
        row. The next --ingest run will re-attribute this group's
        transactions directly to the chosen task."""
        nt = g.conn.execute(
            'SELECT * FROM "Needs Tagging" WHERE id = ?', (record_id,)
        ).fetchone()
        if nt is None:
            abort(404)
        contract_gid = (request.form.get("contract_gid") or "").strip()
        contract_name = (request.form.get("contract_name") or "").strip()
        if not contract_gid or not contract_name:
            flash("Missing contract gid or name — could not record decision.",
                  "error")
            return redirect(url_for("vendor_conflicts"))
        # Verify the gid is one of the engine's candidate gids for this row,
        # to prevent the form from being used to inject an arbitrary gid.
        cand_gids = {
            g.strip()
            for g in (nt["Engine Candidate Gids"] or "").splitlines()
            if g.strip()
        }
        if contract_gid not in cand_gids:
            flash(
                f"GID {contract_gid} is not one of this row's engine "
                f"candidates; refusing to record.",
                "error",
            )
            return redirect(url_for("vendor_conflicts"))

        group_key = (nt["Group Key"] or "").strip()
        campus = (nt["Campus"] or "").strip()
        dept = (nt["Dept"] or "").strip()
        account_no = (nt["Account No"] or "").strip()
        vendor = (nt["Vendor"] or "").strip()
        from datetime import datetime, timezone
        today_iso = datetime.now(timezone.utc).date().isoformat()

        # #8: refuse a date-FUTILE pin. If the chosen contract's term covers
        # NONE of this group's transaction dates, attribution's pinned-gid
        # date guard rejects every row and the conflict re-surfaces every
        # ingest forever (an unresolvable loop whose success message lied).
        # Block it with actionable guidance rather than writing a doomed LM.
        if _pin_is_date_futile(g.conn, contract_gid, nt):
            flash(
                f"{contract_name}'s term does not cover any of this group's "
                f"transaction dates "
                f"({(nt['First Date'] or '?')} – {(nt['Last Date'] or '?')}), "
                f"so pinning it would never attribute these rows. Extend the "
                f"contract's term in Asana, or use the per-description "
                f"“Unassigned – Pre-dates Asana Record” option if "
                f"the spend genuinely pre-dates the contract.",
                "error",
            )
            return redirect(url_for("vendor_conflicts"))

        notes_text = (
            f"Pinned via Vendor Conflicts on {today_iso}. "
            f"Operator selected gid {contract_gid} ({contract_name})."
        )
        sqlite_client.upsert_plain_learned_mapping(
            g.conn,
            key=group_key,
            campus=campus, dept=dept, account_no=account_no, vendor=vendor,
            contract_name=contract_name, contract_gid=contract_gid,
            learned_at=today_iso, notes=notes_text, commit=False,
        )
        g.conn.execute('DELETE FROM "Needs Tagging" WHERE id = ?', (record_id,))
        g.conn.commit()
        flash(
            f"Pinned {vendor} ({campus}) to {contract_name}. "
            f"Future runs will attribute this group directly to that task.",
            "success",
        )
        return redirect(url_for("vendor_conflicts"))

    @app.route("/vendor-conflicts/<int:record_id>/mark-amendment",
               methods=["POST"])
    def vendor_conflicts_mark_amendment(record_id: int):
        """Operator declares: amendment_gid is an amendment of parent_gid.

        Two things happen in one transaction:
          1. Insert/upsert into Amendment Links so the Dashboard renders
             the cross-reference between the two tasks on every future
             render.
          2. Pin this conflict's group to the parent_gid via a plain LM,
             so the engine routes transactions to the parent on the next
             --ingest (otherwise the conflict stays unresolved and the
             group lands back in Needs Tagging).

        Both gids must be in this row's Engine Candidate Gids set
        (anti-tampering, same check as /assign).
        """
        nt = g.conn.execute(
            'SELECT * FROM "Needs Tagging" WHERE id = ?', (record_id,)
        ).fetchone()
        if nt is None:
            abort(404)

        parent_gid = (request.form.get("parent_gid") or "").strip()
        amendment_gid = (request.form.get("amendment_gid") or "").strip()
        parent_name = (request.form.get("parent_name") or "").strip()
        amendment_name = (request.form.get("amendment_name") or "").strip()
        if not parent_gid or not amendment_gid:
            flash("Pick BOTH a parent and an amendment task.", "error")
            return redirect(url_for("vendor_conflicts"))
        if parent_gid == amendment_gid:
            flash("Parent and amendment must be DIFFERENT tasks.", "error")
            return redirect(url_for("vendor_conflicts"))

        cand_gids = {
            g.strip()
            for g in (nt["Engine Candidate Gids"] or "").splitlines()
            if g.strip()
        }
        if parent_gid not in cand_gids or amendment_gid not in cand_gids:
            flash(
                "Parent or amendment gid is not one of this row's engine "
                "candidates; refusing to record.",
                "error",
            )
            return redirect(url_for("vendor_conflicts"))

        from datetime import datetime, timezone
        today_iso = datetime.now(timezone.utc).date().isoformat()

        # #8: refuse a date-futile parent pin. Routing the group to a parent
        # whose term covers none of the group's transaction dates would loop
        # forever (attribution rejects the pin on date, the conflict
        # re-surfaces). Block before writing the link OR the LM so the
        # operator gets honest guidance instead of a lying success message.
        if _pin_is_date_futile(g.conn, parent_gid, nt):
            flash(
                f"{parent_name!r}'s term does not cover this group's "
                f"transaction dates "
                f"({(nt['First Date'] or '?')} – {(nt['Last Date'] or '?')}), "
                f"so routing these transactions to it would never attribute "
                f"them. Extend the parent's term in Asana first.",
                "error",
            )
            return redirect(url_for("vendor_conflicts"))

        # 1. Record the link. Stores names too so the Dashboard render is
        #    self-sufficient even if either task disappears from Dashboard
        #    later (closed in Asana, etc.).
        sqlite_client.insert_amendment_link(
            g.conn,
            parent_gid=parent_gid,
            amendment_gid=amendment_gid,
            parent_name=parent_name,
            amendment_name=amendment_name,
            linked_at=today_iso,
            notes=(
                f"Linked via Vendor Conflicts on {today_iso} "
                f"(group_key={nt['Group Key']!r})."
            ),
        )

        # 2. Pin this conflict's group to the parent via the shared plain-LM
        #    helper (no Description Pattern). Without this, the next --ingest
        #    would re-detect the conflict and stage it again.
        group_key = (nt["Group Key"] or "").strip()
        campus = (nt["Campus"] or "").strip()
        dept = (nt["Dept"] or "").strip()
        account_no = (nt["Account No"] or "").strip()
        vendor = (nt["Vendor"] or "").strip()
        notes_text = (
            f"Pinned via Vendor Conflicts on {today_iso}. "
            f"Operator declared amendment relationship "
            f"({amendment_name!r} -> {parent_name!r}); transactions route "
            f"to parent gid {parent_gid}."
        )
        sqlite_client.upsert_plain_learned_mapping(
            g.conn,
            key=group_key,
            campus=campus, dept=dept, account_no=account_no, vendor=vendor,
            contract_name=parent_name, contract_gid=parent_gid,
            learned_at=today_iso, notes=notes_text, commit=False,
        )
        g.conn.execute('DELETE FROM "Needs Tagging" WHERE id = ?', (record_id,))
        g.conn.commit()
        flash(
            f"Linked {amendment_name!r} as an amendment of {parent_name!r}; "
            f"transactions for {vendor} ({campus}) will now route to the "
            f"parent. Dashboard will show the cross-reference on next render.",
            "success",
        )
        return redirect(url_for("vendor_conflicts"))

    @app.route("/vendor-conflicts/<int:record_id>/mark-other",
               methods=["POST"])
    def vendor_conflicts_mark_other(record_id: int):
        """Operator declares: none of the engine's vendor candidates is the
        right home for this group (e.g. payment is for TUL but every
        candidate is for CEN/BAO/OKC). Setting Conflict Other = 1 hides the
        row from Vendor Conflicts while keeping it on the Open Needs
        Tagging list, where the operator resolves it via Assign Contract."""
        existing = g.conn.execute(
            'SELECT 1 FROM "Needs Tagging" WHERE id = ?', (record_id,)
        ).fetchone()
        if existing is None:
            abort(404)
        sqlite_client.set_needs_tagging_conflict_other(
            g.conn, record_id=record_id, conflict_other=True,
        )
        flash(
            "Marked Other. Row stays in Needs Tagging — open it from the "
            "Needs Tagging tab and use Assign Contract to route it to the "
            "right Asana task.",
            "success",
        )
        return redirect(url_for("vendor_conflicts"))

    @app.route("/p-card-spend")
    def p_card_spend():
        """Read-only audit surface for purchasing-card / journal transactions
        that don't carry a Vendor field. These rows are NOT contracted spend
        and have no Asana attribution target -- they were classified by the
        engine's is_p_card_row predicate on the last upsert. Shown here so
        the operator can eyeball them and (optionally) "Ignore once" the
        ones they've reviewed."""
        show = request.args.get("show", "open").lower()
        if show == "ignored":
            where = ('WHERE COALESCE("Is P-Card", 0) = 1 '
                     'AND COALESCE("P-Card Ignored", 0) = 1')
        elif show == "all":
            where = 'WHERE COALESCE("Is P-Card", 0) = 1'
        else:
            show = "open"
            where = ('WHERE COALESCE("Is P-Card", 0) = 1 '
                     'AND COALESCE("P-Card Ignored", 0) = 0')
        rows = g.conn.execute(
            f'''SELECT * FROM "Needs Tagging"
                {where}
                ORDER BY COALESCE("$ in group", 0) DESC, "Group Key"'''
        ).fetchall()
        # Totals + counts by tab so the operator can see at-a-glance how
        # much p-card exposure is in scope and how many rows they've
        # already eyeballed (Ignored).
        open_count = g.conn.execute(
            '''SELECT COUNT(*) FROM "Needs Tagging"
               WHERE COALESCE("Is P-Card", 0) = 1
                 AND COALESCE("P-Card Ignored", 0) = 0'''
        ).fetchone()[0]
        ignored_count = g.conn.execute(
            '''SELECT COUNT(*) FROM "Needs Tagging"
               WHERE COALESCE("Is P-Card", 0) = 1
                 AND COALESCE("P-Card Ignored", 0) = 1'''
        ).fetchone()[0]
        visible_total = sum((r["$ in group"] or 0) for r in rows)
        return render_template(
            "p_card_spend.html",
            rows=rows,
            show=show,
            open_count=open_count,
            ignored_count=ignored_count,
            visible_total=visible_total,
        )

    @app.route("/p-card-spend/<int:record_id>/ignore-once", methods=["POST"])
    def p_card_spend_ignore_once(record_id: int):
        existing = g.conn.execute(
            'SELECT 1 FROM "Needs Tagging" WHERE id = ?', (record_id,)
        ).fetchone()
        if existing is None:
            abort(404)
        sqlite_client.set_needs_tagging_p_card_ignored(
            g.conn, record_id=record_id, p_card_ignored=True,
        )
        flash(
            "Ignored. Row moved to the Ignored tab; restore from there if "
            "you change your mind.",
            "success",
        )
        return redirect(url_for("p_card_spend", show="open"))

    @app.route("/p-card-spend/<int:record_id>/restore", methods=["POST"])
    def p_card_spend_restore(record_id: int):
        existing = g.conn.execute(
            'SELECT 1 FROM "Needs Tagging" WHERE id = ?', (record_id,)
        ).fetchone()
        if existing is None:
            abort(404)
        sqlite_client.set_needs_tagging_p_card_ignored(
            g.conn, record_id=record_id, p_card_ignored=False,
        )
        flash("Restored to the active P-Card list.", "success")
        return redirect(url_for("p_card_spend", show="ignored"))

    @app.route("/needs-tagging/<int:record_id>/unmark-conflict-other",
               methods=["POST"])
    def needs_tagging_unmark_conflict_other(record_id: int):
        """Undo the operator's "Other" pick — the row reappears in Vendor
        Conflicts if it still has Engine Candidate Gids."""
        existing = g.conn.execute(
            'SELECT 1 FROM "Needs Tagging" WHERE id = ?', (record_id,)
        ).fetchone()
        if existing is None:
            abort(404)
        sqlite_client.set_needs_tagging_conflict_other(
            g.conn, record_id=record_id, conflict_other=False,
        )
        flash(
            "Restored to Vendor Conflicts review (if engine candidates "
            "still apply).",
            "success",
        )
        show = request.form.get("show", "open")
        return redirect(url_for("needs_tagging", show=show))

    @app.route("/vendor-conflicts/<int:record_id>/mark-all-out-of-term-as-pre-dates",
               methods=["POST"])
    def vendor_conflicts_mark_all_out_of_term_as_pre_dates(record_id: int):
        """Phase 14c: bulk-mark every bucket whose ALL candidates are
        date-incompatible as 'Pre-dates Asana Record'. Saves the operator
        from clicking ~300 individual dropdowns when an attribution-rule
        change suddenly surfaces a large batch of out-of-term groups.

        Per-bucket logic: a bucket is bulk-marked iff EVERY candidate's
        [Start, Due] term excludes the bucket's [min_date, max_date].
        Buckets where at least one candidate still fits the date stay
        visible (operator must still decide which contract or pre-dates
        for each of those).

        Same one-shot semantics as the single-bucket Pre-dates option:
        no Learned Mapping is written, so the buckets re-surface on the
        next ingest if the raw transactions are still out of term.
        """
        nt = g.conn.execute(
            'SELECT * FROM "Needs Tagging" WHERE id = ?', (record_id,)
        ).fetchone()
        if nt is None:
            abort(404)

        # Resolve candidate dates by reading the same Dashboard rows the
        # GET route uses, then apply the same _date_intervals_overlap test.
        cand_gids = [
            x.strip()
            for x in (nt["Engine Candidate Gids"] or "").splitlines()
            if x.strip()
        ]
        dash_rows = g.conn.execute(
            f'''SELECT "Asana Task GID", "Start", "Due"
                FROM "Dashboard"
                WHERE "Asana Task GID" IN ({",".join("?" * len(cand_gids))})''',
            cand_gids,
        ).fetchall() if cand_gids else []
        cand_terms = {
            d["Asana Task GID"]: (
                (d["Start"] or "").strip(),
                (d["Due"] or "").strip(),
            )
            for d in dash_rows
        }

        import json as _json
        try:
            buckets = _json.loads(nt["Distinct Descriptions JSON"] or "[]")
        except (TypeError, ValueError):
            buckets = []

        keep: list[dict] = []
        stripped = 0
        stripped_amount = 0.0
        for b in buckets:
            if not isinstance(b, dict):
                keep.append(b)
                continue
            desc_min = (b.get("min_date") or "").strip()
            desc_max = (b.get("max_date") or "").strip()
            if not desc_min or not desc_max:
                # Can't judge -- keep the bucket. Operator handles manually.
                keep.append(b)
                continue
            # Bucket is "all out of term" iff every DASHBOARD-PRESENT
            # candidate fails the date. #11: iterate cand_terms (the gids
            # actually found in the Dashboard) — NOT the raw cand_gids —
            # so this matches exactly the candidate set the GET route used
            # to decide the bulk button's visibility. Previously a gid
            # missing from the Dashboard defaulted to a blank term that read
            # as "compatible", so the button could appear yet strip nothing.
            present_terms = list(cand_terms.values())
            all_out = bool(present_terms) and not any(
                _date_intervals_overlap(start, due, desc_min, desc_max)
                for start, due in present_terms
            )
            if all_out:
                stripped += 1
                try:
                    stripped_amount += float(b.get("amount") or 0)
                except (TypeError, ValueError):
                    pass
            else:
                keep.append(b)

        if stripped == 0:
            flash(
                "No out-of-term buckets to mark on this row.",
                "error",
            )
            return redirect(url_for("vendor_conflicts"))

        g.conn.execute(
            'UPDATE "Needs Tagging" SET "Distinct Descriptions JSON" = ? '
            'WHERE id = ?',
            (_json.dumps(keep), record_id),
        )
        g.conn.commit()
        flash(
            f"Marked {stripped} out-of-term bucket(s) (${stripped_amount:,.2f}) "
            f"as pre-dating Asana for {(nt['Vendor'] or '').strip()}. "
            f"They will re-surface on the next ingest.",
            "success",
        )
        return redirect(url_for("vendor_conflicts"))

    @app.route("/vendor-conflicts/<int:record_id>/assign-by-description",
               methods=["POST"])
    def vendor_conflicts_assign_by_description(record_id: int):
        """Operator picked a SPECIFIC ASANA TASK FOR EACH DISTINCT
        DESCRIPTION in a conflicted group. Used when the engine can't
        auto-resolve and the operator wants to split the group's line items
        across multiple Asana tasks by description (e.g. 'Groundskeeping'
        rows → landscaping task; 'Snow Removal' rows → snow task).

        Form fields:
          desc_<i>          — the Tableau Record Description (or '' for blank)
          gid_<i>           — the chosen Asana task GID
          name_<i>          — the chosen Asana task name (for the LM Contract
                              Name column)

        Behavior:
          - For each (desc_i, gid_i, name_i) where gid_i is non-empty:
              create a pattern-bearing Learned Mapping (Description Pattern
              = the literal description) pointing to that task.
          - The PLAIN group-level LM for this Key (if any) is NOT touched —
              it remains the catch-all for descriptions the operator didn't
              pick for. If no plain LM exists, future rows with unrecognized
              descriptions stay ambiguous (which is the desired behavior;
              the operator can come back and pick them).
          - The Needs Tagging row is deleted ONLY when at least one
              description was assigned; otherwise it's left in place.
        """
        nt = g.conn.execute(
            'SELECT * FROM "Needs Tagging" WHERE id = ?', (record_id,)
        ).fetchone()
        if nt is None:
            abort(404)

        # Engine-curated set of valid candidate gids — anything else is
        # rejected (same anti-tampering check as /assign).
        cand_gids = {
            g.strip()
            for g in (nt["Engine Candidate Gids"] or "").splitlines()
            if g.strip()
        }

        # Phase 14b: sentinel for "Unassigned - Pre-dates Asana Record".
        # Marks a description as one-shot resolved for this ingest without
        # writing a Learned Mapping. Bucket disappears from the picker
        # until next ingest re-attributes from the raw transactions.
        PRE_DATES_SENTINEL = "__PRE_DATES__"

        # Collect (description, gid, name) triples from the form. We allow
        # arbitrary index width — operator can submit any subset of the
        # picker rows. Pre-dates picks are collected separately because they
        # bypass the Learned Mapping write.
        picks: list[tuple[str, str, str]] = []
        pre_dates_descs: list[str] = []
        skipped_invalid = 0
        for key_name in request.form.keys():
            if not key_name.startswith("desc_"):
                continue
            idx = key_name[len("desc_"):]
            description = (request.form.get(f"desc_{idx}") or "").strip()
            chosen_gid = (request.form.get(f"gid_{idx}") or "").strip()
            chosen_name = (request.form.get(f"name_{idx}") or "").strip()
            if not chosen_gid:
                continue  # operator left this description unassigned
            if chosen_gid == PRE_DATES_SENTINEL:
                pre_dates_descs.append(description)
                continue
            if chosen_gid not in cand_gids:
                skipped_invalid += 1
                continue
            picks.append((description, chosen_gid, chosen_name))

        if not picks and not pre_dates_descs:
            flash(
                "No descriptions were assigned. Pick at least one description "
                "before submitting.",
                "error",
            )
            return redirect(url_for("vendor_conflicts"))

        group_key = (nt["Group Key"] or "").strip()
        campus = (nt["Campus"] or "").strip()
        dept = (nt["Dept"] or "").strip()
        account_no = (nt["Account No"] or "").strip()
        vendor = (nt["Vendor"] or "").strip()
        from datetime import datetime, timezone
        today_iso = datetime.now(timezone.utc).date().isoformat()

        for description, chosen_gid, chosen_name in picks:
            # #7: store a NORMALIZED stem as the Description Pattern, not the
            # raw line item. The literal description carries volatile invoice
            # numbers / dates that never recur verbatim, so a literal pattern
            # silently stopped matching and the bucket re-surfaced every
            # ingest. normalize_lm_pattern strips the volatile tokens; the
            # attribution matcher normalizes both sides and checks token
            # subset, so the pattern keeps applying month to month.
            pattern = normalize_lm_pattern(description)
            notes_text = (
                f"Pattern LM pinned via Vendor Conflicts on {today_iso}. "
                f"Description {description!r} (pattern {pattern!r}) "
                f"→ gid {chosen_gid} ({chosen_name})."
            )
            # Composite identity for pattern LMs: same (Key, Description
            # Pattern). Upsert in a single statement so re-saving with the
            # same pattern overwrites instead of duplicating.
            existing = g.conn.execute(
                '''SELECT id FROM "Learned Mappings"
                   WHERE "Key" = ?
                     AND COALESCE("Description Pattern", '') = ?''',
                (group_key, pattern),
            ).fetchone()
            if existing:
                g.conn.execute(
                    '''UPDATE "Learned Mappings"
                       SET "Campus" = ?, "Dept" = ?, "Account No" = ?,
                           "Vendor" = ?, "Contract Name" = ?,
                           "Contract Gid" = ?, "Description Pattern" = ?,
                           "Learned At" = ?, "Notes" = ?
                       WHERE id = ?''',
                    (campus, dept, account_no, vendor, chosen_name,
                     chosen_gid, pattern, today_iso, notes_text,
                     existing["id"]),
                )
            else:
                g.conn.execute(
                    '''INSERT INTO "Learned Mappings"
                         ("Key", "Campus", "Dept", "Account No", "Vendor",
                          "Contract Name", "Contract Gid",
                          "Description Pattern", "Learned At", "Notes")
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (group_key, campus, dept, account_no, vendor, chosen_name,
                     chosen_gid, pattern, today_iso, notes_text),
                )

        # Phase 14b: handle Needs Tagging row disposition.
        # - If the operator wrote at least one real LM (picks): delete the
        #   row entirely. Next --ingest rebuilds it only if any rows still
        #   attribute ambiguously after the LMs are applied. Pre-dates
        #   buckets re-surface naturally on next ingest because we did
        #   NOT write an LM for them (one-shot per the operator's setting).
        # - If the operator ONLY marked pre-dates (no real picks): strip
        #   those buckets out of the row's Distinct Descriptions JSON so
        #   they disappear from the picker for this ingest. The row stays
        #   visible if any other buckets remain; if every bucket was
        #   marked pre-dates the row drops out of the picker (no buckets
        #   left to render). Either way no LM is written.
        import json as _json
        if picks:
            g.conn.execute('DELETE FROM "Needs Tagging" WHERE id = ?', (record_id,))
        elif pre_dates_descs:
            try:
                buckets = _json.loads(nt["Distinct Descriptions JSON"] or "[]")
            except (TypeError, ValueError):
                buckets = []
            keep = [b for b in buckets
                    if (b.get("description") or "").strip() not in pre_dates_descs]
            g.conn.execute(
                'UPDATE "Needs Tagging" SET "Distinct Descriptions JSON" = ? '
                'WHERE id = ?',
                (_json.dumps(keep), record_id),
            )
        g.conn.commit()

        parts = []
        if picks:
            parts.append(f"saved {len(picks)} description pattern(s)")
        if pre_dates_descs:
            parts.append(
                f"marked {len(pre_dates_descs)} as pre-dating Asana "
                f"(re-surfaces next ingest)"
            )
        msg = f"{', '.join(parts)} for {vendor} ({campus})."
        if picks:
            msg += " Run --ingest again to apply."
        if skipped_invalid:
            msg += f" ({skipped_invalid} pick(s) had invalid gids and were ignored.)"
        flash(msg, "success")
        return redirect(url_for("vendor_conflicts"))

    @app.route("/run-log")
    def run_log():
        offset = max(0, int(request.args.get("offset", 0)))
        per_page = 50
        rows = g.conn.execute(
            '''SELECT * FROM "Run Log"
               ORDER BY "Run ID" DESC, id DESC
               LIMIT ? OFFSET ?''',
            (per_page, offset),
        ).fetchall()
        total = g.conn.execute(
            'SELECT COUNT(*) AS c FROM "Run Log"'
        ).fetchone()["c"]
        return render_template(
            "run_log.html",
            rows=rows,
            offset=offset,
            per_page=per_page,
            total=total,
        )

    # ------------------------------------------------------------------
    # Drill-in: /dashboard-detail/<gid>
    # ------------------------------------------------------------------

    @app.route("/dashboard-detail/<gid>")
    def dashboard_detail(gid: str):
        row = g.conn.execute(
            'SELECT * FROM "Dashboard" WHERE "Asana Task GID" = ?', (gid,)
        ).fetchone()
        if row is None:
            abort(404)
        # Show the learned mappings + vendor aliases that feed THIS
        # contract's transactions. Joined by the human-readable contract
        # name (the Asana task name) since that's the cross-table key.
        contract_name = row["Contract"]
        learned = g.conn.execute(
            'SELECT * FROM "Learned Mappings" WHERE "Contract Name" = ?',
            (contract_name,),
        ).fetchall()
        aliases = g.conn.execute(
            'SELECT * FROM "Vendor Aliases" WHERE "Contract Name" = ?',
            (contract_name,),
        ).fetchall()
        # State prior (if any) — useful for "what did the engine see
        # last time" while debugging.
        state_prior = g.conn.execute(
            'SELECT * FROM "State" WHERE "Asana Task GID" = ?', (gid,)
        ).fetchone()
        return render_template(
            "dashboard_detail.html",
            row=row,
            learned=learned,
            aliases=aliases,
            state_prior=state_prior,
        )

    # ------------------------------------------------------------------
    # Admin tables — shared admin_table.html template, three triples of
    # (list-GET, add-POST, update-POST, delete-POST). The
    # _register_admin_routes helper factors out the boilerplate.
    # ------------------------------------------------------------------

    _register_admin_routes(app, _ADMIN_VENDOR_ALIASES, "vendor_aliases",
                           "/vendor-aliases",
                           insert=sqlite_client.insert_vendor_alias,
                           update=sqlite_client.update_vendor_alias,
                           delete=sqlite_client.delete_vendor_alias)
    _register_admin_routes(app, _ADMIN_CAMPUS_MAP, "campus_map",
                           "/campus-map",
                           insert=sqlite_client.insert_campus_map,
                           update=sqlite_client.update_campus_map,
                           delete=sqlite_client.delete_campus_map)
    _register_admin_routes(app, _ADMIN_LEARNED_MAPPINGS, "learned_mappings",
                           "/learned-mappings",
                           insert=sqlite_client.insert_learned_mapping,
                           update=sqlite_client.update_learned_mapping,
                           delete=sqlite_client.delete_learned_mapping)

    # ------------------------------------------------------------------
    # /state — read-only audit view of the State table
    # ------------------------------------------------------------------

    @app.route("/state")
    def state_view():
        rows = g.conn.execute(
            'SELECT * FROM "State" ORDER BY "Contract Name"'
        ).fetchall()
        return render_template("state.html", rows=rows)

    # ------------------------------------------------------------------
    # /settings — read-only display of config.settings.* + env state
    # ------------------------------------------------------------------

    @app.route("/settings")
    def settings_view():
        from config import settings as cfg

        # Whitelist of public settings to display. NOT the full module
        # dict — that would leak internal _PRIVATE constants and module
        # aliases. Listed in the same logical grouping the settings.py
        # source uses so the page reads cleanly.
        groups = [
            ("Asana — read-only IDs", [
                ("ASANA_WORKSPACE_GID", cfg.ASANA_WORKSPACE_GID),
                ("ASANA_PROJECT_GID", cfg.ASANA_PROJECT_GID),
                ("ASANA_WRITE_GATE_SECTION", cfg.ASANA_WRITE_GATE_SECTION),
            ]),
            ("Tableau ingestion scope", [
                ("ACCOUNTS_IN_SCOPE", sorted(cfg.ACCOUNTS_IN_SCOPE)),
                ("DEPTS_IN_SCOPE", sorted(cfg.DEPTS_IN_SCOPE)),
            ]),
            ("Per-contract compute", [
                ("DEFAULT_TERM_MONTHS", cfg.DEFAULT_TERM_MONTHS),
                ("PACE_GUARD_DAYS", cfg.PACE_GUARD_DAYS),
                ("RUNAWAY_PACE", cfg.RUNAWAY_PACE),
                ("MIN_SPEND_FLOOR", cfg.MIN_SPEND_FLOOR),
                ("REVIEW_LARGE_DELTA_DOLLARS", cfg.REVIEW_LARGE_DELTA_DOLLARS),
            ]),
            ("Run Log retention", [
                ("RUN_LOG_RETENTION_DAYS", cfg.RUN_LOG_RETENTION_DAYS),
            ]),
            ("Run-mode env overrides", [
                ("DRY_RUN_ASANA", cfg.DRY_RUN_ASANA),
                ("WRITE_TEST_CONTRACT", cfg.WRITE_TEST_CONTRACT),
                ("TRANSACTION_SOURCE", cfg.TRANSACTION_SOURCE),
            ]),
            ("OneDrive sync (operator memory)", [
                ("ONEDRIVE_BACKUP_PATH", cfg.ONEDRIVE_BACKUP_PATH or "(unset — engine.db lives only on this machine)"),
            ]),
        ]
        # Phase 12: render the sync state from the most recent
        # _restore_database_safely() call. The /settings page is the
        # operator-facing dashboard for "is my memory in the cloud?".
        try:
            from engine.main import _LAST_RESTORE_RESULT
            sync_state = _LAST_RESTORE_RESULT
        except (ImportError, AttributeError):
            sync_state = None
        # Env-var presence check — never the value (some are secrets).
        # 'present' just means "the env var is set", not whether it's
        # valid for use.
        env_state = [
            (name, name in os.environ and os.environ[name].strip() != "")
            for name in (
                "ASANA_PAT",
                "AIRTABLE_PAT",
                "AIRTABLE_BASE_ID",
                "ENGINE_DB_PATH",
                "TABLEAU_PAT_NAME",
                "TABLEAU_PAT_SECRET",
                "TABLEAU_VIEW_ID",
            )
        ]
        return render_template(
            "settings.html",
            groups=groups,
            env_state=env_state,
            sync_state=sync_state,
        )


def _register_admin_routes(app, spec, endpoint_prefix, url_prefix, *,
                            insert, update, delete):
    """Register one CRUD triple (list / add / update / delete) for an
    admin table. Endpoint names: <prefix>_list, <prefix>_add,
    <prefix>_save, <prefix>_delete."""

    @app.route(url_prefix, endpoint=f"{endpoint_prefix}_list")
    def _list():
        rows = g.conn.execute(
            f'SELECT * FROM "{spec["table_name"]}" ORDER BY id'
        ).fetchall()
        return render_template("admin_table.html", spec=spec, rows=rows)

    @app.route(url_prefix, methods=["POST"], endpoint=f"{endpoint_prefix}_add")
    def _add():
        kwargs = _form_kwargs(spec)
        try:
            insert(g.conn, **kwargs)
            flash(f"Added row to {spec['title']}.", "success")
        except sqlite3.IntegrityError as exc:
            flash(
                f"Could not add: {exc}. "
                f"({spec['unique_col']!r} must be unique on this table.)"
                if spec["unique_col"]
                else f"Could not add: {exc}.",
                "error",
            )
        return redirect(url_for(f"{endpoint_prefix}_list"))

    @app.route(f"{url_prefix}/<int:record_id>", methods=["POST"],
               endpoint=f"{endpoint_prefix}_save")
    def _save(record_id: int):
        existing = g.conn.execute(
            f'SELECT 1 FROM "{spec["table_name"]}" WHERE id = ?', (record_id,)
        ).fetchone()
        if existing is None:
            abort(404)
        kwargs = _form_kwargs(spec)
        try:
            update(g.conn, record_id=record_id, **kwargs)
            flash(f"Saved row {record_id}.", "success")
        except sqlite3.IntegrityError as exc:
            flash(f"Could not save: {exc}.", "error")
        return redirect(url_for(f"{endpoint_prefix}_list"))

    @app.route(f"{url_prefix}/<int:record_id>/delete", methods=["POST"],
               endpoint=f"{endpoint_prefix}_delete")
    def _delete(record_id: int):
        existing = g.conn.execute(
            f'SELECT 1 FROM "{spec["table_name"]}" WHERE id = ?', (record_id,)
        ).fetchone()
        if existing is None:
            abort(404)
        delete(g.conn, record_id=record_id)
        flash(f"Deleted row {record_id}.", "success")
        return redirect(url_for(f"{endpoint_prefix}_list"))
