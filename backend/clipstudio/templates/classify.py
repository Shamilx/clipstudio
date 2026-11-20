"""Keyword heuristic that picks a built-in template from content."""

from __future__ import annotations

import re

_KEYWORDS: dict[str, tuple[str, ...]] = {
    "hype": (
        "money", "million", "billion", "rich", "wealth", "invest", "stock", "crypto",
        "bitcoin", "profit", "income", "business", "hustle", "grind", "discipline",
        "motivation", "success", "mindset", "champion", "workout", "gym",
        "training", "fight", "football", "basketball", "nba", "ufc",
        "never give up", "dollars", "millionaire", "entrepreneur", "sales",
    ),
    "podcast": (
        "podcast", "interview", "guest", "episode", "welcome to the show", "my guest",
        "thanks for having me", "tell me about", "let's talk about", "host", "so you",
        "what do you think", "joined by", "conversation", "panel",
    ),
    "story": (
        "story", "vlog", "yesterday", "one day", "years ago", "when i was", "i remember",
        "travel", "trip", "journey", "my life", "childhood", "so we went", "then she",
        "then he", "and then", "i was walking", "growing up", "diary", "day in",
    ),
    "talk": (
        "research", "study", "science", "scientists", "education", "learn", "lecture",
        "theory", "history", "evidence", "brain", "psychology", "economics", "ideas",
        "knowledge", "universe", "technology", "philosophy", "ted", "explain", "concept",
    ),
}  # fmt: skip
_QUESTION_RE = re.compile(r"\?")


def auto_pick(transcript_text: str, title: str = "") -> str:
    """Return one of talk/podcast/hype/story. Never raises, no network."""
    try:
        text = f"{title} {title} {transcript_text or ''}".lower()
        words = max(1, len(text.split()))
        scores: dict[str, float] = {}
        for name, kws in _KEYWORDS.items():
            scores[name] = sum(text.count(k) for k in kws) * 1000.0 / words
        # Many questions suggest an interviewer; exclamations suggest hype.
        scores["podcast"] += len(_QUESTION_RE.findall(text)) * 400.0 / words
        scores["hype"] += text.count("!") * 300.0 / words
        best = max(scores, key=lambda k: scores[k])
        return best if scores[best] >= 1.0 else "talk"
    except Exception:  # noqa: BLE001
        return "talk"
