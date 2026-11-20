"""Shared pytest fixtures.

The central concern here is isolation: nothing in the test suite may touch the
user's real ``~/.clipstudio`` directory or their real OS keyring.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

# The package lives under backend/ so the repo layout matches the PRD. Editable
# installs put it on the path already; this keeps a bare `pytest` working too.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

os.environ["CLIPSTUDIO_NO_ENV"] = "1"  # never read the real .env in tests

from clipstudio import config, db, paths, system  # noqa: E402


@pytest.fixture(autouse=True)
def clipstudio_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point CLIPSTUDIO_HOME at a throwaway directory for every test."""
    home = tmp_path / "clipstudio_home"
    monkeypatch.setenv(paths.ENV_HOME, str(home))
    monkeypatch.delenv(paths.LEGACY_ENV_HOME, raising=False)
    for var in ("NVIDIA_API_KEY", "NVIDIA_MODEL", "GROQ_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    db.reset_connections()
    system.report.cache_clear()
    yield home
    db.reset_connections()


@pytest.fixture
def initialised_db(clipstudio_home: Path) -> int:
    """A ClipStudio home with the schema migrated up to date."""
    return db.init()


class FakeKeyring:
    """In-memory stand-in for the OS keyring."""

    def __init__(self, *, failing: bool = False) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.failing = failing

    def get_password(self, service: str, key: str) -> str | None:
        if self.failing:
            raise RuntimeError("keyring backend unavailable")
        return self.store.get((service, key))

    def set_password(self, service: str, key: str, value: str) -> None:
        if self.failing:
            raise RuntimeError("keyring backend unavailable")
        self.store[(service, key)] = value

    def delete_password(self, service: str, key: str) -> None:
        if self.failing:
            raise RuntimeError("keyring backend unavailable")
        self.store.pop((service, key), None)


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    """Replace the keyring backend with a working in-memory one."""
    kr = FakeKeyring()
    monkeypatch.setattr(config, "_keyring", lambda: kr)
    return kr


@pytest.fixture
def no_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a machine with no usable keyring backend."""
    monkeypatch.setattr(config, "_keyring", lambda: None)


@pytest.fixture
def failing_keyring(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    """A keyring backend that is present but raises on every operation.

    Distinct from `no_keyring`: this is the machine where a backend exists and
    is broken, which must degrade to the file fallback rather than propagate.
    """
    kr = FakeKeyring(failing=True)
    monkeypatch.setattr(config, "_keyring", lambda: kr)
    return kr
