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


# ---------------------------------------------------------------------------
# Records-level fake — for Step 3 read/write helpers
# ---------------------------------------------------------------------------

import re


def _split_top_level_commas(s: str) -> list[str]:
    depth = 0
    parts, current = [], []
    for c in s:
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(c)
    if current:
        parts.append("".join(current))
    return [p.strip() for p in parts]


def _eval_formula(formula: str, record: dict) -> bool:
    """Tiny Airtable-formula evaluator covering only the patterns the engine
    emits. Anything else raises so a future engine code change surfaces in
    these tests, not silently."""
    fields = record.get("fields") or {}
    formula = formula.strip()

    # NOT(<inner>) — recurse.
    if formula.startswith("NOT(") and formula.endswith(")"):
        return not _eval_formula(formula[4:-1], record)

    # AND(<part>, <part>, ...) — recurse on each top-level comma-separated
    # clause.
    if formula.startswith("AND(") and formula.endswith(")"):
        inner = formula[4:-1]
        parts = _split_top_level_commas(inner)
        return all(_eval_formula(p, record) for p in parts)

    # {Field} bare reference — truthy check (e.g. checkbox).
    m = re.match(r"\{([^}]+)\}$", formula)
    if m:
        return bool(fields.get(m.group(1), False))

    # {Field}!=BLANK()
    m = re.match(r"\{([^}]+)\}!=BLANK\(\)$", formula)
    if m:
        v = fields.get(m.group(1))
        return v is not None and v != ""

    # {Field}!=''  (single-quoted empty)
    m = re.match(r"\{([^}]+)\}!=''$", formula)
    if m:
        v = fields.get(m.group(1), "")
        return bool(str(v).strip())

    # {Field}='value'  (single-quoted literal)
    m = re.match(r"\{([^}]+)\}='(.*)'$", formula)
    if m:
        return str(fields.get(m.group(1), "")) == m.group(2)

    # {Field}="value"  (double-quoted literal — current engine convention
    # because backslash escapes inside single-quoted literals are unreliable
    # in Airtable formulas).
    m = re.match(r'\{([^}]+)\}="(.*)"$', formula)
    if m:
        return str(fields.get(m.group(1), "")) == m.group(2)

    raise NotImplementedError(f"fake doesn't support formula: {formula!r}")


class _RecordsTable:
    def __init__(self, base, name):
        self.base = base
        self.name = name

    def all(self, formula=None, **kwargs):
        records = self.base._tables.get(self.name, [])
        if formula is None:
            return [dict(r) for r in records]
        return [dict(r) for r in records if _eval_formula(formula, r)]

    def first(self, formula=None):
        hits = self.all(formula=formula)
        return hits[0] if hits else None

    def create(self, fields, typecast=False):
        self.base._counter += 1
        rec_id = f"rec{self.base._counter:04d}"
        record = {"id": rec_id, "fields": dict(fields),
                  "createdTime": "2026-06-12T00:00:00.000Z"}
        self.base._tables.setdefault(self.name, []).append(record)
        self.base.ops.append(("create", self.name, dict(fields)))
        return dict(record)

    def update(self, record_id, fields, typecast=False, replace=False):
        for r in self.base._tables.get(self.name, []):
            if r["id"] == record_id:
                if replace:
                    r["fields"] = dict(fields)
                else:
                    r["fields"] = {**r["fields"], **fields}
                self.base.ops.append(("update", self.name, record_id, dict(fields)))
                return dict(r)
        raise KeyError(record_id)

    def delete(self, record_id):
        records = self.base._tables.get(self.name, [])
        for i, r in enumerate(records):
            if r["id"] == record_id:
                records.pop(i)
                self.base.ops.append(("delete", self.name, record_id))
                return
        raise KeyError(record_id)


class _RecordsBase:
    """Minimal fake base supporting record I/O. seed() populates an initial
    table state."""

    def __init__(self):
        self._tables: dict[str, list[dict]] = {}
        self._counter = 0
        self.ops: list[tuple] = []

    def table(self, name):
        return _RecordsTable(self, name)

    def seed(self, name, records: list[dict]) -> None:
        for r in records:
            if "id" not in r:
                self._counter += 1
                r["id"] = f"rec_seed_{self._counter:04d}"
            r.setdefault("createdTime", "2026-06-12T00:00:00.000Z")
        self._tables.setdefault(name, []).extend(records)


# ---------------------------------------------------------------------------
# load_vendor_aliases
# ---------------------------------------------------------------------------

