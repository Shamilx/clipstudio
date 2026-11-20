"""Cheap heuristic pre-score for clip candidates, plus the blend and quality gate.

The LLM score stays the main signal; the heuristic (speech density, numbers,
punctuation energy, length) only nudges it, so a good clip the model under-rated
still has a chance and a padded one is demoted a little.
"""

from __future__ import annotations

import re

from .transcript import Word

_NUMBER = re.compile(r"\d|[%$€£₼]|\b(?:AZN|USD|EUR|manat|dollar|million|billion)\b", re.IGNORECASE)


def _trapezoid(value: float, low: float, ideal_low: float, ideal_high: float, high: float) -> float:
    """1 inside the ideal band, falling linearly to 0 at the outer edges."""
    if value <= low or value >= high:
        return 0.0
    if value < ideal_low:
        return (value - low) / (ideal_low - low)
    if value > ideal_high:
        return (high - value) / (high - ideal_high)
    return 1.0


def heuristic_score(
    words: list[Word], *, min_duration_s: float = 20.0, max_duration_s: float = 60.0
) -> int:
    """Score 0-100 from words/sec, number share, ! and ? share and duration fit."""
    if not words:
        return 0
    duration = max(0.1, words[-1].end - words[0].start)
    rate = len(words) / duration
    density = _trapezoid(rate, 0.8, 2.0, 3.4, 5.0)
    numbers = sum(1 for w in words if _NUMBER.search(w.text)) / len(words)
    energy = sum(1 for w in words if w.text.rstrip().endswith(("!", "?"))) / len(words)
    span = max_duration_s - min_duration_s
    length = _trapezoid(
        duration,
        min_duration_s * 0.6,
        min_duration_s + 0.1 * span,
        min_duration_s + 0.6 * span,
        max_duration_s * 1.2,
    )
    score = 100 * (
        0.35 * density
        + 0.25 * min(1.0, numbers * 12)
        + 0.15 * min(1.0, energy * 15)
        + 0.25 * length
    )
    return round(max(0.0, min(100.0, score)))


def blend(llm: int, heuristic: int, weight: float) -> int:
    weight = max(0.0, min(1.0, weight))
    return round((1 - weight) * llm + weight * heuristic)


def quality_gate(scored: list, *, threshold: float, min_keep: int) -> list:
    """Keep the best ``min_keep`` always; the rest only at or above ``threshold``.

    ``scored`` items need a ``score`` attribute and must be sorted best-first.
    """
    return [c for i, c in enumerate(scored) if i < min_keep or c.score >= threshold]
