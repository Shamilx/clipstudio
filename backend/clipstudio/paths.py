"""Filesystem layout for ClipStudio artifacts.

Everything ClipStudio writes lives under a single root (``~/.clipstudio`` by
default). The root is resolved through ``CLIPSTUDIO_HOME`` so tests — and users
who keep media on another drive — can relocate it without touching code.

Layout::

    <root>/
      media/            source videos, one directory per source id
      work/<job_id>/    stage intermediates (audio, transcript, crop path)
      exports/          finished clips
      clipstudio.db     SQLite database
      config.json       settings (secrets live in the OS keyring)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

ENV_HOME = "CLIPSTUDIO_HOME"
#: Older name of the same variable; still honoured so existing setups keep working.
LEGACY_ENV_HOME = "AUTOCLIP_HOME"

DEFAULT_DIR_NAME = ".clipstudio"

#: Earlier names of the data directory, newest first. An install that predates
#: the rename has its media, database, and settings under an old name, and media
#: files are far too large to copy silently - so an old directory is adopted in
#: place when the new one doesn't exist yet.
LEGACY_DIR_NAMES: tuple[str, ...] = (".autoclip", ".clipforge")


def env_value(name: str, legacy: str | None = None) -> str | None:
    """Read an environment variable, falling back to its pre-rename name."""
    value = os.environ.get(name)
    if not value and legacy:
        value = os.environ.get(legacy)
    return value or None


def root() -> Path:
    """Return the ClipStudio home directory.

    Resolved fresh on every call rather than cached at import time, so tests
    that patch ``CLIPSTUDIO_HOME`` take effect without reloading the module.
    """
    override = env_value(ENV_HOME, LEGACY_ENV_HOME)
    if override:
        return Path(override).expanduser().resolve()

    default = Path.home() / DEFAULT_DIR_NAME
    if not default.exists():
        for name in LEGACY_DIR_NAMES:
            legacy = Path.home() / name
            if legacy.is_dir():
                log.info(
                    "Using the existing data directory %s. Rename it to %s when "
                    "convenient; ClipStudio will use whichever it finds.",
                    legacy,
                    default,
                )
                return legacy.resolve()

    return default.resolve()


def media_dir() -> Path:
    return root() / "media"


def work_dir() -> Path:
    return root() / "work"


def exports_dir() -> Path:
    return root() / "exports"


DB_NAME = "clipstudio.db"
LEGACY_DB_NAMES: tuple[str, ...] = ("autoclip.db", "clipforge.db")


def db_path() -> Path:
    """Return the SQLite database path.

    Adopts a database from an earlier release in place rather than starting an
    empty one beside it, which would silently orphan every job the user has.
    """
    base = root()
    current = base / DB_NAME
    if not current.exists():
        for name in LEGACY_DB_NAMES:
            legacy = base / name
            if legacy.is_file():
                return legacy
    return current


def config_path() -> Path:
    return root() / "config.json"


def job_work_dir(job_id: str) -> Path:
    """Return the per-job scratch directory for pipeline stage artifacts."""
    return work_dir() / job_id


def source_media_dir(source_id: str) -> Path:
    """Return the directory holding a source's downloaded/uploaded media."""
    return media_dir() / source_id


def ensure_layout() -> Path:
    """Create the directory tree if absent and return the root.

    Safe to call repeatedly; used on startup and at the top of each CLI command.
    """
    base = root()
    for path in (base, media_dir(), work_dir(), exports_dir()):
        path.mkdir(parents=True, exist_ok=True)
    return base