def test_load_vendor_aliases_parses_commas_and_newlines():
    from engine.airtable_client import load_vendor_aliases
    base = _RecordsBase()
    base.seed("Vendor Aliases", [
        {"fields": {"Contract Name": "Acme SaaS", "Aliases": "ACME, ACME INC"}},
        {"fields": {"Contract Name": "Beta Tools", "Aliases": "BETA\nBETA TOOLS\nbeta inc"}},
    ])
    out = load_vendor_aliases(base)
    assert out == {
        "Acme SaaS": ["ACME", "ACME INC"],
        "Beta Tools": ["BETA", "BETA TOOLS", "beta inc"],
    }


def test_load_vendor_aliases_handles_empty_aliases_cell():
    from engine.airtable_client import load_vendor_aliases
    base = _RecordsBase()
    base.seed("Vendor Aliases", [
        {"fields": {"Contract Name": "Solo Contract", "Aliases": ""}},
        {"fields": {"Contract Name": "Another"}},  # no Aliases field at all
    ])
    out = load_vendor_aliases(base)
    assert out == {"Solo Contract": [], "Another": []}


# ---------------------------------------------------------------------------
# load_campus_map_overrides
# ---------------------------------------------------------------------------

def test_load_campus_map_overrides_picks_up_forward_and_drops():
    from engine.airtable_client import load_campus_map_overrides
    base = _RecordsBase()
    base.seed("Campus Map", [
        {"fields": {"Tableau Code": "CEN", "Asana Option Names": "CEN, CEN/EDM, EDM_NEW"}},
        {"fields": {"Tableau Code": "ZZZ", "Drop": True}},
        {"fields": {"Tableau Code": "OMH", "Asana Option Names": "OMH"}},
    ])
    overrides, drops = load_campus_map_overrides(base)
    assert overrides == {
        "CEN": frozenset({"CEN", "CEN/EDM", "EDM_NEW"}),
        "OMH": frozenset({"OMH"}),
    }
    assert drops == frozenset({"ZZZ"})


def test_load_campus_map_returns_none_drops_when_no_drop_checkboxes_set():
    """If no Campus Map row has Drop=true, return None for drops so the
    builder falls back to config defaults (INT)."""
    from engine.airtable_client import load_campus_map_overrides
    base = _RecordsBase()
    base.seed("Campus Map", [
        {"fields": {"Tableau Code": "CEN", "Asana Option Names": "CEN, CEN/EDM"}},
    ])
    overrides, drops = load_campus_map_overrides(base)
    assert overrides == {"CEN": frozenset({"CEN", "CEN/EDM"})}
    assert drops is None


# ---------------------------------------------------------------------------
# load_learned_mappings
# ---------------------------------------------------------------------------

def test_load_learned_mappings_builds_key_tuple():
    from engine.airtable_client import load_learned_mappings
    base = _RecordsBase()
    base.seed("Learned Mappings", [
        {"fields": {
            "Key": "CEN|000|63015|Acme SaaS",
            "Campus": "CEN", "Dept": "000",
            "Account No": "63015", "Vendor": "Acme SaaS",
            "Contract Name": "Acme SaaS Contract",
        }},
        {"fields": {  # incomplete — must be skipped
            "Key": "PARTIAL", "Campus": "CEN", "Contract Name": "",
        }},
    ])
    out = load_learned_mappings(base)
    assert out == {
        ("CEN", "000", "63015", "Acme SaaS"): "Acme SaaS Contract",
    }


# ---------------------------------------------------------------------------
# upsert_needs_tagging_group
# ---------------------------------------------------------------------------

def test_upsert_needs_tagging_creates_new_row():
    from engine.airtable_client import upsert_needs_tagging_group
    base = _RecordsBase()
    result = upsert_needs_tagging_group(
        base,
        group_key="CEN|000|63015|Mystery",
        campus="CEN", dept="000", account_no="63015", vendor="Mystery",
        sample_description="some desc",
        amount=1234.56,
        candidate_names=["Acme SaaS"],
        created_at_iso_date="2026-06-12",
    )
    assert result["fields"]["Group Key"] == "CEN|000|63015|Mystery"
    assert result["fields"]["$ in group"] == 1234.56
    # Engine writes to Engine Candidates (its own field), leaving Notes
    # untouched for operator annotations.
    assert result["fields"]["Engine Candidates"].startswith("Engine vendor candidates")
    assert "Notes" not in result["fields"]
    # Exactly one create, no update.
    create_ops = [op for op in base.ops if op[0] == "create"]
    update_ops = [op for op in base.ops if op[0] == "update"]
    assert len(create_ops) == 1
    assert len(update_ops) == 0


