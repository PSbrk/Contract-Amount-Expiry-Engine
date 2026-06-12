"""Tests for engine.airtable_client schema management — no live Airtable.

A small Fake* hierarchy stands in for pyairtable's Api/Base/Table/Schema
classes. It records create_table / create_field calls so tests assert exactly
which mutations the engine would perform.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from config import airtable_schema
from engine.airtable_client import (
    SchemaPlan,
    _coerce_inbox,
    _field_payload,
    ensure_schema,
)


# ---------------------------------------------------------------------------
# Fake pyairtable surface
# ---------------------------------------------------------------------------

@dataclass
class _FakeFieldSchema:
    name: str
    type: str
    id: str = "fldFAKE"


@dataclass
class _FakeTableSchema:
    name: str
    fields: list
    id: str = "tblFAKE"


@dataclass
class _FakeBaseSchema:
    tables: list


@dataclass
class _FakeTable:
    base: "_FakeBase"
    name: str

    def create_field(self, name: str, field_type: str, description: str | None = None,
                     options: dict | None = None) -> None:
        self.base._created_fields.append({
            "table": self.name,
            "name": name,
            "type": field_type,
            "options": options,
            "description": description,
        })
        self.base._existing.setdefault(self.name, []).append((name, field_type))


@dataclass
class _FakeBase:
    # {table_name: [(field_name, field_type), ...]}
    _existing: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    _created_tables: list[dict] = field(default_factory=list)
    _created_fields: list[dict] = field(default_factory=list)

    def schema(self, force: bool = False) -> _FakeBaseSchema:
        return _FakeBaseSchema([
            _FakeTableSchema(
                name=n,
                fields=[_FakeFieldSchema(name=fn, type=ft) for fn, ft in fields],
            )
            for n, fields in self._existing.items()
        ])

    def create_table(self, name: str, fields: list, description: str | None = None) -> None:
        self._created_tables.append({"name": name, "fields": fields, "description": description})
        self._existing[name] = [(f["name"], f["type"]) for f in fields]

    def table(self, name: str) -> _FakeTable:
        return _FakeTable(base=self, name=name)


def _full_existing_from_schema() -> dict[str, list[tuple[str, str]]]:
    return {
        t["name"]: [(f["name"], f["type"]) for f in t["fields"]]
        for t in airtable_schema.TABLES_SCHEMA
    }


# ---------------------------------------------------------------------------
# _field_payload — picks only the keys Airtable's create-field API accepts
# ---------------------------------------------------------------------------

def test_field_payload_strips_extra_keys():
    full = {
        "name": "X", "type": "singleLineText",
        "description": "doc", "options": {"k": "v"},
        "extra_engine_only": "should be dropped",
    }
    assert _field_payload(full) == {
        "name": "X", "type": "singleLineText",
        "description": "doc", "options": {"k": "v"},
    }


def test_field_payload_drops_none_values():
    minimal = {"name": "X", "type": "checkbox", "options": None, "description": None}
    assert _field_payload(minimal) == {"name": "X", "type": "checkbox"}


# ---------------------------------------------------------------------------
# ensure_schema — empty / full / partial / dry-run
# ---------------------------------------------------------------------------

def test_ensure_schema_empty_base_creates_all_eight_tables():
    base = _FakeBase()
    plan = ensure_schema(base)

    assert isinstance(plan, SchemaPlan)
    expected = {t["name"] for t in airtable_schema.TABLES_SCHEMA}
    assert set(plan.tables_created) == expected
    # Fields land via create_table when a whole table is being created, NOT
    # via individual create_field calls.
    assert plan.fields_added == []
    assert plan.tables_already_present == []
    assert len(base._created_tables) == 8
    assert base._created_fields == []


def test_ensure_schema_already_provisioned_base_is_noop():
    base = _FakeBase(_existing=_full_existing_from_schema())
    plan = ensure_schema(base)

    assert plan.tables_created == []
    assert plan.fields_added == []
    assert plan.is_noop is True
    assert set(plan.tables_already_present) == {
        t["name"] for t in airtable_schema.TABLES_SCHEMA
    }
    assert base._created_tables == []
    assert base._created_fields == []


def test_ensure_schema_partial_adds_only_missing_fields():
    """Inbox exists but is missing 'Notes' (drift); other tables are absent."""
    existing = _full_existing_from_schema()
    # Drop 'Notes' from Inbox.
    existing["Inbox"] = [
        (n, t) for (n, t) in existing["Inbox"] if n != "Notes"
    ]
    # Remove every table except Inbox.
    keep = "Inbox"
    existing = {k: v for k, v in existing.items() if k == keep}

    base = _FakeBase(_existing=existing)
    plan = ensure_schema(base)

    assert ("Inbox", "Notes") in plan.fields_added
    assert "Inbox" not in plan.tables_created
    other_names = {t["name"] for t in airtable_schema.TABLES_SCHEMA if t["name"] != "Inbox"}
    assert set(plan.tables_created) == other_names
    # Exactly one field created (Notes on Inbox); the rest landed via the
    # create_table calls for the 7 other tables.
    assert len(base._created_fields) == 1
    assert base._created_fields[0]["table"] == "Inbox"
    assert base._created_fields[0]["name"] == "Notes"


def test_ensure_schema_dry_run_makes_no_writes():
    base = _FakeBase()
    plan = ensure_schema(base, dry_run=True)

    expected_table_names = [t["name"] for t in airtable_schema.TABLES_SCHEMA]
    assert plan.tables_created == expected_table_names
    # ZERO writes.
    assert base._created_tables == []
    assert base._created_fields == []


def test_ensure_schema_dry_run_against_partial_base():
    """dry_run on a partial base should plan adds without applying them."""
    existing = _full_existing_from_schema()
    existing["Inbox"] = [(n, t) for (n, t) in existing["Inbox"] if n != "Notes"]
    base = _FakeBase(_existing=existing)
    plan = ensure_schema(base, dry_run=True)

    assert ("Inbox", "Notes") in plan.fields_added
    assert plan.is_noop is False
    assert base._created_fields == []


# ---------------------------------------------------------------------------
# _coerce_inbox — record shape handling
# ---------------------------------------------------------------------------

def test_coerce_inbox_handles_complete_record():
    raw = {
        "id": "recABC",
        "createdTime": "2026-06-12T14:00:00.000Z",
        "fields": {
            "Name": "Q2.csv",
            "Attachment": [{"url": "https://x", "filename": "Q2.csv"}],
            "File Hash": "abc123",
            "Processed": True,
        },
    }
    rec = _coerce_inbox(raw)
    assert rec.id == "recABC"
    assert rec.name == "Q2.csv"
    assert rec.created_time == "2026-06-12T14:00:00.000Z"
    assert len(rec.attachments) == 1
    assert rec.file_hash == "abc123"
    assert rec.processed is True


def test_coerce_inbox_handles_missing_optional_fields():
    """Airtable omits empty fields from the API response."""
    raw = {"id": "recXYZ", "createdTime": "2026-06-12T14:00:00.000Z", "fields": {}}
    rec = _coerce_inbox(raw)
    assert rec.name == ""
    assert rec.attachments == []
    assert rec.file_hash == ""
    assert rec.processed is False


def test_coerce_inbox_treats_none_attachment_as_empty_list():
    """Airtable may return Attachment as None or omit it entirely; both must
    coerce to an empty list, not break downstream `if not record.attachments`
    checks."""
    raw = {
        "id": "recDEF",
        "createdTime": "2026-06-12T14:00:00.000Z",
        "fields": {"Attachment": None},
    }
    rec = _coerce_inbox(raw)
    assert rec.attachments == []


# ---------------------------------------------------------------------------
# Run Log validation — typecast=False means typos must raise
# ---------------------------------------------------------------------------

def test_append_run_log_rejects_unknown_mode():
    from engine.airtable_client import append_run_log
    base = _FakeBase(_existing=_full_existing_from_schema())
    with pytest.raises(ValueError, match="not one of"):
        append_run_log(base, run_id="x", mode="ingst", outcome="ok")


def test_append_run_log_rejects_unknown_outcome():
    from engine.airtable_client import append_run_log
    base = _FakeBase(_existing=_full_existing_from_schema())
    with pytest.raises(ValueError, match="not one of"):
        append_run_log(base, run_id="x", mode="ingest", outcome="success")
