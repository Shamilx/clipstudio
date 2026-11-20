"""YouTube channel watch: poll uploads cheaply and enqueue new videos.

Listing uses yt-dlp's flat extraction (one request, no downloads). When a
channel is added, everything it currently lists is recorded as "seen" (the
baseline); only videos that appear afterwards are enqueued.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse

from ..db import connection
from ..db.models import new_id, utcnow

log = logging.getLogger(__name__)

SHORTS_MAX_S = 60
LIST_LIMIT = 15
DEFAULT_INTERVAL_MIN = 60
MIN_INTERVAL_MIN = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id            TEXT PRIMARY KEY,
    url           TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    template      TEXT NOT NULL DEFAULT 'auto',
    interval_min  INTEGER NOT NULL DEFAULT 60,
    min_duration_s REAL,
    max_duration_s REAL,
    include_keywords TEXT NOT NULL DEFAULT '',
    exclude_keywords TEXT NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    added_at      TEXT NOT NULL,
    last_checked  REAL,
    last_error    TEXT,
    enqueued_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS channel_seen (
    channel_id TEXT NOT NULL,
    video_id   TEXT NOT NULL,
    PRIMARY KEY (channel_id, video_id)
);
"""

_HANDLE_RE = re.compile(r"^@?[\w.\-]{2,}$")


class ChannelError(Exception):
    pass


def normalize_channel_url(text: str) -> str:
    """Accept ``@handle``, a channel URL, or a /videos URL; return the /videos URL."""
    text = (text or "").strip()
    if not text:
        raise ChannelError("Enter a channel URL or @handle.")
    if not text.startswith(("http://", "https://")):
        if _HANDLE_RE.match(text):
            return f"https://www.youtube.com/{'@' + text.lstrip('@')}/videos"
        raise ChannelError("That does not look like a YouTube channel.")
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if host not in ("youtube.com", "www.youtube.com", "m.youtube.com"):
        raise ChannelError("That is not a YouTube URL.")
    parts = [p for p in parsed.path.split("/") if p]
    if not parts or parts[0] not in ("channel", "c", "user") and not parts[0].startswith("@"):
        raise ChannelError("Use a channel URL such as https://www.youtube.com/@name")
    base = "/" + "/".join(parts[:2] if parts[0] in ("channel", "c", "user") else parts[:1])
    return f"https://www.youtube.com{base}/videos"


@dataclass
class Filters:
    min_duration_s: float | None = None
    max_duration_s: float | None = None
    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)


@dataclass
class Candidate:
    id: str
    title: str
    url: str
    duration_s: float | None


def split_keywords(text: str | list[str] | None) -> list[str]:
    if isinstance(text, list):
        text = ",".join(text)
    return [k.strip().lower() for k in re.split(r"[,\n]", text or "") if k.strip()]


def parse_listing(info: dict[str, Any], filters: Filters) -> tuple[list[Candidate], list[str]]:
    """Split a flat-playlist result into (videos to enqueue, ids to just mark seen).

    Skips Shorts (under a minute, or /shorts/ URLs), live and upcoming streams,
    and anything outside the duration/keyword filters. Skipped videos are still
    returned in the second list so they are not re-evaluated every check.
    """
    keep: list[Candidate] = []
    skipped: list[str] = []
    for e in (info or {}).get("entries") or []:
        if not isinstance(e, dict) or not e.get("id"):
            continue
        vid = str(e["id"])
        title = str(e.get("title") or "")
        dur = e.get("duration")
        dur = float(dur) if isinstance(dur, (int, float)) else None
        url = str(e.get("url") or e.get("webpage_url") or "")
        if not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={vid}"
        live = e.get("live_status") in ("is_live", "is_upcoming") or e.get("is_live") is True
        low = title.lower()
        bad = (
            live
            or "/shorts/" in url
            or (dur is not None and dur < SHORTS_MAX_S)
            or (
                filters.min_duration_s is not None
                and dur is not None
                and dur < filters.min_duration_s
            )
            or (
                filters.max_duration_s is not None
                and dur is not None
                and dur > filters.max_duration_s
            )
            or (filters.include_keywords and not any(k in low for k in filters.include_keywords))
            or any(k in low for k in filters.exclude_keywords)
        )
        if bad:
            skipped.append(vid)
        else:
            keep.append(Candidate(vid, title, url, dur))
    return keep, skipped


def fetch_listing(url: str) -> dict[str, Any]:
    """Flat, download-free listing of a channel's latest uploads."""
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "playlistend": LIST_LIMIT,
        "ignoreerrors": False,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False) or {}
    except Exception as exc:
        raise ChannelError(f"Could not read the channel: {str(exc)[:200]}") from exc


# -- persistence -------------------------------------------------------------

_COLS = [
    "id",
    "url",
    "title",
    "template",
    "interval_min",
    "min_duration_s",
    "max_duration_s",
    "include_keywords",
    "exclude_keywords",
    "enabled",
    "added_at",
    "last_checked",
    "last_error",
    "enqueued_count",
]


def _ensure() -> None:
    with connection() as conn:
        conn.executescript(_SCHEMA)


def _row(r) -> dict[str, Any]:
    d = {k: r[k] for k in _COLS}
    d["enabled"] = bool(d["enabled"])
    return d


def list_channels() -> list[dict[str, Any]]:
    _ensure()
    with connection() as conn:
        return [_row(r) for r in conn.execute("SELECT * FROM channels ORDER BY added_at")]


def get_channel(channel_id: str) -> dict[str, Any] | None:
    _ensure()
    with connection() as conn:
        r = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    return _row(r) if r else None


def _filters(ch: dict[str, Any]) -> Filters:
    return Filters(
        ch["min_duration_s"],
        ch["max_duration_s"],
        split_keywords(ch["include_keywords"]),
        split_keywords(ch["exclude_keywords"]),
    )


