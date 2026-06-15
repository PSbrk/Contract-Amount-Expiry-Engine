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
        return render_template(
            "dashboard.html",
            rows=rows,
            alarm_count=alarm_count,
            over_count=over_count,
        )

    @app.route("/needs-tagging")
    def needs_tagging():
        rows = g.conn.execute(
            '''SELECT * FROM "Needs Tagging"
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
        )
        return render_template(
            "needs_tagging.html",
            rows=rows,
            contract_names=contract_names,
            unfilled=unfilled,
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
        ]
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
