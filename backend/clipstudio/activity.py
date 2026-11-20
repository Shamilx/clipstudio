"""Live activity reporting: what a running job is doing *right now*.

The UI (and CLI) only see what is in the database, so the pipeline writes a
human-readable ``activity`` line, a ``heartbeat_at`` timestamp and a short
rolling log into the ``jobs`` row. Deep code (visual analysis, providers) calls
the module-level :func:`note` / :func:`waiting` helpers; they are no-ops when no
job is bound, so library code and tests never need to care.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlparse

LOG_LIMIT = 30
#: Minimum seconds between DB writes for transient / unchanged messages.
MIN_INTERVAL_S = 0.5
TICK_INTERVAL_S = 2.0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _default_writer(job_id: str, activity: str, heartbeat_at: str, log_json: str) -> None:
    from .db import store

    store.set_job_activity(job_id, activity, heartbeat_at, log_json)


class ActivityTracker:
    """Writes throttled activity updates for one job."""

    def __init__(
        self,
        job_id: str,
        *,
        writer: Callable[[str, str, str, str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        min_interval_s: float = MIN_INTERVAL_S,
    ) -> None:
        self.job_id = job_id
        self.stage = ""
        self.log: list[dict[str, str]] = []
        self._writer = writer or _default_writer
        self._clock = clock
        self._min_interval = min_interval_s
        self._last_msg = ""
        self._last_write = float("-inf")
        self._lock = threading.Lock()
        self.writes = 0

    def set(self, msg: str, *, log: bool = True) -> bool:
        """Record ``msg``. Returns True when a DB write happened.

        A changed message that opens a new log line is always written. Transient
        changes (counters, percentages: ``log=False``) and unchanged messages
        are throttled to one write per ``min_interval_s``.
        """
        if not msg:
            return False
        with self._lock:
            now = self._clock()
            changed = msg != self._last_msg
            new_line = log and changed
            if not new_line and now - self._last_write < self._min_interval:
                return False
            if new_line:
                self.log.append({"t": _now_iso(), "stage": self.stage, "msg": msg})
                del self.log[:-LOG_LIMIT]
            self._last_msg = msg
            self._last_write = now
            payload = json.dumps(self.log)
            self.writes += 1
        for listener in list(listeners):
            try:
                listener(msg)
            except Exception:  # noqa: BLE001
                pass
        try:
            self._writer(self.job_id, msg, _now_iso(), payload)
        except Exception:  # noqa: BLE001 - telemetry must never break a job
            return False
        return True


_current: ActivityTracker | None = None
#: Extra observers (the CLI progress bar) told about every message actually written.
listeners: list[Callable[[str], None]] = []


def bind(tracker: ActivityTracker | None) -> None:
    global _current
    _current = tracker


def current() -> ActivityTracker | None:
    return _current


def note(msg: str, *, log: bool = True) -> None:
    if _current is not None:
        _current.set(msg, log=log)


class Waiting:
    """A pending remote call; its label can change while it waits."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.since = time.monotonic()

    def relabel(self, label: str) -> None:
        self.label = label
        self.since = time.monotonic()

    def text(self) -> str:
        return f"{self.label}… waiting {int(time.monotonic() - self.since)}s"


_waiting: contextvars.ContextVar[Waiting | None] = contextvars.ContextVar("waiting", default=None)


def relabel(label: str) -> None:
    """Change the label of the enclosing :func:`waiting` block (else just note it)."""
    handle = _waiting.get()
    if handle is not None:
        handle.relabel(label)
        note(label)
    else:
        note(label)


@contextlib.asynccontextmanager
async def waiting(label: str, *, interval_s: float = TICK_INTERVAL_S):
    """Keep the heartbeat moving ("… waiting 12s") while an awaited call is pending."""
    handle = Waiting(label)
    token = _waiting.set(handle)
    note(label)

    async def tick() -> None:
        while True:
            await asyncio.sleep(interval_s)
            note(handle.text(), log=False)

    task = asyncio.ensure_future(tick())
    try:
        yield handle
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        _waiting.reset(token)


@contextlib.contextmanager
def waiting_sync(label: str, *, interval_s: float = TICK_INTERVAL_S):
    """Thread-based variant of :func:`waiting` for blocking calls."""
    handle = Waiting(label)
    stop = threading.Event()
    note(label)

    def tick() -> None:
        while not stop.wait(interval_s):
            note(handle.text(), log=False)

    thread = threading.Thread(target=tick, daemon=True, name="activity-ticker")
    thread.start()
    try:
        yield handle
    finally:
        stop.set()
        thread.join(timeout=1.0)


# -- naming helpers ----------------------------------------------------------

_SERVICES = {
    "groq": "Groq",
    "nvidia": "NVIDIA",
    "openrouter": "OpenRouter",
    "openai.com": "OpenAI",
    "deepseek": "DeepSeek",
    "together": "Together",
    "localhost": "local server",
    "127.0.0.1": "local server",
}


def service_name(base_url: str | None, fallback: str = "LLM") -> str:
    host = urlparse(base_url or "").netloc.lower()
    for key, name in _SERVICES.items():
        if key in host:
            return name
    return fallback


def describe(provider, *, fallback: bool = False) -> str:
    """ "Groq (gpt-oss-120b)" style label for a provider (or its fallback)."""
    if fallback and getattr(provider, "fallback_model", None):
        url = getattr(provider, "fallback_base_url", None) or getattr(provider, "base_url", None)
        model = provider.fallback_model
    else:
        url, model = getattr(provider, "base_url", None), getattr(provider, "model", "")
    name = service_name(url, fallback=str(getattr(provider, "name", "LLM")).capitalize())
    short = str(model).split("/")[-1]
    return f"{name} ({short})" if short else name
