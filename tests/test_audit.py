"""Tests for engine.audit that don't touch Asana.

These pin the documented Step 1 deliverable that audit handles a missing
ASANA_PAT gracefully — clear error message and non-zero exit code.
"""

from __future__ import annotations

import dotenv

from engine import audit


def test_audit_exits_2_when_pat_missing(monkeypatch, capsys):
    """Missing PAT → exit code 2 + clear FATAL message on stderr.

    A future refactor that swallows the error or returns 0/1 would break the
    contract that an unauthenticated run is loudly distinguishable from a
    schema mismatch (exit 1) or success (exit 0).
    """
    monkeypatch.delenv("ASANA_PAT", raising=False)
    # Stub load_dotenv so a local .env from the developer's machine cannot
    # silently re-populate the var and make the test pass / fail by accident.
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: None)

    rc = audit.main([])

    assert rc == 2, f"expected exit code 2 on missing PAT, got {rc}"
    captured = capsys.readouterr()
    assert "FATAL" in captured.err
    assert "ASANA_PAT" in captured.err


def test_audit_finding_severity_constants():
    """Marker mapping pins the three severities the codebase uses elsewhere."""
    assert set(audit._MARKERS) == {"PASS", "FAIL", "WARN"}
