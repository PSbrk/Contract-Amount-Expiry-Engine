"""Step 1 verification — read-only check that Asana's project matches the GIDs,
option names, and sections the engine is coded against.

Run as:
    python -m engine.audit         (or: python -m engine.main --audit)

Exit codes:
    0  — every expectation passed
    1  — one or more FAIL findings; engine must NOT proceed past Step 1
    2  — environmental error (missing ASANA_PAT, API auth failure, etc.)

Lookups are by GID — a renamed field surfaces as a WARN, not a FAIL, since the
GID is the engine's contract and the rename is visible to the operator.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass

from config import settings
from engine.asana_client import (
    ApiException,
    enumerate_project_custom_fields,
    get_api_client,
    list_sections,
)


@dataclass
class Finding:
    severity: str   # "PASS" | "FAIL" | "WARN"
    category: str   # "field" | "option" | "section"
    name: str
    message: str

    @property
    def is_failure(self) -> bool:
        return self.severity == "FAIL"


def _check_field(
    expected_name: str,
    spec: dict,
    actual_by_gid: dict[str, dict],
) -> list[Finding]:
    findings: list[Finding] = []
    expected_gid = spec["gid"]
    actual = actual_by_gid.get(expected_gid)

    if actual is None:
        findings.append(Finding(
            "FAIL", "field", expected_name,
            f"expected custom field '{expected_name}' (gid {expected_gid}) "
            f"not present on project {settings.ASANA_PROJECT_GID}",
        ))
        return findings

    field_has_failure = False
    if actual["type"] != spec["type"]:
        field_has_failure = True
        findings.append(Finding(
            "FAIL", "field", expected_name,
            f"type mismatch on gid {expected_gid}: expected {spec['type']}, got {actual['type']}",
        ))

    if actual["name"] != expected_name:
        findings.append(Finding(
            "WARN", "field", expected_name,
            f"renamed in Asana: gid {expected_gid} now shows as '{actual['name']}'. "
            f"Engine still works (lookup is by gid) but worth checking.",
        ))

    if not field_has_failure:
        findings.append(Finding(
            "PASS", "field", expected_name,
            f"gid={expected_gid} type={actual['type']}",
        ))

    expected_options = spec.get("expected_options") or {}
    if expected_options:
        actual_by_opt_gid = {o["gid"]: o for o in (actual.get("enum_options") or [])}
        for opt_name, opt_gid in expected_options.items():
            actual_opt = actual_by_opt_gid.get(opt_gid)
            label = f"{expected_name}/{opt_name}"
            if actual_opt is None:
                findings.append(Finding(
                    "FAIL", "option", label,
                    f"option gid {opt_gid} not present on field gid {expected_gid}",
                ))
            elif not actual_opt.get("enabled", True):
                findings.append(Finding(
                    "FAIL", "option", label,
                    f"option '{opt_name}' (gid {opt_gid}) is disabled — engine "
                    f"cannot set it without re-enabling it in Asana",
                ))
            elif actual_opt["name"] != opt_name:
                findings.append(Finding(
                    "WARN", "option", label,
                    f"option renamed: gid {opt_gid} now shows as '{actual_opt['name']}' "
                    f"(engine writes by gid; rename is non-fatal)",
                ))
            else:
                findings.append(Finding("PASS", "option", label, f"gid={opt_gid}"))

    return findings


def run_audit(api_client) -> tuple[list[Finding], dict[str, str]]:
    """Return (findings, sections) — sections is {name: gid} on the project,
    surfaced separately so print_report can show the full topology.
    """
    findings: list[Finding] = []

    actual_fields_by_gid = {
        f["gid"]: f for f in enumerate_project_custom_fields(api_client)
    }
    for name, spec in settings.ASANA_EXPECTED_READ_FIELDS.items():
        findings.extend(_check_field(name, spec, actual_fields_by_gid))
    for name, spec in settings.ASANA_EXPECTED_WRITE_FIELDS.items():
        findings.extend(_check_field(name, spec, actual_fields_by_gid))

    sections = list_sections(api_client)
    gate = settings.ASANA_WRITE_GATE_SECTION
    if gate in sections:
        findings.append(Finding(
            "PASS", "section", gate, f"gid={sections[gate]}",
        ))
    else:
        findings.append(Finding(
            "FAIL", "section", gate,
            f"section '{gate}' not present on project "
            f"{settings.ASANA_PROJECT_GID}; engine cannot apply the live gate. "
            f"Sections found: {sorted(sections)}",
        ))

    return findings, sections


_MARKERS = {"PASS": "[ ok ]", "FAIL": "[FAIL]", "WARN": "[warn]"}


def print_report(findings: list[Finding], sections: dict[str, str]) -> int:
    pass_count = sum(1 for f in findings if f.severity == "PASS")
    fail_count = sum(1 for f in findings if f.severity == "FAIL")
    warn_count = sum(1 for f in findings if f.severity == "WARN")

    print(f"Asana audit — project {settings.ASANA_PROJECT_GID}")
    print("-" * 72)
    print(f"sections discovered ({len(sections)}):")
    gate = settings.ASANA_WRITE_GATE_SECTION
    non_write = set(settings.ASANA_NON_WRITE_SECTIONS_INFO)
    for name, gid in sorted(sections.items()):
        if name == gate:
            marker = "[gate] "
        elif name in non_write:
            marker = "[skip] "
        else:
            marker = "       "
        print(f"  {marker}{name:50s}  {gid}")
    print("-" * 72)
    for f in findings:
        print(f"{_MARKERS[f.severity]}  {f.category:8s}  {f.name:40s}  {f.message}")
    print("-" * 72)
    print(f"summary: {pass_count} pass, {fail_count} fail, {warn_count} warn")
    return 1 if fail_count else 0


def _configure_logging() -> None:
    """Cap urllib3 + asana at INFO so a stray DEBUG cannot leak Bearer PATs
    via the SDK's request-header logging.
    """
    logging.getLogger("urllib3").setLevel(logging.INFO)
    logging.getLogger("asana").setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify Asana project schema matches the engine's expectations."
    )
    parser.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    _configure_logging()

    try:
        api_client = get_api_client()
    except RuntimeError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    try:
        findings, sections = run_audit(api_client)
    except ApiException as exc:
        # exc.body is the Asana JSON error response. Asana does NOT echo bearer
        # tokens or Authorization headers in error bodies, so this is safe to
        # print. If you copy this pattern into a new exception handler in a
        # later build step, verify the new exception's payload is similarly
        # token-free before printing it.
        print(f"FATAL: Asana API error {exc.status}: {exc.body}", file=sys.stderr)
        return 2

    return print_report(findings, sections)


if __name__ == "__main__":
    sys.exit(main())