def test_upsert_needs_tagging_updates_existing_row_by_group_key():
    """Idempotency: re-upsert with the same Group Key must update the
    rolling fields, not create a duplicate row."""
    from engine.airtable_client import upsert_needs_tagging_group
    base = _RecordsBase()
    base.seed("Needs Tagging", [{
        "fields": {
            "Group Key": "CEN|000|63015|Mystery",
            "Campus": "CEN", "Dept": "000", "Account No": "63015",
            "Vendor": "Mystery",
            "Sample Record Description": "old desc",
            "$ in group": 100.0,
            "Created At": "2026-05-01",
            "Notes": "operator annotation — must survive",
        },
    }])
    upsert_needs_tagging_group(
        base,
        group_key="CEN|000|63015|Mystery",
        campus="CEN", dept="000", account_no="63015", vendor="Mystery",
        sample_description="updated desc",
        amount=999.99,
        candidate_names=[],
        created_at_iso_date="2026-06-12",
    )
    create_ops = [op for op in base.ops if op[0] == "create"]
    update_ops = [op for op in base.ops if op[0] == "update"]
    assert len(create_ops) == 0
    assert len(update_ops) == 1
    # And the existing record now has the updated amount; the operator's
    # Notes annotation is preserved (engine writes Engine Candidates only).
    records = base._tables["Needs Tagging"]
    assert len(records) == 1
    assert records[0]["fields"]["$ in group"] == 999.99
    assert records[0]["fields"]["Sample Record Description"] == "updated desc"
    assert records[0]["fields"]["Notes"] == "operator annotation — must survive"


def test_upsert_needs_tagging_with_no_candidates_says_so_in_engine_candidates():
    from engine.airtable_client import upsert_needs_tagging_group
    base = _RecordsBase()
    upsert_needs_tagging_group(
        base,
        group_key="CEN|000|63015|TotallyUnknown",
        campus="CEN", dept="000", account_no="63015", vendor="TotallyUnknown",
        sample_description="x",
        amount=10.0,
        candidate_names=[],
        created_at_iso_date="2026-06-12",
    )
    rec = base._tables["Needs Tagging"][0]
    assert "No vendor candidates" in rec["fields"]["Engine Candidates"]


def test_upsert_needs_tagging_handles_vendor_with_apostrophe():
    """Regression: vendors like Domino's or O'Reilly must round-trip through
    the formula lookup without producing a malformed query."""
    from engine.airtable_client import upsert_needs_tagging_group
    base = _RecordsBase()
    upsert_needs_tagging_group(
        base,
        group_key="CEN|000|63015|Domino's",
        campus="CEN", dept="000", account_no="63015", vendor="Domino's",
        sample_description="x",
        amount=10.0,
        candidate_names=[],
        created_at_iso_date="2026-06-12",
    )
    # Second upsert MUST update the same row, not create a duplicate.
    upsert_needs_tagging_group(
        base,
        group_key="CEN|000|63015|Domino's",
        campus="CEN", dept="000", account_no="63015", vendor="Domino's",
        sample_description="y",
        amount=20.0,
        candidate_names=[],
        created_at_iso_date="2026-06-12",
    )
    assert len(base._tables["Needs Tagging"]) == 1
    assert base._tables["Needs Tagging"][0]["fields"]["$ in group"] == 20.0


def test_formula_literal_rejects_value_with_double_quote():
    """A Group Key containing a literal double quote would break the
    formula. The helper must refuse rather than silently misquoting."""
    from engine.airtable_client import _formula_literal
    with pytest.raises(ValueError, match="double-quote"):
        _formula_literal('bad "value" with dquotes')


# ---------------------------------------------------------------------------
# promote_filled_needs_tagging
# ---------------------------------------------------------------------------

def test_promote_filled_needs_tagging_creates_learned_and_deletes_nt():
    from engine.airtable_client import promote_filled_needs_tagging
    base = _RecordsBase()
    base.seed("Needs Tagging", [
        {  # filled — must be promoted + deleted
            "fields": {
                "Group Key": "CEN|000|63015|Acme",
                "Campus": "CEN", "Dept": "000", "Account No": "63015",
                "Vendor": "Acme", "Assign Contract": "Acme SaaS Contract",
            },
        },
        {  # unfilled — must be left alone
            "fields": {
                "Group Key": "CEN|107|63020|Beta",
                "Campus": "CEN", "Dept": "107", "Account No": "63020",
                "Vendor": "Beta", "Assign Contract": "",
            },
        },
    ])
    promotions = promote_filled_needs_tagging(base, learned_at_iso_date="2026-06-12")
    assert len(promotions) == 1
    p = promotions[0]
    assert p.group_key == "CEN|000|63015|Acme"
    assert p.contract_name == "Acme SaaS Contract"

    # The Learned Mappings table now has the promoted row.
    lm = base._tables["Learned Mappings"]
    assert len(lm) == 1
    assert lm[0]["fields"]["Key"] == "CEN|000|63015|Acme"
    assert lm[0]["fields"]["Contract Name"] == "Acme SaaS Contract"
    # The Needs Tagging row was deleted; the unfilled one survives.
    nt = base._tables["Needs Tagging"]
    assert len(nt) == 1
    assert nt[0]["fields"]["Assign Contract"] == ""


