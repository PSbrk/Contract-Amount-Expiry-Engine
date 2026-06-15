"""SQLite-aware view of the declarative storage schema.

Phase 1 of the local-first migration: re-exports TABLES_SCHEMA from the
original config.airtable_schema, with the multipleAttachments field type
filtered out. In the local-first model, Inbox attachments live in the
data/inbox/ folder rather than as table cells, so the column has no
SQLite equivalent and is dropped.

config.airtable_schema remains the literal source of truth for the
field declarations through Phase 1 — once the Airtable code path is
removed in a later phase, the declarations move here directly.

sqlite_column_type() is the type-string mapper that engine.sqlite_client
uses to translate field declarations into SQLite column definitions.
"""

from __future__ import annotations

from typing import Final

from config import airtable_schema


def _filter_unsupported_fields(table_decl: dict) -> dict:
    return {
        **table_decl,
        "fields": [
            f for f in table_decl["fields"]
            if f["type"] != "multipleAttachments"
        ],
    }


TABLES_SCHEMA: Final = [
    _filter_unsupported_fields(t) for t in airtable_schema.TABLES_SCHEMA
]

TABLE_NAMES: Final = tuple(t["name"] for t in TABLES_SCHEMA)


def table_spec(name: str) -> dict:
    for t in TABLES_SCHEMA:
        if t["name"] == name:
            return t
    raise KeyError(
        f"no declared schema for table {name!r}; known: {TABLE_NAMES}"
    )


def field_spec(table_name: str, field_name: str) -> dict:
    t = table_spec(table_name)
    for f in t["fields"]:
        if f["name"] == field_name:
            return f
    raise KeyError(
        f"no declared field {field_name!r} on table {table_name!r}"
    )


def sqlite_column_type(field_decl: dict) -> str:
    """Map a declarative field type to a SQLite column-type clause.

    - singleLineText / multilineText / singleSelect / date  → TEXT
        (dates are stored as ISO YYYY-MM-DD strings — SQLite has no
        native date type, and string ordering matches calendar order
        for ISO form).
    - checkbox                                              → INTEGER NOT NULL DEFAULT 0
        (so a missing value reads as falsy without a NULL check at
        every callsite).
    - number with precision == 0                            → INTEGER
    - number with precision >= 1                            → REAL
    - multipleAttachments                                   → raises
        (should be filtered before reaching this mapper; surface a
        loud error if the filter is bypassed).
    """
    ft = field_decl["type"]
    if ft in ("singleLineText", "multilineText", "singleSelect", "date"):
        return "TEXT"
    if ft == "checkbox":
        return "INTEGER NOT NULL DEFAULT 0"
    if ft == "number":
        prec = (field_decl.get("options") or {}).get("precision", 0)
        return "INTEGER" if prec == 0 else "REAL"
    if ft == "multipleAttachments":
        raise ValueError(
            "multipleAttachments has no SQLite equivalent — filter it out "
            "before reaching sqlite_column_type."
        )
    raise ValueError(f"unsupported field type {ft!r} for SQLite mapping")


__all__ = [
    "TABLES_SCHEMA",
    "TABLE_NAMES",
    "table_spec",
    "field_spec",
    "sqlite_column_type",
]
