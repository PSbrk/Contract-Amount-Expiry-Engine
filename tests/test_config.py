"""Smoke tests for config — pins the new shape after the Airtable pivot.

Step 1 sweeps Google + n8n + engine-sends-email constants. These tests pin the
new layout so a future rewrite cannot silently drop a required constant or
let an obsolete name return.
"""

from config import campus_map, settings


def test_filter_sets_match_spec():
    # 63015 (Capital Projects) removed from scope on 2026-06-16 — engine
    # no longer tracks CapEx.
    assert settings.ACCOUNTS_IN_SCOPE == frozenset(
        {"63020", "63040", "63080", "63090"}
    )
    assert settings.DEPTS_IN_SCOPE == frozenset({"000", "107"})


def test_writable_field_gids_present():
    assert settings.ASANA_FIELD_SPENT_SO_FAR == "1215629256175944"
    assert settings.ASANA_FIELD_PCT_SPENT == "1215629256175946"
    assert settings.ASANA_FIELD_SPENDING_RATE == "1215629256175948"
    assert settings.ASANA_FIELD_SPENDING_RATE_ALARM == "1215629256175950"
    assert settings.ASANA_FIELD_ALARMS == "1215681548746113"


def test_spending_rate_alarm_options():
    assert settings.ASANA_SPENDING_RATE_ALARM_OPTIONS == {
        "75%": "1215629256175951",
        "90%": "1215629256175952",
        "100%": "1215629256175953",
        "Over": "1215629256175954",
    }


def test_alarms_options():
    assert settings.ASANA_ALARMS_OPTIONS == {
        "Clear": "1215681548746114",
        "ALARM": "1215681548746115",
    }


def test_expected_write_fields_carry_options():
    sra = settings.ASANA_EXPECTED_WRITE_FIELDS["Spending Rate Alarm"]
    assert sra["gid"] == settings.ASANA_FIELD_SPENDING_RATE_ALARM
    assert sra["type"] == "enum"
    assert sra["expected_options"] == settings.ASANA_SPENDING_RATE_ALARM_OPTIONS

    alarms = settings.ASANA_EXPECTED_WRITE_FIELDS["Alarms"]
    assert alarms["gid"] == settings.ASANA_FIELD_ALARMS
    assert alarms["type"] == "enum"
    assert alarms["expected_options"] == settings.ASANA_ALARMS_OPTIONS

    # Exactly five writable fields, no more.
    assert set(settings.ASANA_EXPECTED_WRITE_FIELDS) == {
        "Spent so far", "% Spent", "Spending Rate",
        "Spending Rate Alarm", "Alarms",
    }


def test_expected_read_fields_have_status_and_expire_options():
    cs = settings.ASANA_EXPECTED_READ_FIELDS["Contract Status"]
    assert cs["expected_options"] == {"Active": settings.ASANA_OPTION_STATUS_ACTIVE}

    ec = settings.ASANA_EXPECTED_READ_FIELDS["Expire countdown"]
    assert ec["expected_options"] == {"EXPIRED!": settings.ASANA_OPTION_EXPIRE_EXPIRED}


def test_engine_table_names_in_schema():
    """The 9-table contract: Inbox, Dashboard, Needs Tagging, Vendor Aliases,
    Campus Map, Learned Mappings, Amendment Links, State, Run Log. Pinned
    in config.schema so a rename or deletion fails CI rather than silently
    breaking the UI."""
    from config import schema
    expected = {
        "Inbox", "Dashboard", "Needs Tagging", "Vendor Aliases",
        "Campus Map", "Learned Mappings", "Amendment Links",
        "State", "Run Log",
    }
    assert set(schema.TABLE_NAMES) == expected
    assert len(schema.TABLE_NAMES) == 9  # no duplicates


def test_write_gate_section_is_active_compliant():
    # Pending Onboarding does NOT receive writes per spec §7.
    assert settings.ASANA_WRITE_GATE_SECTION == "Active - Compliant"


def test_campus_map_overrides():
    assert campus_map.TABLEAU_DROP_CODES == frozenset({"INT"})
    assert campus_map.ASANA_WILDCARD_OPTIONS == frozenset({"All Campuses"})
    assert campus_map.TABLEAU_TO_ASANA["CEN"] == frozenset({"CEN", "CEN/EDM"})
    assert campus_map.TABLEAU_TO_ASANA["YVN"] == frozenset({"CEN", "CEN/EDM"})
    assert campus_map.ASANA_OVERRIDE_TO_TABLEAU["***NOR (contract is for OMH)"] == frozenset({"OMH"})
    assert campus_map.ASANA_OVERRIDE_TO_TABLEAU["***TUL (contract is for SBA)"] == frozenset({"SBA"})
    assert campus_map.ASANA_NO_TABLEAU_EQUIVALENT == frozenset({"DEN", "KC"})