def test_promote_filled_updates_existing_learned_mapping_in_place():
    """If the same Key already exists in Learned Mappings (operator
    re-promotion), update rather than duplicate."""
    from engine.airtable_client import promote_filled_needs_tagging
    base = _RecordsBase()
    base.seed("Learned Mappings", [{
        "fields": {
            "Key": "CEN|000|63015|Acme",
            "Campus": "CEN", "Dept": "000", "Account No": "63015",
            "Vendor": "Acme", "Contract Name": "OLD Contract Name",
        },
    }])
    base.seed("Needs Tagging", [{
        "fields": {
            "Group Key": "CEN|000|63015|Acme",
            "Campus": "CEN", "Dept": "000", "Account No": "63015",
            "Vendor": "Acme", "Assign Contract": "NEW Contract Name",
        },
    }])
    promotions = promote_filled_needs_tagging(base, learned_at_iso_date="2026-06-12")
    assert len(promotions) == 1
    lm = base._tables["Learned Mappings"]
    assert len(lm) == 1  # no duplicate
    assert lm[0]["fields"]["Contract Name"] == "NEW Contract Name"


def test_promote_skips_rows_with_incomplete_fields():
    """A row with a filled Assign Contract but missing Campus/Dept/etc.
    should be skipped with a warning, not promoted into a broken Learned
    Mappings row."""
    from engine.airtable_client import promote_filled_needs_tagging
    base = _RecordsBase()
    base.seed("Needs Tagging", [{
        "fields": {
            "Group Key": "MISSING",
            "Campus": "",   # missing
            "Dept": "000", "Account No": "63015", "Vendor": "Acme",
            "Assign Contract": "Some Contract",
        },
    }])
    promotions = promote_filled_needs_tagging(base, learned_at_iso_date="2026-06-12")
    assert len(promotions) == 0
    # Needs Tagging row left intact (no destructive op).
    assert len(base._tables["Needs Tagging"]) == 1


def test_promote_rejects_unknown_contract_name_when_validation_set_provided():
    """Validation guard: an operator typo in Assign Contract must not bake
    a permanent broken Learned Mapping. Row stays for correction."""
    from engine.airtable_client import promote_filled_needs_tagging
    base = _RecordsBase()
    base.seed("Needs Tagging", [{
        "fields": {
            "Group Key": "CEN|000|63015|Acme",
            "Campus": "CEN", "Dept": "000", "Account No": "63015",
            "Vendor": "Acme", "Assign Contract": "TypoedNameHere",
        },
    }])
    promotions = promote_filled_needs_tagging(
        base, learned_at_iso_date="2026-06-12",
        valid_contract_names=frozenset({"Acme SaaS Contract"}),
    )
    assert promotions == []
    # NT row preserved for operator correction.
    assert len(base._tables["Needs Tagging"]) == 1
    # No LM created.
    assert base._tables.get("Learned Mappings", []) == []


def test_promote_with_no_validation_set_still_promotes():
    """valid_contract_names=None disables validation (initial-run / dev path)."""
    from engine.airtable_client import promote_filled_needs_tagging
    base = _RecordsBase()
    base.seed("Needs Tagging", [{
        "fields": {
            "Group Key": "CEN|000|63015|Acme",
            "Campus": "CEN", "Dept": "000", "Account No": "63015",
            "Vendor": "Acme", "Assign Contract": "Any Name",
        },
    }])
    promotions = promote_filled_needs_tagging(base, learned_at_iso_date="2026-06-12")
    assert len(promotions) == 1


# ---------------------------------------------------------------------------
# cleanup_stale_needs_tagging
# ---------------------------------------------------------------------------

def test_cleanup_stale_needs_tagging_deletes_only_empty_rows_not_in_live_set():
    from engine.airtable_client import cleanup_stale_needs_tagging
    base = _RecordsBase()
    base.seed("Needs Tagging", [
        {"fields": {"Group Key": "STALE|000|63015|X", "Assign Contract": ""}},
        {"fields": {"Group Key": "LIVE|000|63015|Y", "Assign Contract": ""}},
        # Filled row — NEVER deleted regardless of live-set membership.
        {"fields": {"Group Key": "STALE_BUT_FILLED|000|63015|Z",
                    "Assign Contract": "Some Contract"}},
    ])
    deleted = cleanup_stale_needs_tagging(
        base, live_group_keys={"LIVE|000|63015|Y"},
    )
    assert deleted == 1
    remaining_keys = {
        r["fields"]["Group Key"] for r in base._tables["Needs Tagging"]
    }
    assert remaining_keys == {"LIVE|000|63015|Y", "STALE_BUT_FILLED|000|63015|Z"}


