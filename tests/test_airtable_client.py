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
