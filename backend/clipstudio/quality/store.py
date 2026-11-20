"""Persistence for quality reports: one row per clip, table created idempotently."""

from __future__ import annotations

import json

from ..db import connection

_DDL = """
CREATE TABLE IF NOT EXISTS clip_quality (
    clip_id     TEXT PRIMARY KEY,
    job_id      TEXT NOT NULL,
    export_id   TEXT,
    status      TEXT NOT NULL,
    score       INTEGER NOT NULL,
    report_json TEXT NOT NULL,
    checked_at  TEXT NOT NULL
)
"""


def _ensure(conn) -> None:
    conn.execute(_DDL)


def save(clip_id: str, job_id: str, export_id: str | None, report: dict) -> None:
    with connection() as conn:
        _ensure(conn)
        conn.execute(
            "INSERT OR REPLACE INTO clip_quality VALUES (?,?,?,?,?,?,datetime('now'))",
            (clip_id, job_id, export_id, report["status"], int(report["score"]), json.dumps(report)),
        )


def _row(row) -> dict:
    data = json.loads(row["report_json"])
    data.update(clip_id=row["clip_id"], export_id=row["export_id"], checked_at=row["checked_at"])
    return data


def get(clip_id: str) -> dict | None:
    with connection() as conn:
        _ensure(conn)
        row = conn.execute("SELECT * FROM clip_quality WHERE clip_id = ?", (clip_id,)).fetchone()
    return _row(row) if row else None


def list_for_job(job_id: str) -> list[dict]:
    with connection() as conn:
        _ensure(conn)
        rows = conn.execute("SELECT * FROM clip_quality WHERE job_id = ?", (job_id,)).fetchall()
    return [_row(r) for r in rows]


def save_qa(clip_id: str, job_id: str, export_id: str | None, qa: dict) -> None:
    """Attach the self-QA result (``qa`` key) to the clip's report.

    With no quality report yet, a row with status ``"qa"`` is created so the UI can
    show the QA outcome on its own; a later quality check keeps the ``qa`` key.
    """
    prev = get(clip_id)
    if prev is not None:
        report = {k: v for k, v in prev.items() if k not in ("clip_id", "export_id", "checked_at")}
        report["qa"] = qa
        save(clip_id, job_id, export_id or prev.get("export_id"), report)
        return
    save(
        clip_id, job_id, export_id,
        {"status": "qa", "score": 0, "issues": [], "metrics": {}, "qa": qa},
    )  # fmt: skip
