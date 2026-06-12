"""Import smoke tests.

Catches packaging / import errors at the unit-test layer so they fail in CI
without requiring an ASANA_PAT. Without these, a typo or stray import in
engine/* would only surface when an operator runs `python -m engine.audit`
against the live API.
"""


def test_import_asana_client():
    import engine.asana_client  # noqa: F401


def test_import_audit():
    import engine.audit  # noqa: F401


def test_import_main():
    import engine.main  # noqa: F401


def test_engine_modules_re_export_apiexception():
    """audit catches ApiException via engine.asana_client — pin that surface."""
    from engine.asana_client import ApiException
    from asana.rest import ApiException as DirectApiException
    assert ApiException is DirectApiException
