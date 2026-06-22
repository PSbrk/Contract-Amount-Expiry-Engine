"""Local web UI for the Contract Amount Expiry Engine.

Launched via `python -m engine.main --ui` (or `EngineApp.exe --ui` once
packaged), the Flask app binds to localhost:8080 and lets the operator
edit Needs Tagging rows, browse the Dashboard, and inspect the Run Log
without leaving the browser. No auth — single-user single-machine
context (the engine's whole point is that it runs entirely on the
operator's laptop / VM).

App factory pattern:
- create_app(db_path=...) — production. Per-request SQLite connection.
- create_app(conn=...) — tests. One in-memory connection shared across
  requests (sqlite3 is not thread-safe by default, so we set
  check_same_thread=False on test connections, but Flask's test_client
  runs requests synchronously anyway).

Styling is Pico.css served via CDN — table-heavy, no JS framework, fast.
The base.html template is the single place to swap CSS / nav.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from flask import Flask, g, request

from engine import sqlite_client


def create_app(
    *,
    db_path: str | Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> Flask:
    """Construct a Flask app bound to the engine's SQLite store.

    Exactly one of (db_path, conn) is used. When db_path is given
    (production), each request opens a fresh per-request connection
    stored on flask.g.conn and closes it on teardown. When conn is
    given (tests), the same connection is reused for every request.

    If both are None, defaults to sqlite_client.DEFAULT_DB_PATH.
    """
    if db_path is None and conn is None:
        db_path = sqlite_client.DEFAULT_DB_PATH

    app = Flask(__name__)
    # Store strategy on the app so before_request can dispatch.
    app.config["ENGINE_DB_PATH"] = str(db_path) if db_path else None
    app.config["ENGINE_INJECTED_CONN"] = conn

    @app.before_request
    def _open_conn() -> None:
        injected = app.config["ENGINE_INJECTED_CONN"]
        if injected is not None:
            g.conn = injected
            g.conn_owned = False
        else:
            c = sqlite3.connect(app.config["ENGINE_DB_PATH"])
            c.row_factory = sqlite3.Row
            g.conn = c
            g.conn_owned = True

    @app.teardown_request
    def _close_conn(_exc) -> None:
        if getattr(g, "conn_owned", False) and getattr(g, "conn", None) is not None:
            g.conn.close()

    @app.after_request
    def _backup_on_mutation(response):
        # #4: the UI writes operator decisions to the LOCAL engine.db between
        # ingests. Mirror them to OneDrive after each successful mutating
        # request so a later cloud-newer restore can't silently discard them.
        # Best-effort only — a backup failure never breaks the response.
        try:
            if (
                request.method == "POST"
                and response.status_code < 400
                and app.config.get("ENGINE_DB_PATH")
                and app.config.get("ENGINE_INJECTED_CONN") is None
            ):
                from config import settings
                sqlite_client.backup_database_safely(
                    app.config["ENGINE_DB_PATH"], settings.ONEDRIVE_BACKUP_PATH,
                )
        except Exception:  # noqa: BLE001 — never break a response on backup
            pass
        return response

    from engine.ui.routes import register_routes
    register_routes(app)

    return app


__all__ = ["create_app"]
