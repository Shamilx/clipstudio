"""Filesystem layout resolution."""

from __future__ import annotations

from pathlib import Path

import pytest
from clipstudio import paths

LEGACY_DIR = ".autoclip"
LEGACY_DB = "autoclip.db"


def test_home_override_is_honoured(clipstudio_home: Path) -> None:
    assert paths.root() == clipstudio_home.resolve()


def test_override_is_read_per_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Resolving lazily rather than caching at import time is what lets tests —
    # and a user changing the env var — take effect without a reload.
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "first"))
    first = paths.root()

    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "second"))

    assert paths.root() != first


def test_defaults_under_the_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Path.home() is patched, not just the env var: without it this test reads
    # the developer's real home directory and passes or fails depending on
    # whether they happen to have a pre-rename install.
    monkeypatch.delenv(paths.ENV_HOME, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert paths.root() == (tmp_path / ".clipstudio").resolve()


class TestLegacyHomeAdoption:
    """Installs from before the rename must survive.

    Media files are far too large to copy silently, so a pre-rename directory is
    adopted in place rather than migrated.
    """

    @pytest.fixture(autouse=True)
    def _isolated_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(paths.ENV_HOME, raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    def test_legacy_directory_is_adopted_when_the_new_one_is_absent(self, tmp_path: Path) -> None:
        (tmp_path / LEGACY_DIR).mkdir()

        assert paths.root() == (tmp_path / LEGACY_DIR).resolve()

    def test_oldest_directory_name_is_still_adopted(self, tmp_path: Path) -> None:
        (tmp_path / ".clipforge").mkdir()

        assert paths.root() == (tmp_path / ".clipforge").resolve()

    def test_legacy_env_var_is_still_honoured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(paths.LEGACY_ENV_HOME, str(tmp_path / "old-var"))

        assert paths.root() == (tmp_path / "old-var").resolve()

    def test_new_env_var_beats_legacy_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(paths.LEGACY_ENV_HOME, str(tmp_path / "old-var"))
        monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "new-var"))

        assert paths.root() == (tmp_path / "new-var").resolve()

    def test_new_directory_wins_when_both_exist(self, tmp_path: Path) -> None:
        (tmp_path / LEGACY_DIR).mkdir()
        (tmp_path / paths.DEFAULT_DIR_NAME).mkdir()

        assert paths.root() == (tmp_path / paths.DEFAULT_DIR_NAME).resolve()

    def test_new_directory_is_used_on_a_clean_machine(self, tmp_path: Path) -> None:
        assert paths.root() == (tmp_path / paths.DEFAULT_DIR_NAME).resolve()

    def test_explicit_override_beats_both(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / LEGACY_DIR).mkdir()
        elsewhere = tmp_path / "somewhere-else"
        monkeypatch.setenv(paths.ENV_HOME, str(elsewhere))

        assert paths.root() == elsewhere.resolve()

    def test_legacy_database_is_adopted_in_place(self, tmp_path: Path) -> None:
        legacy_home = tmp_path / LEGACY_DIR
        legacy_home.mkdir()
        legacy_db = legacy_home / LEGACY_DB
        legacy_db.write_bytes(b"SQLite format 3\x00")

        # Pointing at a fresh clipstudio.db beside it would silently orphan every
        # job the user already has.
        assert paths.db_path() == legacy_db

    def test_new_database_wins_when_both_exist(self, tmp_path: Path) -> None:
        home = tmp_path / LEGACY_DIR
        home.mkdir()
        (home / LEGACY_DB).write_bytes(b"old")
        (home / paths.DB_NAME).write_bytes(b"new")

        assert paths.db_path().name == paths.DB_NAME

    def test_clean_install_uses_the_new_database_name(self, tmp_path: Path) -> None:
        (tmp_path / paths.DEFAULT_DIR_NAME).mkdir()

        assert paths.db_path().name == paths.DB_NAME


def test_ensure_layout_creates_the_tree(clipstudio_home: Path) -> None:
    paths.ensure_layout()

    for directory in (paths.media_dir(), paths.work_dir(), paths.exports_dir()):
        assert directory.is_dir()


def test_ensure_layout_is_idempotent(clipstudio_home: Path) -> None:
    paths.ensure_layout()
    marker = paths.media_dir() / "keep.txt"
    marker.write_text("x", encoding="utf-8")

    paths.ensure_layout()

    assert marker.exists()


def test_all_artifacts_live_under_the_root(clipstudio_home: Path) -> None:
    root = paths.root()
    derived = [
        paths.media_dir(),
        paths.work_dir(),
        paths.exports_dir(),
        paths.db_path(),
        paths.config_path(),
        paths.job_work_dir("job123"),
        paths.source_media_dir("src456"),
    ]

    for path in derived:
        assert root in path.parents


def test_per_job_and_per_source_directories_are_namespaced(clipstudio_home: Path) -> None:
    assert paths.job_work_dir("job123").name == "job123"
    assert paths.job_work_dir("job123").parent == paths.work_dir()
    assert paths.source_media_dir("src456").parent == paths.media_dir()
