"""Tests for engine.asana_contracts — task → Contract translation.

Exercises the v5 Asana custom-field shapes (enum_value vs multi_enum_values
vs date_value vs number_value vs text_value) so a future SDK shape change
trips a clear test rather than a silent attribution miss.
"""

from __future__ import annotations

from datetime import date

import pytest

from config import settings
from engine.asana_contracts import Contract, task_to_contract


def _task(**overrides) -> dict:
    """Synthetic Asana task payload mirroring the v5 API shape."""
    base = {
        "gid": "1234567890",
        "name": "Acme SaaS",
        "due_on": "2026-12-31",
        "completed": False,
        "memberships": [
            {
                "project": {"gid": settings.ASANA_PROJECT_GID},
                "section": {"gid": "secX", "name": "Active - Compliant"},
            },
            # Multi-project memberships are common; engine must filter to
            # OUR project, not just take memberships[0].
            {
                "project": {"gid": "9999999999"},
                "section": {"gid": "secY", "name": "Some Other Section"},
            },
        ],
        "custom_fields": [
            {
                "gid": settings.ASANA_FIELD_CAMPUS,
                "type": "multi_enum",
                "multi_enum_values": [
                    {"gid": "optCEN", "name": "CEN", "enabled": True},
                    {"gid": "optEDM", "name": "CEN/EDM", "enabled": True},
                    # Disabled options stay selected on the task but the
                    # engine treats them as "operator retired this option";
                    # exclude.
                    {"gid": "optDis", "name": "DisabledOption", "enabled": False},
                ],
            },
            {
                "gid": settings.ASANA_FIELD_CONTRACT_AMOUNT,
                "type": "number",
                "number_value": 50000.0,
            },
            {
                "gid": settings.ASANA_FIELD_TARGET_START,
                "type": "date",
                "date_value": {"date": "2026-01-01"},
            },
            {
                "gid": settings.ASANA_FIELD_CONTRACT_STATUS,
                "type": "enum",
                "enum_value": {"gid": "optActive", "name": "Active", "enabled": True},
            },
            {
                "gid": settings.ASANA_FIELD_EXPIRE_COUNTDOWN,
                "type": "enum",
                "enum_value": None,
            },
            {
                "gid": settings.ASANA_FIELD_PM_EMAIL,
                "type": "text",
                "text_value": "phil.seabrook@life.church",
            },
        ],
    }
    base.update(overrides)
    return base


def test_task_to_contract_extracts_basic_fields():
    c = task_to_contract(_task())
    assert isinstance(c, Contract)
    assert c.gid == "1234567890"
    assert c.name == "Acme SaaS"
    assert c.contract_amount == pytest.approx(50000.0)
    assert c.target_start == date(2026, 1, 1)
    assert c.due_on == date(2026, 12, 31)
    assert c.status == "Active"
    assert c.expire_countdown is None
    assert c.pm_email == "phil.seabrook@life.church"


def test_task_to_contract_filters_to_enabled_multi_enum_options():
    """The Campus multi_enum reading drops DisabledOption — operator removed
    it from the field's option list, so it's retired even if still selected."""
    c = task_to_contract(_task())
    assert c.campus_options == frozenset({"CEN", "CEN/EDM"})


def test_task_to_contract_picks_section_from_correct_project():
    """A task with memberships in multiple projects must surface ONLY the
    Contractor Database project's section. Picking memberships[0] blindly
    would surface the wrong section name."""
    task = _task()
    # Force a Pending Onboarding section in OUR project and reorder so the
    # foreign-project membership is first.
    task["memberships"] = [
        {
            "project": {"gid": "9999999999"},
            "section": {"gid": "secOther", "name": "Foreign Section"},
        },
        {
            "project": {"gid": settings.ASANA_PROJECT_GID},
            "section": {"gid": "secPO", "name": "Pending Onboarding"},
        },
    ]
    c = task_to_contract(task)
    assert c.section_name == "Pending Onboarding"


def test_task_to_contract_handles_missing_custom_fields():
    """A task with no custom_fields at all (a freshly created stub) must not
    crash — every field defaults to None / empty."""
    task = _task()
    task["custom_fields"] = []
    c = task_to_contract(task)
    assert c.campus_options == frozenset()
    assert c.contract_amount is None
    assert c.target_start is None
    assert c.status is None
    assert c.expire_countdown is None
    assert c.pm_email is None


def test_task_to_contract_handles_missing_due_on():
    task = _task()
    task["due_on"] = None
    c = task_to_contract(task)
    assert c.due_on is None


def test_task_to_contract_handles_empty_text_field():
    """An empty text_value ('' vs None) must not become an empty string —
    treating both as None keeps the operator-facing field optional."""
    task = _task()
    for cf in task["custom_fields"]:
        if cf["gid"] == settings.ASANA_FIELD_PM_EMAIL:
            cf["text_value"] = ""
            break
    c = task_to_contract(task)
    assert c.pm_email is None


def test_task_to_contract_handles_no_membership_in_our_project():
    """If somehow a returned task has no membership in our project (e.g.
    deleted between fetches), section_name is None, not a crash."""
    task = _task()
    task["memberships"] = [
        {"project": {"gid": "9999999999"}, "section": {"gid": "x", "name": "Foo"}},
    ]
    c = task_to_contract(task)
    assert c.section_name is None
