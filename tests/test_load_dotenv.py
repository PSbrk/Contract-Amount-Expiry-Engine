"""Tests for engine.main._load_dotenv -- the bundle-vs-dev secrets discovery.

Where this function looks for environment variables decides whether the
operator's ASANA_PAT actually reaches the engine. A regression here is
silent: the engine starts up fine, then fails on the first Asana call
because the PAT is empty. The smoke test path is slow (needs a built
bundle + a network call); these unit tests catch the same regressions
in milliseconds.

The function tries three locations in order:
  1. If sys.frozen is set (PyInstaller bundle): <exe-dir>/config/secrets.env
  2. Otherwise (dev): <cwd>/config/secrets.env
  3. Otherwise: dotenv's default .env search
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from engine import main as engine_main


@pytest.fixture
def fresh_env(monkeypatch):
    """Strip any inherited env vars whose presence could mask the test signal,
    AND ensure the dev-path fallback doesn't load a developer's real .env."""
    for var in ("CONTRACT_ENGINE_TEST_FLAG", "ASANA_PAT"):
        monkeypatch.delenv(var, raising=False)
    yield monkeypatch


def _write_secrets(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_load_dotenv_finds_bundle_secrets_env_when_frozen(
    tmp_path: Path, fresh_env, monkeypatch,
):
    """Simulates a PyInstaller bundle: sys.frozen=True + sys.executable points
    at a path inside the bundle. _load_dotenv must read config/secrets.env
    from the bundle root, NOT from CWD."""
    bundle_dir = tmp_path / "ContractEngine"
    fake_exe = bundle_dir / "EngineApp.exe"
    fake_exe.parent.mkdir(parents=True, exist_ok=True)
    fake_exe.write_bytes(b"PE\x00\x00fake-exe")

    _write_secrets(
        bundle_dir / "config" / "secrets.env",
        "CONTRACT_ENGINE_TEST_FLAG=from-bundle\n",
    )

    # Put CWD somewhere else so we know the bundle-path branch is what fired.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))

    engine_main._load_dotenv()
    import os
    assert os.environ.get("CONTRACT_ENGINE_TEST_FLAG") == "from-bundle"


def test_load_dotenv_finds_cwd_secrets_env_when_not_frozen(
    tmp_path: Path, fresh_env, monkeypatch,
):
    """Dev workflow: not frozen, CWD has config/secrets.env. The cwd-relative
    branch must fire."""
    _write_secrets(
        tmp_path / "config" / "secrets.env",
        "CONTRACT_ENGINE_TEST_FLAG=from-cwd\n",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    engine_main._load_dotenv()
    import os
    assert os.environ.get("CONTRACT_ENGINE_TEST_FLAG") == "from-cwd"


def test_load_dotenv_bundle_path_wins_over_cwd_when_both_present(
    tmp_path: Path, fresh_env, monkeypatch,
):
    """If a developer is somehow running a frozen build with a stray
    config/secrets.env in CWD, the bundle's own copy must take precedence
    -- otherwise the operator's prod PAT could get silently shadowed by a
    dev leftover."""
    bundle_dir = tmp_path / "ContractEngine"
    fake_exe = bundle_dir / "EngineApp.exe"
    fake_exe.parent.mkdir(parents=True, exist_ok=True)
    fake_exe.write_bytes(b"PE\x00\x00fake-exe")
    _write_secrets(
        bundle_dir / "config" / "secrets.env",
        "CONTRACT_ENGINE_TEST_FLAG=from-bundle\n",
    )
    # Stray cwd copy with a different value.
    cwd_dir = tmp_path / "somewhere-else"
    cwd_dir.mkdir()
    _write_secrets(
        cwd_dir / "config" / "secrets.env",
        "CONTRACT_ENGINE_TEST_FLAG=from-cwd\n",
    )

    monkeypatch.chdir(cwd_dir)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))

    engine_main._load_dotenv()
    import os
    assert os.environ.get("CONTRACT_ENGINE_TEST_FLAG") == "from-bundle"


def test_load_dotenv_does_not_override_existing_env_vars(
    tmp_path: Path, fresh_env, monkeypatch,
):
    """Anything already set in os.environ (e.g. from the parent shell or
    Task Scheduler config) must win. secrets.env is a fallback for
    locally-stored secrets, not an override that surprises CI."""
    _write_secrets(
        tmp_path / "config" / "secrets.env",
        "CONTRACT_ENGINE_TEST_FLAG=from-file\n",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONTRACT_ENGINE_TEST_FLAG", "pre-set-from-shell")
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    engine_main._load_dotenv()
    import os
    assert os.environ.get("CONTRACT_ENGINE_TEST_FLAG") == "pre-set-from-shell"


def test_load_dotenv_no_secrets_env_is_a_noop(tmp_path: Path, fresh_env, monkeypatch):
    """No bundle, no CWD secrets.env, no .env -- the function must not raise.
    Dotenv's default fallback handles the missing-file case cleanly."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    # Must not raise.
    engine_main._load_dotenv()
    import os
    assert os.environ.get("CONTRACT_ENGINE_TEST_FLAG") is None
