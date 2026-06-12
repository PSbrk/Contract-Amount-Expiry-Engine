"""Read-only wrapper around the Asana Python SDK (v5).

ALL functions in this module are GETs. The audit + main engine import from
here. Writes will live in engine.asana_writer (Step 5) — not here.

SDK notes (v5 footguns):
- Package is "asana" on PyPI. "python-asana" is unrelated and abandoned.
- v5 is a from-scratch rewrite of v3. `asana.Client.access_token(...)` and
  `client.tasks.find_by_project(...)` are gone. Use Configuration +
  ApiClient + per-resource API classes (TasksApi, SectionsApi,
  CustomFieldSettingsApi).
- get_tasks returns only {gid, name, resource_type} by default; opt_fields is
  required to get anything else. For enum custom fields the SELECTED option
  comes back only when you explicitly include `custom_fields.enum_value`
  (+ `.gid`, `.name`) — listing `custom_fields` alone returns enum_value:null.
  TASK_OPT_FIELDS below covers this.
- All list endpoints auto-paginate; iterate and the SDK fetches subsequent
  pages on demand. `limit` (max 100) controls page size; do not pass `offset`.
- Errors raise asana.rest.ApiException; .status holds the HTTP code.
"""

from __future__ import annotations

import os
from typing import Generator

import asana
from asana.rest import ApiException

from config import settings


__all__ = [
    "ApiException",
    "TASK_OPT_FIELDS",
    "get_api_client",
    "enumerate_project_custom_fields",
    "list_sections",
    "iter_open_tasks",
]


# Comma-separated opt_fields covering everything the engine needs from a task.
# CRITICAL: custom_fields.enum_value and custom_fields.multi_enum_values must be
# listed with their sub-paths or selected options come back null.
TASK_OPT_FIELDS: str = ",".join([
    "gid",
    "name",
    "due_on",
    "completed",
    "memberships",
    "memberships.section",
    "memberships.section.gid",
    "memberships.section.name",
    "memberships.project",
    "memberships.project.gid",
    "custom_fields.gid",
    "custom_fields.name",
    "custom_fields.type",
    "custom_fields.resource_subtype",
    "custom_fields.display_value",
    "custom_fields.number_value",
    "custom_fields.text_value",
    "custom_fields.date_value",
    "custom_fields.date_value.date",
    "custom_fields.enum_value",
    "custom_fields.enum_value.gid",
    "custom_fields.enum_value.name",
    "custom_fields.enum_value.enabled",
    "custom_fields.multi_enum_values",
    "custom_fields.multi_enum_values.gid",
    "custom_fields.multi_enum_values.name",
    "custom_fields.multi_enum_values.enabled",
])


def get_api_client(pat: str | None = None) -> asana.ApiClient:
    """Return an authenticated ApiClient.

    Reads ASANA_PAT from the environment when `pat` is not provided. Raises a
    clear error if neither is set rather than producing an unauthenticated
    client that 401s on the first call.
    """
    token = pat if pat is not None else os.environ.get("ASANA_PAT", "").strip()
    if not token:
        raise RuntimeError(
            "ASANA_PAT is not set. Add it to .env locally or to GitHub Actions "
            "secrets for CI runs."
        )
    configuration = asana.Configuration()
    configuration.access_token = token
    return asana.ApiClient(configuration)


def enumerate_project_custom_fields(
    api_client: asana.ApiClient,
    project_gid: str = settings.ASANA_PROJECT_GID,
) -> Generator[dict, None, None]:
    """Yield one dict per custom_field_setting attached to the project.

    Shape per yield:
        {"gid": str, "name": str, "type": str, "resource_subtype": str,
         "enum_options": [{"gid": str, "name": str, "enabled": bool}, ...]}
    """
    cfs_api = asana.CustomFieldSettingsApi(api_client)
    opts = {
        "limit": 100,
        "opt_fields": ",".join([
            "custom_field",
            "custom_field.gid",
            "custom_field.name",
            "custom_field.type",
            "custom_field.resource_subtype",
            "custom_field.enum_options",
            "custom_field.enum_options.gid",
            "custom_field.enum_options.name",
            "custom_field.enum_options.enabled",
        ]),
    }
    for entry in cfs_api.get_custom_field_settings_for_project(project_gid, opts):
        cf = entry["custom_field"]
        yield {
            "gid": cf["gid"],
            "name": cf["name"],
            "type": cf.get("type") or cf.get("resource_subtype"),
            "resource_subtype": cf.get("resource_subtype"),
            "enum_options": cf.get("enum_options") or [],
        }


def list_sections(
    api_client: asana.ApiClient,
    project_gid: str = settings.ASANA_PROJECT_GID,
) -> dict[str, str]:
    """Return {section_name: section_gid} for the project."""
    sections_api = asana.SectionsApi(api_client)
    opts = {"limit": 100, "opt_fields": "gid,name"}
    return {
        s["name"]: s["gid"]
        for s in sections_api.get_sections_for_project(project_gid, opts)
    }


def iter_open_tasks(
    api_client: asana.ApiClient,
    project_gid: str = settings.ASANA_PROJECT_GID,
) -> Generator[dict, None, None]:
    """Iterate every non-completed task in the project with full field payloads.

    Each yielded dict is shaped per TASK_OPT_FIELDS — gid, name, due_on,
    completed, memberships (incl. section name+gid), custom_fields (incl.
    enum_value.gid/.name and multi_enum_values for every enum field).
    """
    tasks_api = asana.TasksApi(api_client)
    opts = {
        "project": project_gid,
        "completed_since": "now",  # Asana idiom: only incomplete tasks
        "limit": 100,
        "opt_fields": TASK_OPT_FIELDS,
    }
    yield from tasks_api.get_tasks(opts)
