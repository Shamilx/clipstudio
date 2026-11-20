"""Cold open: a clip's first spoken word lands right at the start.

Retention in the first second or two decides whether a Short gets pushed, so a
clip must not open on dead air or on a throwaway "so" / "um" / "yeah".

Everything here works on *clip-relative* times (the same timeline as the word
list and edit plan in ``export.make_plan``) and only ever trims the **head**: the
end of the clip never moves, and the clip never drops below the configured
minimum duration. A user-trimmed clip is left alone.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from .transcript import Word

#: The first word should start this soon after the clip start.
MAX_LEAD_S = 0.25
#: Silence kept in front of the first word so its onset is not clipped.
ONSET_LEAD_S = 0.10
#: A weak opener is skipped when the next sentence starts within this window.
FILLER_JUMP_WINDOW_S = 2.0

WEAK_OPENERS = frozenset({"so", "um", "uh", "uhm", "umm", "yeah", "and", "like", "okay", "ok"})

_STRIP = re.compile(r"[^\w]", re.UNICODE)


def is_weak_opener(text: str) -> bool:
    return _STRIP.sub("", text.lower()) in WEAK_OPENERS


def _onset(first: Word, silences: Sequence[tuple[float, float]] | None) -> float:
    """Start of speech: the word start, pushed to the end of a silence that covers it."""
    onset = first.start
    for a, b in silences or ():
        # Word timings can begin a little inside a measured silence.
        if a - 0.02 <= onset < b and b - onset <= 0.5 and b < first.end:
            onset = max(onset, b)
    return onset


def head_trim(
    words: Sequence[Word],
    *,
    clip_duration_s: float,
    min_duration_s: float = 0.0,
    user_trimmed: bool = False,
    silences: Sequence[tuple[float, float]] | None = None,
    keep_start: float = 0.0,
) -> float:
    """Seconds to cut off the front of the clip (0.0 = leave it as is).

    ``words`` and ``silences`` are clip-relative. The trim is capped so the clip
    stays at or above ``min_duration_s`` (a clip already below it is not trimmed).
    ``keep_start`` is where the edit plan already starts the clip (leading air that
    silence trimming removed), so it is not trimmed twice.
    """
    if user_trimmed or not words:
        return 0.0
    allowed = max(0.0, clip_duration_s - max(0.0, min_duration_s))
    if allowed <= 0.0:
        return 0.0

    first_index = 0
    first = words[0]
    # Skip a weak opener when the next sentence begins soon enough.
    if is_weak_opener(first.text):
        for j in range(1, len(words)):
            if words[j - 1].ends_sentence:
                if words[j].start - first.start <= FILLER_JUMP_WINDOW_S:
                    first_index = j
                break
            if words[j].start - first.start > FILLER_JUMP_WINDOW_S:
                break
    jumped = first_index > 0
    target = _onset(words[first_index], silences) - ONSET_LEAD_S

    if jumped:
        trim = target
        if trim > allowed:  # moving to the sentence would break the minimum: skip it
            first_index, trim = 0, _onset(words[0], silences) - ONSET_LEAD_S
    else:
        trim = target
    # Already within the cold-open budget: nothing to do.
    if first_index == 0 and words[0].start - keep_start <= MAX_LEAD_S:
        return 0.0
    trim = min(trim, allowed)
    return round(trim, 3) if trim - keep_start > 0.05 else 0.0


def trim_keep_head(keep: list[tuple[float, float]], trim_s: float) -> list[tuple[float, float]]:
    """Drop the first ``trim_s`` clip-relative seconds from the kept intervals."""
    if trim_s <= 0:
        return list(keep)
    out: list[tuple[float, float]] = []
    for a, b in keep:
        if b <= trim_s:
            continue
        out.append((max(a, trim_s), b))
    return out or list(keep)