def test_cleanup_stale_needs_tagging_with_empty_live_set_deletes_all_unfilled():
    from engine.airtable_client import cleanup_stale_needs_tagging
    base = _RecordsBase()
    base.seed("Needs Tagging", [
        {"fields": {"Group Key": "A|0|1|X", "Assign Contract": ""}},
        {"fields": {"Group Key": "B|0|1|Y", "Assign Contract": ""}},
        {"fields": {"Group Key": "C|0|1|Z", "Assign Contract": "Has Answer"}},
    ])
    deleted = cleanup_stale_needs_tagging(base, live_group_keys=set())
    assert deleted == 2
    remaining = base._tables["Needs Tagging"]
    assert len(remaining) == 1
    assert remaining[0]["fields"]["Group Key"] == "C|0|1|Z"


# ---------------------------------------------------------------------------
# upsert_dashboard_row — Step 4
# ---------------------------------------------------------------------------

from datetime import date  # noqa: E402

from engine.compute import DashboardRow  # noqa: E402


def _dashboard_row(**overrides) -> DashboardRow:
    base = dict(
        contract_name="Acme",
        asana_task_gid="gid-acme-001",
        campus_set="CEN, OMH",
        contract_amount=10000.0,
        spent_so_far=5000.0,
        pct_spent=50.0,
        spending_rate=1.0,
        spending_rate_alarm=None,
        alarms="Clear",
        start=date(2026, 1, 1),
        due=date(2026, 12, 31),
        status="Active",
        pm_email="pm@example.com",
        last_updated=date(2026, 6, 12),
    )
    base.update(overrides)
    return DashboardRow(**base)


def test_upsert_dashboard_creates_new_row_when_no_gid_match():
    from engine.airtable_client import upsert_dashboard_row
    base = _RecordsBase()
    result = upsert_dashboard_row(base, _dashboard_row())
    assert result["fields"]["Contract"] == "Acme"
    assert result["fields"]["Asana Task GID"] == "gid-acme-001"
    assert result["fields"]["% Spent"] == 50.0
    assert result["fields"]["Alarms"] == "Clear"
    assert result["fields"]["Start"] == "2026-01-01"
    # Exactly one create, no update.
    creates = [op for op in base.ops if op[0] == "create"]
    assert len(creates) == 1


def test_upsert_dashboard_updates_existing_row_by_gid():
    """Idempotency: same GID → update, not duplicate."""
    from engine.airtable_client import upsert_dashboard_row
    base = _RecordsBase()
    upsert_dashboard_row(base, _dashboard_row(spent_so_far=1000.0, pct_spent=10.0))
    upsert_dashboard_row(base, _dashboard_row(spent_so_far=8000.0, pct_spent=80.0,
                                                spending_rate_alarm="75%",
                                                alarms="ALARM"))
    rows = base._tables["Dashboard"]
    assert len(rows) == 1
    assert rows[0]["fields"]["Spent so far"] == 8000.0
    assert rows[0]["fields"]["% Spent"] == 80.0
    assert rows[0]["fields"]["Spending Rate Alarm"] == "75%"
    assert rows[0]["fields"]["Alarms"] == "ALARM"


def test_upsert_dashboard_omits_none_fields_so_cells_stay_blank():
    """When the pace guard blanks Spending Rate or the contract has no amount,
    those fields must NOT be written as 0 — the cell stays blank."""
    from engine.airtable_client import upsert_dashboard_row
    base = _RecordsBase()
    upsert_dashboard_row(base, _dashboard_row(
        contract_amount=None,
        pct_spent=None,
        spending_rate=None,
        spending_rate_alarm=None,
    ))
    rec = base._tables["Dashboard"][0]
    assert "Contract Amount" not in rec["fields"]
    assert "% Spent" not in rec["fields"]
    assert "Spending Rate" not in rec["fields"]
    assert "Spending Rate Alarm" not in rec["fields"]
    # But the unconditional fields ARE present.
    assert "Spent so far" in rec["fields"]
    assert "Alarms" in rec["fields"]


def test_upsert_dashboard_rejects_unknown_spending_rate_alarm():
    """Client-side validation prevents typecast from silently spawning a
    new singleSelect option in Airtable."""
    from engine.airtable_client import upsert_dashboard_row
    base = _RecordsBase()
    with pytest.raises(ValueError, match="Spending Rate Alarm"):
        upsert_dashboard_row(base, _dashboard_row(spending_rate_alarm="ALMOST"))


