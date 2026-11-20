"""Row models for the tables in :mod:`clipstudio.db.schema`.

Plain dataclasses rather than Pydantic models: these mirror SQLite rows and are
constructed from trusted database output, so validation would be dead weight.
The API layer converts them to Pydantic response models at the boundary.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

SourceType = Literal["youtube", "upload"]
JobStatus = Literal["queued", "running", "failed", "done", "cancelled"]
ClipStatus = Literal["candidate", "kept", "discarded", "exported"]


#: Allowed values for ``Clip.reject_reason``.
REJECT_REASONS = (
    "ad_or_announcement",
    "boring_no_story",
    "bad_framing",
    "product_not_visible",
    "other",
)

#: Human wording used when telling the LLM what the user disliked.
REJECT_REASON_LABELS = {
    "ad_or_announcement": "an ad / announcement / promo",
    "boring_no_story": "boring, no story or payoff",
    "bad_framing": "bad framing",
    "product_not_visible": "product not visible",
    "other": "other",
}


def new_id() -> str:
    """Generate a short, filesystem-safe, collision-resistant identifier."""
    return uuid.uuid4().hex[:16]


def utcnow() -> str:
    """Current UTC time as an ISO-8601 string — the storage format for all timestamps."""
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Source:
    id: str
    type: SourceType
    path: str
    title: str = ""
    url: str | None = None
    filename: str | None = None
    channel: str | None = None
    duration_s: float = 0.0
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    has_audio: bool = True
    has_video: bool = True
    created_at: str = field(default_factory=utcnow)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Source:
        return cls(
            id=row["id"],
            type=row["type"],
            path=row["path"],
            title=row["title"],
            url=row["url"],
            filename=row["filename"],
            channel=row["channel"],
            duration_s=row["duration_s"],
            width=row["width"],
            height=row["height"],
            fps=row["fps"],
            has_audio=bool(row["has_audio"]),
            has_video=bool(row["has_video"]),
            created_at=row["created_at"],
        )


@dataclass
class Job:
    id: str
    source_id: str
    status: JobStatus = "queued"
    current_stage: str = ""
    progress: float = 0.0
    error: str | None = None
    provider: str = ""
    settings: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)
    started_at: str | None = None
    finished_at: str | None = None
    activity: str = ""
    heartbeat_at: str | None = None
    activity_log: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Job:
        return cls(
            id=row["id"],
            source_id=row["source_id"],
            status=row["status"],
            current_stage=row["current_stage"],
            progress=row["progress"],
            error=row["error"],
            provider=row["provider"],
            settings=json.loads(row["settings_json"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            activity=row["activity"] or "",
            heartbeat_at=row["heartbeat_at"],
            activity_log=json.loads(row["activity_log_json"] or "[]"),
        )


@dataclass
class Transcript:
    job_id: str
    json_path: str
    language: str = ""
    model: str = ""
    has_diarization: bool = False
    word_count: int = 0
    #: "whisper" or "youtube" — which path produced this transcript.
    source: str = "whisper"
    created_at: str = field(default_factory=utcnow)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Transcript:
        return cls(
            job_id=row["job_id"],
            json_path=row["json_path"],
            language=row["language"],
            model=row["model"],
            has_diarization=bool(row["has_diarization"]),
            word_count=row["word_count"],
            source=row["source"],
            created_at=row["created_at"],
        )


@dataclass
class Clip:
    id: str
    job_id: str
    start_s: float
    end_s: float
    rank: int = 0
    start_word: int = 0
    end_word: int = 0
    title: str = ""
    hook: str = ""
    score: int = 0
    reason: str = ""
    status: ClipStatus = "candidate"
    user_trimmed: bool = False
    created_at: str = field(default_factory=utcnow)
    #: YouTube Shorts metadata written by the model.
    description: str = ""
    hashtags: list[str] = field(default_factory=list)
    #: Transcript word indices the model marked as emphasis moments.
    emphasis: list[int] = field(default_factory=list)
    #: Score breakdown: ``score`` is the blend of these two.
    llm_score: int = 0
    heuristic_score: int = 0
    #: Why the user rejected the clip (one of REJECT_REASONS, or "") + free text.
    reject_reason: str = ""
    reject_note: str = ""
    #: Alternative on-screen hook (runner-up of the hook writers' room) and every
    #: judged candidate: ``{"text", "formula", "score", "scores", "rejected"?}``.
    hook_alt: str = ""
    hook_candidates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Clip:
        return cls(
            id=row["id"],
            job_id=row["job_id"],
            start_s=row["start_s"],
            end_s=row["end_s"],
            rank=row["rank"],
            start_word=row["start_word"],
            end_word=row["end_word"],
            title=row["title"],
            hook=row["hook"],
            score=row["score"],
            reason=row["reason"],
            status=row["status"],
            user_trimmed=bool(row["user_trimmed"]),
            created_at=row["created_at"],
            description=row["description"],
            hashtags=json.loads(row["hashtags_json"] or "[]"),
            emphasis=json.loads(row["emphasis_json"] or "[]"),
            llm_score=row["llm_score"],
            heuristic_score=row["heuristic_score"],
            reject_reason=row["reject_reason"],
            reject_note=row["reject_note"],
            hook_alt=row["hook_alt"],
            hook_candidates=json.loads(row["hook_candidates_json"] or "[]"),
        )


Platform = Literal["youtube", "tiktok", "instagram"]
PLATFORMS = ("youtube", "tiktok", "instagram")


@dataclass
class ClipResult:
    """Performance of a posted clip on one platform, with the hook that was used."""

    id: str
    clip_id: str
    platform: Platform
    hook: str = ""
    formula: str = ""
    views: int = 0
    #: YouTube Shorts "viewed vs swiped away" (percent viewed), 0-100.
    viewed_pct: float | None = None
    #: Average view duration as a percent of the clip length, 0-100.
    avg_view_pct: float | None = None
    days_live: int = 1
    recorded_at: str = field(default_factory=utcnow)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ClipResult:
        return cls(
            id=row["id"],
            clip_id=row["clip_id"],
            platform=row["platform"],
            hook=row["hook"],
            formula=row["formula"],
            views=row["views"],
            viewed_pct=row["viewed_pct"],
            avg_view_pct=row["avg_view_pct"],
            days_live=row["days_live"],
            recorded_at=row["recorded_at"],
        )


@dataclass
class ClipEdit:
    clip_id: str
    edited_words: list[dict[str, Any]] | None = None
    caption_style: str = "yellow_pop"
    ratio: str = "9:16"
    updated_at: str = field(default_factory=utcnow)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ClipEdit:
        raw = row["edited_words_json"]
        return cls(
            clip_id=row["clip_id"],
            edited_words=json.loads(raw) if raw else None,
            caption_style=row["caption_style"],
            ratio=row["ratio"],
            updated_at=row["updated_at"],
        )


@dataclass
class Export:
    id: str
    clip_id: str
    path: str
    ratio: str = "9:16"
    style: str = "yellow_pop"
    size_bytes: int = 0
    created_at: str = field(default_factory=utcnow)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Export:
        return cls(
            id=row["id"],
            clip_id=row["clip_id"],
            path=row["path"],
            ratio=row["ratio"],
            style=row["style"],
            size_bytes=row["size_bytes"],
            created_at=row["created_at"],
        )
