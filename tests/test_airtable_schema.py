"""Schema declaration sanity checks. Does not touch Airtable.

Pins the spec §3 field list per table plus the two Airtable field-type
footguns Step 1 research surfaced (multipleAttachments vs attachment,
multilineText vs longText).
"""

from __future__ import annotations

from config import airtable_schema, settings


def test_eight_tables_declared():
    assert len(airtable_schema.TABLES_SCHEMA) == 8
    assert set(airtable_schema.TABLE_NAMES) == {
        "Inbox", "Dashboard", "Needs Tagging", "Vendor Aliases",
        "Campus Map", "Learned Mappings", "State", "Run Log",
    }


def test_table_names_match_settings_constants():
    # config.settings.AIRTABLE_TABLES and airtable_schema.TABLE_NAMES must
    # agree — a divergence means the engine could declare a schema the rest
    # of the code doesn't know how to look up.
    assert set(airtable_schema.TABLE_NAMES) == set(settings.AIRTABLE_TABLES)


def test_inbox_has_required_fields_per_spec_section_3():
    field_names = {f["name"] for f in airtable_schema.table_spec("Inbox")["fields"]}
    # Spec §3: Attachment, Processed, File Hash, Processed At, Rows In Scope, Notes
    for required in ("Attachment", "Processed", "File Hash",
                     "Processed At", "Rows In Scope", "Notes"):
        assert required in field_names, f"Inbox missing required field {required!r}"


def test_dashboard_has_required_fields_per_spec_section_3():
    field_names = {f["name"] for f in airtable_schema.table_spec("Dashboard")["fields"]}
    for required in (
        "Contract", "Contract Amount", "Spent so far", "% Spent",
        "Spending Rate", "Spending Rate Alarm", "Alarms",
        "Start", "Due", "Status",
    ):
        assert required in field_names, f"Dashboard missing required field {required!r}"


def test_needs_tagging_has_required_fields_per_spec_section_3():
    field_names = {f["name"] for f in airtable_schema.table_spec("Needs Tagging")["fields"]}
    for required in (
        "Campus", "Dept", "Account No", "Vendor",
        "Sample Record Description", "$ in group", "Assign Contract",
    ):
        assert required in field_names


def test_attachment_field_uses_multiple_attachments_type():
    """Step 1 research footgun: Airtable's attachment type is named
    'multipleAttachments' in the schema API, NOT 'attachment'."""
    att = airtable_schema.field_spec("Inbox", "Attachment")
    assert att["type"] == "multipleAttachments"


def test_long_text_uses_multiline_text_type():
    """Step 1 research footgun: long text is 'multilineText', NOT 'longText'."""
    assert airtable_schema.field_spec("Inbox", "Notes")["type"] == "multilineText"
    assert airtable_schema.field_spec("Run Log", "Anomalies")["type"] == "multilineText"


def test_single_select_choices_mirror_asana_option_names():
    """Spending Rate Alarm + Alarms dropdowns in Airtable must mirror the
    Asana option names exactly so the dashboard and Asana stay in lock-step."""
    sra = airtable_schema.field_spec("Dashboard", "Spending Rate Alarm")
    assert [c["name"] for c in sra["options"]["choices"]] == [
        "75%", "90%", "100%", "Over",
    ]
    alarms = airtable_schema.field_spec("Dashboard", "Alarms")
    assert [c["name"] for c in alarms["options"]["choices"]] == ["Clear", "ALARM"]


def test_state_table_carries_prior_alarm_options():
    """State.Prior Alarms / Prior Spending Rate Alarm must use the same option
    names as the Dashboard / Asana to support change detection comparisons."""
    pa = airtable_schema.field_spec("State", "Prior Alarms")
    assert [c["name"] for c in pa["options"]["choices"]] == ["Clear", "ALARM"]
    psra = airtable_schema.field_spec("State", "Prior Spending Rate Alarm")
    assert [c["name"] for c in psra["options"]["choices"]] == [
        "75%", "90%", "100%", "Over",
    ]


def test_inbox_first_field_is_singleLineText_not_attachment():
    """Airtable's primary field cannot be an attachment. First field has to be
    a text field. Pins the choice so a refactor that would put Attachment
    first (and silently break create_table) fails this test."""
    inbox = airtable_schema.table_spec("Inbox")
    assert inbox["fields"][0]["type"] == "singleLineText"


def test_run_log_primary_field_is_run_id():
    """The primary field cannot be deleted on Airtable, only renamed. Pin
    the choice so a future refactor doesn't quietly orphan existing rows."""
    rl = airtable_schema.table_spec("Run Log")
    assert rl["fields"][0]["name"] == "Run ID"
    assert rl["fields"][0]["type"] == "singleLineText"


def test_table_spec_keyerror_on_unknown_table():
    import pytest
    with pytest.raises(KeyError):
        airtable_schema.table_spec("NotARealTable")


def test_field_spec_keyerror_on_unknown_field():
    import pytest
    with pytest.raises(KeyError):
        airtable_schema.field_spec("Inbox", "NotARealField")