def test_upsert_dashboard_rejects_unknown_alarms_value():
    from engine.airtable_client import upsert_dashboard_row
    base = _RecordsBase()
    with pytest.raises(ValueError, match="Alarms"):
        upsert_dashboard_row(base, _dashboard_row(alarms="MAYBE"))


def test_upsert_dashboard_accepts_apostrophe_in_contract_name():
    """Regression for the formula-escape fix: a contract name like Domino's
    must round-trip through the GID lookup without breaking."""
    from engine.airtable_client import upsert_dashboard_row
    base = _RecordsBase()
    upsert_dashboard_row(base, _dashboard_row(
        contract_name="Domino's Pizza",
        asana_task_gid="gid-with-quote",
    ))
    # Second upsert MUST update, not create a duplicate.
    upsert_dashboard_row(base, _dashboard_row(
        contract_name="Domino's Pizza",
        asana_task_gid="gid-with-quote",
        spent_so_far=999.0,
    ))
    assert len(base._tables["Dashboard"]) == 1
    assert base._tables["Dashboard"][0]["fields"]["Spent so far"] == 999.0


def test_upsert_dashboard_clears_cells_when_value_transitions_to_none():
    """HIGH-severity regression from the Step 4 review: a Dashboard cell
    that goes from a non-None value back to None must be CLEARED on update,
    not left stale. The classic failure mode is Spending Rate Alarm showing
    '75%' forever after the operator raised Contract Amount in Asana.

    On the UPDATE path, nullable fields must be explicitly written as None
    so Airtable's PATCH-merge clears the cell.
    """
    from engine.airtable_client import upsert_dashboard_row
    base = _RecordsBase()

    # Run 1: alarm tripping at 75% band.
    upsert_dashboard_row(base, _dashboard_row(
        pct_spent=80.0,
        spending_rate=1.5,
        spending_rate_alarm="75%",
        alarms="ALARM",
    ))
    # Run 2: operator raised Contract Amount; %spent drops to 30, no band.
    upsert_dashboard_row(base, _dashboard_row(
        pct_spent=30.0,
        spending_rate=0.5,
        spending_rate_alarm=None,   # must clear the cell, not stay "75%"
        alarms="Clear",
    ))

    rec = base._tables["Dashboard"][0]
    # The cell MUST now be None / absent. Stale "75%" would be a SEV-1 bug.
    assert rec["fields"].get("Spending Rate Alarm") is None, (
        f"Spending Rate Alarm cell was not cleared on update; got "
        f"{rec['fields'].get('Spending Rate Alarm')!r}. PATCH-merge bug."
    )
    assert rec["fields"]["Alarms"] == "Clear"
    assert rec["fields"]["% Spent"] == 30.0


def test_upsert_dashboard_clears_contract_amount_when_removed():
    """Same family as the stale-cell bug above — Contract Amount removed in
    Asana must clear the Dashboard cell."""
    from engine.airtable_client import upsert_dashboard_row
    base = _RecordsBase()
    upsert_dashboard_row(base, _dashboard_row(contract_amount=10000.0))
    upsert_dashboard_row(base, _dashboard_row(contract_amount=None,
                                                pct_spent=None,
                                                spending_rate=None,
                                                spending_rate_alarm=None))
    rec = base._tables["Dashboard"][0]
    assert rec["fields"].get("Contract Amount") is None


def test_dashboard_singleSelect_validators_match_settings_options():
    """Four sources of truth must stay in lock-step:
    1. settings.ASANA_SPENDING_RATE_ALARM_OPTIONS / ASANA_ALARMS_OPTIONS
    2. airtable_schema._SPENDING_RATE_ALARM_CHOICES / _ALARMS_CHOICES
    3. airtable_client._DASHBOARD_SPENDING_RATE_ALARM_VALUES / _DASHBOARD_ALARMS_VALUES
    4. (Step 1 audit reads from settings → comparison covers that path)

    A divergence between (1) and (3) means the client validator could reject
    a valid Asana option (false negative) or accept a stale one (false
    positive). Pin them equal here so a future edit to one fails CI.
    """
    from config import airtable_schema, settings
    from engine.airtable_client import (
        _DASHBOARD_ALARMS_VALUES,
        _DASHBOARD_SPENDING_RATE_ALARM_VALUES,
    )

    # settings → client validator
    assert _DASHBOARD_SPENDING_RATE_ALARM_VALUES == frozenset(
        settings.ASANA_SPENDING_RATE_ALARM_OPTIONS
    )
    assert _DASHBOARD_ALARMS_VALUES == frozenset(settings.ASANA_ALARMS_OPTIONS)

    # settings → Airtable schema choices
    sra_schema_names = [
        c["name"] for c in airtable_schema.field_spec("Dashboard", "Spending Rate Alarm")["options"]["choices"]
    ]
    assert set(sra_schema_names) == set(settings.ASANA_SPENDING_RATE_ALARM_OPTIONS)

    alarms_schema_names = [
        c["name"] for c in airtable_schema.field_spec("Dashboard", "Alarms")["options"]["choices"]
    ]
    assert set(alarms_schema_names) == set(settings.ASANA_ALARMS_OPTIONS)