def _mark_seen(channel_id: str, ids: list[str]) -> None:
    with connection() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO channel_seen (channel_id, video_id) VALUES (?, ?)",
            [(channel_id, i) for i in ids],
        )


def seen_ids(channel_id: str) -> set[str]:
    _ensure()
    with connection() as conn:
        return {
            r[0]
            for r in conn.execute(
                "SELECT video_id FROM channel_seen WHERE channel_id = ?", (channel_id,)
            )
        }


def _clean_interval(v: Any) -> int:
    try:
        return max(MIN_INTERVAL_MIN, min(int(v), 24 * 60))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_MIN


def _clean_dur(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def add_channel(
    text: str,
    *,
    template: str = "auto",
    interval_min: Any = DEFAULT_INTERVAL_MIN,
    min_duration_s: Any = None,
    max_duration_s: Any = None,
    include_keywords: str = "",
    exclude_keywords: str = "",
    lister: Callable[[str], dict[str, Any]] = fetch_listing,
) -> dict[str, Any]:
    """Add a channel and baseline its current uploads as already seen."""
    _ensure()
    url = normalize_channel_url(text)
    info = lister(url)
    ids = [str(e["id"]) for e in info.get("entries") or [] if isinstance(e, dict) and e.get("id")]
    cid = new_id()
    title = str(info.get("channel") or info.get("uploader") or info.get("title") or url)
    with connection() as conn:
        if conn.execute("SELECT 1 FROM channels WHERE url = ?", (url,)).fetchone():
            raise ChannelError("That channel is already being watched.")
        conn.execute(
            "INSERT INTO channels (id, url, title, template, interval_min, min_duration_s,"
            " max_duration_s, include_keywords, exclude_keywords, added_at, last_checked)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                cid,
                url,
                title[:120],
                template or "auto",
                _clean_interval(interval_min),
                _clean_dur(min_duration_s),
                _clean_dur(max_duration_s),
                ",".join(split_keywords(include_keywords)),
                ",".join(split_keywords(exclude_keywords)),
                utcnow(),
                time.time(),
            ),
        )
    _mark_seen(cid, ids)
    return get_channel(cid) or {}


def update_channel(channel_id: str, **fields: Any) -> dict[str, Any] | None:
    _ensure()
    if get_channel(channel_id) is None:
        return None
    sets: dict[str, Any] = {}
    if "interval_min" in fields and fields["interval_min"] is not None:
        sets["interval_min"] = _clean_interval(fields["interval_min"])
    for k in ("min_duration_s", "max_duration_s"):
        if k in fields:
            sets[k] = _clean_dur(fields[k])
    for k in ("include_keywords", "exclude_keywords"):
        if fields.get(k) is not None:
            sets[k] = ",".join(split_keywords(fields[k]))
    if fields.get("template"):
        sets["template"] = fields["template"]
    if fields.get("enabled") is not None:
        sets["enabled"] = 1 if fields["enabled"] else 0
    if sets:
        cols = ", ".join(f"{k} = ?" for k in sets)
        with connection() as conn:
            conn.execute(f"UPDATE channels SET {cols} WHERE id = ?", (*sets.values(), channel_id))
    return get_channel(channel_id)


def delete_channel(channel_id: str) -> bool:
    _ensure()
    with connection() as conn:
        cur = conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        conn.execute("DELETE FROM channel_seen WHERE channel_id = ?", (channel_id,))
        return cur.rowcount > 0


def check_channel(
    channel_id: str,
    *,
    lister: Callable[[str], dict[str, Any]] = fetch_listing,
    enqueue: Callable[..., Any] | None = None,
) -> list[Candidate]:
    """List the channel now and enqueue unseen, passing videos. Returns those enqueued."""
    ch = get_channel(channel_id)
    if ch is None:
        return []
    if enqueue is None:
        from ..jobs import batch as _batch

        enqueue = _batch.add_items
    try:
        info = lister(ch["url"])
    except ChannelError as exc:
        _set_checked(channel_id, str(exc))
        return []
    keep, skipped = parse_listing(info, _filters(ch))
    seen = seen_ids(channel_id)
    fresh = [c for c in keep if c.id not in seen]
    # Oldest first so a burst is processed in upload order.
    fresh.reverse()
    if fresh:
        # Re-check: the channel may have been removed while we were listing.
        if get_channel(channel_id) is None:
            return []
        enqueue([c.url for c in fresh], ch["template"], f"channel:{channel_id}")
    _mark_seen(channel_id, [c.id for c in fresh] + [s for s in skipped if s not in seen])
    _set_checked(channel_id, None, added=len(fresh))
    return fresh


def _set_checked(channel_id: str, error: str | None, added: int = 0) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE channels SET last_checked = ?, last_error = ?, enqueued_count = enqueued_count + ?"
            " WHERE id = ?",
            (time.time(), error, added, channel_id),
        )


def due_channels(now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    return [
        c["id"]
        for c in list_channels()
        if c["enabled"] and now - (c["last_checked"] or 0) >= c["interval_min"] * 60
    ]


class ChannelWatcher:
    """Background task that checks due channels once a minute."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="clipstudio-channel-watch")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                for cid in await asyncio.to_thread(due_channels):
                    await asyncio.to_thread(check_channel, cid)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Channel check failed.")
            await asyncio.sleep(60)


watcher = ChannelWatcher()

__all__ = [
    "ChannelError",
    "Filters",
    "add_channel",
    "check_channel",
    "delete_channel",
    "get_channel",
    "list_channels",
    "normalize_channel_url",
    "parse_listing",
    "update_channel",
    "watcher",
    "asdict",
]
