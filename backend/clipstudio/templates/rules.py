"""Campaign rules carried by a :class:`~clipstudio.templates.Template`.

Fields (all optional; defaults mean "no rule"):

``max_duration_s`` (float | None, 5-600)
    Hard cap on clip length for the campaign. Video-side enforcement is done by
    the pipeline; use :func:`clamp_duration` for the value to cut to.
``required_hashtags`` (list[str])
    Hashtags every post must carry. Stored as ``#tag``; applied by
    :func:`apply_to_metadata` (and the export pack's description.txt).
``credit_text`` (str)
    e.g. ``"Credit: @creator"``. Appended to the description once.
``watermark_text`` (str | None)
    Text to burn into the video (pipeline-side; not applied to metadata).
"""

from __future__ import annotations

import re
from typing import NamedTuple

from . import Template

__all__ = ["Metadata", "apply_to_metadata", "clamp_duration"]

MAX_TITLE = 100


class Metadata(NamedTuple):
    title: str
    description: str
    hashtags: list[str]


def _tag(t: str) -> str:
    core = re.sub(r"[^\w]", "", str(t), flags=re.UNICODE)
    return f"#{core}" if core else ""


def apply_to_metadata(
    template: Template | None, title: str, description: str, hashtags: list[str] | None
) -> Metadata:
    """Return (title, description, hashtags) with the template's rules applied.

    Required hashtags come first, followed by the clip's own (deduplicated,
    case-insensitive). The credit line is appended to the description unless it
    is already present. Title is trimmed to YouTube's 100-character limit.
    """
    title = (title or "").strip()[:MAX_TITLE]
    description = (description or "").strip()

    tags: list[str] = []
    seen: set[str] = set()
    required = list(template.required_hashtags) if template else []
    for raw in [*required, *(hashtags or [])]:
        tag = _tag(raw)
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            tags.append(tag)

    credit = (template.credit_text or "").strip() if template else ""
    if credit and credit.lower() not in description.lower():
        description = f"{description}\n\n{credit}" if description else credit

    return Metadata(title, description, tags)


def clamp_duration(template: Template | None, duration_s: float) -> float:
    """Clip length to cut to under the template's ``max_duration_s``."""
    cap = template.max_duration_s if template else None
    return min(duration_s, cap) if cap else duration_s