# ---------------------------------------------------------------------------
# State table I/O — Step 6
# ---------------------------------------------------------------------------

def test_load_state_priors_parses_state_rows():
    """The State table is keyed by Asana Task GID. load_state_priors
    returns a {gid: StatePrior} dict. Rows missing the GID are skipped
    with a logged warning (legacy / hand-edited rows)."""
    from engine.airtable_client import load_state_priors
    base = _RecordsBase()
    base.seed("State", [
        {"fields": {
            "Contract Name": "Acme SaaS",
            "Asana Task GID": "gid-acme",
            "Prior Spent": 1234.56,
            "Prior % Spent": 12.35,
            "Prior Spending Rate": 1.5,
            "Prior Spending Rate Alarm": "75%",
            "Prior Alarms": "ALARM",
            "Last Processed Hash": "abc123",
            "Last Updated At": "2026-06-11",
        }},
        {"fields": {  # legacy row without Asana Task GID — skipped with warn
            "Contract Name": "Legacy",
            "Prior Spent": 100.0,
        }},
    ])
    priors = load_state_priors(base)
    assert set(priors) == {"gid-acme"}
    p = priors["gid-acme"]
    assert p.contract_name == "Acme SaaS"
    assert p.asana_task_gid == "gid-acme"
    assert p.prior_spent == pytest.approx(1234.56)
    assert p.prior_pct_spent == pytest.approx(12.35)
    assert p.prior_spending_rate_alarm == "75%"
    assert p.prior_alarms == "ALARM"
    assert p.last_processed_hash == "abc123"
    assert p.last_updated_at == date(2026, 6, 11)


def test_cleanup_stale_state_deletes_rows_not_in_live_set():
    """A contract archived in Asana between runs leaves its State row
    orphaned. cleanup_stale_state sweeps these — engine-owned table,
    operator doesn't hand-edit."""
    from engine.airtable_client import cleanup_stale_state
    base = _RecordsBase()
    base.seed("State", [
        {"fields": {"Contract Name": "Live", "Asana Task GID": "gid-live"}},
        {"fields": {"Contract Name": "Stale", "Asana Task GID": "gid-stale"}},
        {"fields": {"Contract Name": "Other Stale", "Asana Task GID": "gid-other"}},
    ])
    deleted = cleanup_stale_state(base, live_asana_task_gids={"gid-live"})
    assert deleted == 2
    remaining = base._tables["State"]
    assert len(remaining) == 1
    assert remaining[0]["fields"]["Asana Task GID"] == "gid-live"


def test_load_state_priors_returns_empty_dict_on_empty_table():
    """First-run: every contract surfaces as `first_run` in the diff."""
    from engine.airtable_client import load_state_priors
    base = _RecordsBase()
    base.seed("State", [])
    assert load_state_priors(base) == {}


def test_upsert_state_creates_new_row_for_first_seen_contract():
    from engine.airtable_client import upsert_state_for_contract
    base = _RecordsBase()
    upsert_state_for_contract(
        base,
        contract_name="Acme", asana_task_gid="gid-acme",
        spent=1234.56, pct_spent=12.35, spending_rate=1.5,
        spending_rate_alarm="75%", alarms="ALARM",
        last_processed_hash="hash-1",
        last_updated_iso_date="2026-06-12",
    )
    rows = base._tables["State"]
    assert len(rows) == 1
    fields = rows[0]["fields"]
    assert fields["Contract Name"] == "Acme"
    assert fields["Asana Task GID"] == "gid-acme"
    assert fields["Prior Spent"] == 1234.56
    assert fields["Prior Spending Rate Alarm"] == "75%"
    assert fields["Prior Alarms"] == "ALARM"
    assert fields["Last Processed Hash"] == "hash-1"


def test_upsert_state_updates_existing_by_asana_task_gid():
    """Idempotency by Asana Task GID — the stable identity. A rename
    on the Contract Name side still updates the same State row."""
    from engine.airtable_client import upsert_state_for_contract
    base = _RecordsBase()
    upsert_state_for_contract(
        base, contract_name="Acme", asana_task_gid="gid-acme",
        spent=1000.0, pct_spent=10.0, spending_rate=0.5,
        spending_rate_alarm=None, alarms="Clear",
        last_processed_hash="hash-1", last_updated_iso_date="2026-06-11",
    )
    # Same GID, RENAMED Contract Name — should update, not create duplicate.
    upsert_state_for_contract(
        base, contract_name="Acme Inc.", asana_task_gid="gid-acme",
        spent=2000.0, pct_spent=20.0, spending_rate=1.0,
        spending_rate_alarm=None, alarms="Clear",
        last_processed_hash="hash-2", last_updated_iso_date="2026-06-12",
    )
    rows = base._tables["State"]
    assert len(rows) == 1, "Rename created a duplicate State row — GID keying broken"
    assert rows[0]["fields"]["Contract Name"] == "Acme Inc."  # renamed
    assert rows[0]["fields"]["Prior Spent"] == 2000.0
    assert rows[0]["fields"]["Last Processed Hash"] == "hash-2"


