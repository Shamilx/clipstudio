"""SQLite schema and migrations.

Migrations are driven by SQLite's ``user_version`` pragma: each entry in
:data:`MIGRATIONS` moves the database forward one version. Applying them is
idempotent, so :func:`migrate` runs safely on every startup.

We use raw ``sqlite3`` rather than an ORM. The schema is six small tables read
by one local process, so an ORM would add a dependency and a layer of
indirection without buying anything.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

_V1 = """
CREATE TABLE sources (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL CHECK (type IN ('youtube', 'upload')),
    url         TEXT,
    filename    TEXT,
    path        TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    channel     TEXT,
    duration_s  REAL NOT NULL DEFAULT 0,
    width       INTEGER,
    height      INTEGER,
    fps         REAL,
    has_audio   INTEGER NOT NULL DEFAULT 1,
    has_video   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE TABLE jobs (
    id            TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    status        TEXT NOT NULL CHECK (status IN ('queued','running','failed','done','cancelled')),
    current_stage TEXT NOT NULL DEFAULT '',
    progress      REAL NOT NULL DEFAULT 0,
    error         TEXT,
    provider      TEXT NOT NULL DEFAULT '',
    settings_json TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT
);
CREATE INDEX idx_jobs_source ON jobs(source_id);
CREATE INDEX idx_jobs_status ON jobs(status);

CREATE TABLE transcripts (
    job_id          TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    json_path       TEXT NOT NULL,
    language        TEXT NOT NULL DEFAULT '',
    model           TEXT NOT NULL DEFAULT '',
    has_diarization INTEGER NOT NULL DEFAULT 0,
    word_count      INTEGER NOT NULL DEFAULT 0,
    source          TEXT NOT NULL DEFAULT 'whisper',
    created_at      TEXT NOT NULL
);

CREATE TABLE clips (
    id           TEXT PRIMARY KEY,
    job_id       TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    rank         INTEGER NOT NULL DEFAULT 0,
    start_s      REAL NOT NULL,
    end_s        REAL NOT NULL,
    start_word   INTEGER NOT NULL DEFAULT 0,
    end_word     INTEGER NOT NULL DEFAULT 0,
    title        TEXT NOT NULL DEFAULT '',
    hook         TEXT NOT NULL DEFAULT '',
    score        INTEGER NOT NULL DEFAULT 0,
    reason       TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'candidate'
                 CHECK (status IN ('candidate','kept','discarded','exported')),
    user_trimmed INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
CREATE INDEX idx_clips_job ON clips(job_id, rank);

CREATE TABLE clip_edits (
    clip_id           TEXT PRIMARY KEY REFERENCES clips(id) ON DELETE CASCADE,
    edited_words_json TEXT,
    caption_style     TEXT NOT NULL DEFAULT 'bold_pop',
    ratio             TEXT NOT NULL DEFAULT '9:16',
    updated_at        TEXT NOT NULL
);

CREATE TABLE exports (
    id         TEXT PRIMARY KEY,
    clip_id    TEXT NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    ratio      TEXT NOT NULL DEFAULT '9:16',
    style      TEXT NOT NULL DEFAULT 'bold_pop',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_exports_clip ON exports(clip_id);
"""


def _migration_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(_V1)


def _migration_v2(conn: sqlite3.Connection) -> None:
    """ClipStudio: Shorts metadata, emphasis marks and score breakdown."""
    conn.executescript(
        """
        ALTER TABLE clips ADD COLUMN description TEXT NOT NULL DEFAULT '';
        ALTER TABLE clips ADD COLUMN hashtags_json TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE clips ADD COLUMN emphasis_json TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE clips ADD COLUMN llm_score INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE clips ADD COLUMN heuristic_score INTEGER NOT NULL DEFAULT 0;
        """
    )


def _migration_v3(conn: sqlite3.Connection) -> None:
    """Reject reasons: why the user discarded a clip (optional)."""
    conn.executescript(
        """
        ALTER TABLE clips ADD COLUMN reject_reason TEXT NOT NULL DEFAULT '';
        ALTER TABLE clips ADD COLUMN reject_note TEXT NOT NULL DEFAULT '';
        """
    )


def _migration_v4(conn: sqlite3.Connection) -> None:
    """Live activity: what a running job is doing right now, plus a rolling log."""
    conn.executescript(
        """
        ALTER TABLE jobs ADD COLUMN activity TEXT NOT NULL DEFAULT '';
        ALTER TABLE jobs ADD COLUMN heartbeat_at TEXT;
        ALTER TABLE jobs ADD COLUMN activity_log_json TEXT NOT NULL DEFAULT '[]';
        """
    )


def _migration_v5(conn: sqlite3.Connection) -> None:
    """Hook variants (alternative hook + judged candidates) and recorded results."""
    conn.executescript(
        """
        ALTER TABLE clips ADD COLUMN hook_alt TEXT NOT NULL DEFAULT '';
        ALTER TABLE clips ADD COLUMN hook_candidates_json TEXT NOT NULL DEFAULT '[]';

        CREATE TABLE clip_results (
            id           TEXT PRIMARY KEY,
            clip_id      TEXT NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
            platform     TEXT NOT NULL CHECK (platform IN ('youtube','tiktok','instagram')),
            hook         TEXT NOT NULL DEFAULT '',
            formula      TEXT NOT NULL DEFAULT '',
            views        INTEGER NOT NULL DEFAULT 0,
            viewed_pct   REAL,
            avg_view_pct REAL,
            days_live    INTEGER NOT NULL DEFAULT 1,
            recorded_at  TEXT NOT NULL
        );
        CREATE INDEX idx_clip_results_clip ON clip_results(clip_id);
        """
    )


#: Ordered migrations. Index + 1 is the resulting ``user_version``.
#: Append only — never edit a migration that has shipped.
MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _migration_v1,
    _migration_v2,
    _migration_v3,
    _migration_v4,
    _migration_v5,
]

SCHEMA_VERSION = len(MIGRATIONS)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply any outstanding migrations. Returns the resulting schema version."""
    current: int = conn.execute("PRAGMA user_version").fetchone()[0]

    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {current} is newer than this ClipStudio "
            f"build supports ({SCHEMA_VERSION}). Upgrade ClipStudio."
        )

    for version in range(current, SCHEMA_VERSION):
        migration = MIGRATIONS[version]
        with conn:
            migration(conn)
            # PRAGMA doesn't accept bound parameters; version is loop-controlled.
            conn.execute(f"PRAGMA user_version = {version + 1}")

    return SCHEMA_VERSION
