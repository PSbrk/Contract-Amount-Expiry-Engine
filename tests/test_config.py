"""Smoke tests for config — confirms the spec values are intact and importable."""

from config import campus_map, settings


def test_filter_sets_match_spec():
    assert settings.ACCOUNTS_IN_SCOPE == {"63015", "63020", "63040", "63080", "63090"}
    assert settings.DEPTS_IN_SCOPE == {"000", "107"}


def test_writable_field_gids_present():
    assert settings.ASANA_FIELD_SPENT_SO_FAR == "1215629256175944"
    assert settings.ASANA_FIELD_PCT_SPENT == "1215629256175946"
    assert settings.ASANA_FIELD_SPENDING_RATE == "1215629256175948"
    assert settings.ASANA_FIELD_SPENDING_RATE_ALARM == "1215629256175950"


def test_alarm_option_gids_present():
    assert set(settings.ASANA_ALARM_OPTIONS) == {"75%", "90%", "100%", "Over"}


def test_campus_map_overrides():
    assert "***NOR (contract is for OMH)" in campus_map.ASANA_OVERRIDE_TO_TABLEAU
    assert "***TUL (contract is for SBA)" in campus_map.ASANA_OVERRIDE_TO_TABLEAU
    assert "INT" in campus_map.TABLEAU_DROP_CODES
    assert "All Campuses" in campus_map.ASANA_WILDCARD_OPTIONS


def test_pace_and_budget_defaults():
    assert settings.RUNAWAY_PACE == 2.0
    assert settings.PACE_GUARD_DAYS == 30
    assert [band[1] for band in settings.BUDGET_BANDS] == ["75%", "90%", "100%"]
    assert settings.BUDGET_OVER_LABEL == "Over"


def test_dry_run_default_true():
    # During build, the safe default is dry-run.
    assert settings.DRY_RUN_ASANA is True