def test_upsert_state_clears_nullable_cells_on_update():
    """Same PATCH-merge nullable gotcha as Dashboard: a Prior Spending
    Rate Alarm transitioning from '75%' back to blank must be EXPLICITLY
    cleared on update."""
    from engine.airtable_client import upsert_state_for_contract
    base = _RecordsBase()
    upsert_state_for_contract(
        base, contract_name="Acme", asana_task_gid="gid-acme",
        spent=8000.0, pct_spent=80.0, spending_rate=1.5,
        spending_rate_alarm="75%", alarms="ALARM",
        last_processed_hash="h1", last_updated_iso_date="2026-06-11",
    )
    upsert_state_for_contract(
        base, contract_name="Acme", asana_task_gid="gid-acme",
        spent=3000.0, pct_spent=30.0, spending_rate=None,  # pace guard kicked
        spending_rate_alarm=None,                          # band dropped
        alarms="Clear",
        last_processed_hash="h2", last_updated_iso_date="2026-06-12",
    )
    rec = base._tables["State"][0]
    assert rec["fields"].get("Prior Spending Rate Alarm") is None
    assert rec["fields"].get("Prior Spending Rate") is None
    assert rec["fields"]["Prior Alarms"] == "Clear"


def test_upsert_state_rejects_unknown_alarms_option():
    """Client-side singleSelect validation — a typo would otherwise spawn
    a new dropdown option in Airtable."""
    from engine.airtable_client import upsert_state_for_contract
    base = _RecordsBase()
    with pytest.raises(ValueError, match="Prior Alarms"):
        upsert_state_for_contract(
            base, contract_name="Acme", asana_task_gid="gid-acme",
            spent=0.0, pct_spent=None, spending_rate=None,
            spending_rate_alarm=None, alarms="WRONG",
            last_processed_hash="x", last_updated_iso_date="2026-06-12",
        )


def test_upsert_state_rejects_unknown_band_option():
    from engine.airtable_client import upsert_state_for_contract
    base = _RecordsBase()
    with pytest.raises(ValueError, match="Prior Spending Rate Alarm"):
        upsert_state_for_contract(
            base, contract_name="Acme", asana_task_gid="gid-acme",
            spent=0.0, pct_spent=None, spending_rate=None,
            spending_rate_alarm="ALMOST",  # not in {75%,90%,100%,Over}
            alarms="Clear",
            last_processed_hash="x", last_updated_iso_date="2026-06-12",
        )


def test_upsert_dashboard_writes_all_fourteen_fields_on_create():
    """Defensive: when a DashboardRow has every optional field set, the
    create payload should contain all 14 keys mapping to Airtable's
    Dashboard schema (the spec §3 fields). A regression that drops one
    silently from the payload (e.g. someone removes 'Status' on a refactor)
    would otherwise pass the existing tests."""
    from engine.airtable_client import upsert_dashboard_row
    base = _RecordsBase()
    upsert_dashboard_row(base, _dashboard_row(
        contract_name="Acme",
        asana_task_gid="gid-full",
        campus_set="CEN, OMH",
        contract_amount=10000.0,
        spent_so_far=5000.0,
        pct_spent=50.0,
        spending_rate=1.0,
        spending_rate_alarm=None,  # validly None — band not reached
        alarms="Clear",
        start=date(2026, 1, 1),
        due=date(2026, 12, 31),
        status="Active",
        pm_email="pm@example.com",
        last_updated=date(2026, 6, 12),
    ))
    fields = base._tables["Dashboard"][0]["fields"]
    # 13 fields populated; Spending Rate Alarm is intentionally absent
    # because the band is None — that's the CREATE-path None-omitting
    # behavior, verified separately.
    for key in (
        "Contract", "Asana Task GID", "Campus Set", "Contract Amount",
        "Spent so far", "% Spent", "Spending Rate", "Alarms",
        "Start", "Due", "Status", "PM Email", "Last Updated",
    ):
        assert key in fields, f"Dashboard create payload missing key {key!r}"
    # And the absent one stays absent.
    assert "Spending Rate Alarm" not in fields