def test_pace_and_budget_defaults():
    assert settings.RUNAWAY_PACE == 2.0
    assert settings.PACE_GUARD_DAYS == 30
    assert settings.MIN_SPEND_FLOOR == 1000.0
    assert [band[1] for band in settings.BUDGET_BANDS] == ["75%", "90%", "100%"]
    assert settings.BUDGET_OVER_LABEL == "Over"


def test_dry_run_default_true():
    # During build, the safe default is dry-run.
    assert settings.DRY_RUN_ASANA is True


def test_env_bool_truthy_aliases(monkeypatch):
    """Recognized truthy aliases parse to True."""
    eb = settings._env_bool
    for truthy in ("1", "true", "TRUE", "yes", "ON", " true ", "True"):
        monkeypatch.setenv("_CAE_TEST_BOOL", truthy)
        assert eb("_CAE_TEST_BOOL", False) is True, f"expected True for {truthy!r}"


def test_env_bool_explicit_falsy_aliases(monkeypatch):
    """Recognized falsy aliases (the EXPLICIT off-list) parse to False.
    Empty / whitespace-only is included — operator clearing the value
    likely means 'turn this off'."""
    eb = settings._env_bool
    for falsy in ("0", "false", "FALSE", "no", "off", "", "  "):
        monkeypatch.setenv("_CAE_TEST_BOOL", falsy)
        assert eb("_CAE_TEST_BOOL", True) is False, f"expected False for {falsy!r}"


def test_env_bool_unrecognized_value_falls_back_to_default(monkeypatch, caplog):
    """SAFETY-CRITICAL: a typo'd DRY_RUN_ASANA=tru must NOT flip dry-run
    to live. Unrecognized values fall back to the supplied default so the
    operator's safe-by-default invariant holds across copy-paste mistakes."""
    import logging
    eb = settings._env_bool
    for typo in ("maybe", "2", "tru", "T", "yse", "junk"):
        monkeypatch.setenv("_CAE_TEST_BOOL", typo)
        # default=True must survive the typo (the DRY_RUN_ASANA case)
        with caplog.at_level(logging.WARNING, logger="config.settings"):
            assert eb("_CAE_TEST_BOOL", True) is True, (
                f"typo {typo!r} flipped True default to False — UNSAFE"
            )
        # default=False survives too (no spurious flip in either direction)
        assert eb("_CAE_TEST_BOOL", False) is False


def test_env_bool_unset_uses_default(monkeypatch):
    eb = settings._env_bool
    monkeypatch.delenv("_CAE_TEST_BOOL", raising=False)
    assert eb("_CAE_TEST_BOOL", True) is True
    assert eb("_CAE_TEST_BOOL", False) is False


def test_non_write_sections_info_documents_pending_onboarding():
    # Informational only — Pending Onboarding contracts are excluded from
    # writes by the gate. Test pins the intent so the operator-facing audit
    # output can label these sections as 'skip'.
    assert "Pending Onboarding" in settings.ASANA_NON_WRITE_SECTIONS_INFO


def test_obsolete_constants_removed():
    """Pivot away from Google + n8n + engine-sends-email — these names must not
    come back accidentally on a refactor. Pinning the absence here means a
    future copy-paste that resurrects an obsolete constant fails CI.
    """
    obsolete = (
        "GOOGLE_DASHBOARD_SHEET_ID",
        "GOOGLE_DRIVE_INBOX_FOLDER_ID",
        "GOOGLE_CAPITAL_BREAKDOWN_SHEET_ID",
        "SHEET_TAB_DASHBOARD",
        "SHEET_TAB_NEEDS_TAGGING",
        "SHEET_TAB_VENDOR_ALIASES",
        "SHEET_TAB_CAMPUS_MAP",
        "SHEET_TAB_LEARNED_MAPPINGS",
        "SHEET_TAB_STATE",
        "SHEET_TAB_RUN_LOG",
        "SHEET_TAB_REVIEW",
        "ALERT_RECIPIENTS",
        "INCLUDE_PM_EMAIL",
        "ASANA_LIVE_SECTIONS",          # dropped — only the write gate matters
        "ASANA_WRITE_GATE_SECTIONS",    # renamed singular
        "ASANA_ALARM_OPTIONS",          # renamed to ASANA_SPENDING_RATE_ALARM_OPTIONS
    )
    for name in obsolete:
        assert not hasattr(settings, name), (
            f"obsolete constant {name!r} reappeared on settings — it was "
            f"removed in the Airtable pivot and must not return"
        )
