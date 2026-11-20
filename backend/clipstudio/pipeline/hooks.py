"""Hook quality: reject generic on-screen hooks, ask for a specific one, else fall back.

A hook is the short line shown in the first seconds. Generic bait ("Can you believe
this?", "Ready for a wild twist?") says nothing about the clip, so it is filtered in
code, regardless of what the prompts asked for.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

HOOK_MAX_WORDS = 8
HOOK_MAX_CHARS = 64
LLM_TIMEOUT_S = 45.0

#: Phrases that make a hook generic (matched case-insensitively on the normalised text).
GENERIC_PATTERNS = [
    r"can you believe",
    r"(?:you )?(?:won'?t|will not|wont) believe",
    r"wild twist",
    r"plot twist",
    r"wait for it",
    r"watch (?:this )?(?:till|until|to) the end",
    r"stay till the end",
    r"ready for",
    r"this is (?:so |absolutely |totally )?(?:crazy|insane|wild|unreal|amazing|nuts)",
    r"(?:that|this) (?:was|is) (?:so |absolutely )?(?:crazy|insane|wild|unreal|amazing)",
    r"(?:you )?(?:need|have|gotta) to see (?:this|that)",
    r"what (?:just )?happened",
    r"did (?:you|he|she|they) (?:just )?(?:see|do) (?:that|this)",
    r"mind[- ]?blow",
    r"no way+",
    r"hold on",
    r"wait,? what",
    r"nobody (?:saw|expected) (?:this|that) coming",
    r"(?:the )?best (?:scene|moment|part) ever",
    r"you (?:have|gotta|need) to watch",
    r"goosebumps",
    r"(?:oh )?my (?:god|gosh)",
    r"\bomg\b",
    r"\bwow\b",
    r"^(?:[\w'.-]+\s+){1,3}(?:reveal|challenge|moment|scene|clip|showdown|face-?off)s?$",
]
_GENERIC = re.compile("|".join(f"(?:{p})" for p in GENERIC_PATTERNS), re.IGNORECASE)
_FILLER_ONLY = frozenset(
    "a an the this that it is was are what why how who when so just really very wow oh "
    "yes no ok okay well hey look watch see wait here there".split()
)


def _norm(text: str) -> str:
    text = text.replace("’", "'")
    return " ".join(re.sub(r"[^\w\s']", " ", text.lower()).split())


_SMALL = frozenset("a an the of in on at to for and or vs with".split())


def _is_label(text: str) -> bool:
    """Title-Case noun-phrase labels ("Ignored Tooth Consequence") are not hooks."""
    words = [w for w in re.sub(r"[^\w\s']", "", text or "").split() if w.lower() not in _SMALL]
    if len(words) < 2 or re.search(r"[?!.:]", text or ""):
        return False
    return all(w[0].isupper() for w in words)


def is_generic_hook(text: str) -> bool:
    """True for empty, one-word, filler-only or bait-phrase hooks."""
    norm = _norm(text or "")
    if not norm:
        return True
    words = norm.split()
    if len(words) < 2 or all(w in _FILLER_ONLY for w in words):
        return True
    if _is_label(text):
        return True
    return bool(_GENERIC.search(norm))


def clamp_hook(text: str, max_words: int = HOOK_MAX_WORDS) -> str:
    words = " ".join((text or "").split()).split()
    short = " ".join(words[:max_words]).rstrip(",;:-")
    return short[:HOOK_MAX_CHARS].rstrip()


def title_fallback(title: str) -> str:
    """A cleaned clip title as the hook of last resort (shortened, no trailing bait)."""
    cleaned = re.sub(r"\s*[|:–—-]\s*.*$", "", " ".join((title or "").split())) or title
    return clamp_hook(cleaned)


SYSTEM = (
    "You write the on-screen hook (first seconds) of a short vertical video. "
    "Reply with ONLY a JSON object, no prose."
)

USER = """Write ONE hook for this clip: a short dramatic line or question in plain spoken words,
at most {max_words} words, specific to THIS clip (e.g. "He just threatened to knock his teeth out").
NOT a label or title such as "Charlie's Jacket Reveal" or "The Teeth Challenge".
It must name something concrete from the transcript or the scene description: a person's
name, an action, an object, a place, a number or a line of dialogue's key idea.
Never use generic bait such as "can you believe this", "wild twist", "you won't believe",
"wait for it", "watch till the end", "ready for", "this is crazy". No hashtags, emoji,
brand/sales language or @tags. Same language as the transcript.
{tone}
Clip title: {title}
Rejected hook (too generic): {rejected}
Transcript of the clip:
{transcript}
{scene}
Reply: {{"hook": "..."}}"""


async def specific_hook(
    provider: Any,
    *,
    title: str,
    transcript: str,
    scene: str = "",
    rejected: str = "",
    tone: str = "",
) -> str | None:
    """Ask the LLM once for a specific hook; None when it fails or is still generic."""
    if provider is None:
        return None
    from ..providers.base import DetectionConfig, extract_json_object

    user = USER.format(
        max_words=HOOK_MAX_WORDS,
        title=title or "(none)",
        rejected=rejected or "(none)",
        transcript=(transcript or "(no speech)")[:1500],
        scene=(f"Scene descriptions:\n{scene[:700]}" if scene else ""),
        tone=(f"Tone: {tone}." if tone else ""),
    )
    try:
        raw = await asyncio.wait_for(
            provider._complete(SYSTEM, user, DetectionConfig(temperature=0.6)), timeout=LLM_TIMEOUT_S
        )
        data = extract_json_object(raw)
        hook = clamp_hook(str(data.get("hook") or "")) if isinstance(data, dict) else ""
    except Exception as exc:  # noqa: BLE001 - hooks must never fail a job
        log.warning("Specific-hook request failed: %s", exc)
        return None
    return hook if hook and not is_generic_hook(hook) else None


async def ensure_specific_hook(
    hook: str,
    *,
    provider: Any,
    title: str,
    transcript: str,
    scene: str = "",
    tone: str = "",
) -> tuple[str, str]:
    """Return ``(hook, how)`` where ``how`` is "kept", "regenerated" or "title".

    A non-generic hook is kept (clamped to 8 words). A generic one is regenerated
    once with the clip's context; if that fails, the cleaned title is used.
    """
    hook = clamp_hook(hook)
    if hook and not is_generic_hook(hook):
        return hook, "kept"
    better = await specific_hook(
        provider, title=title, transcript=transcript, scene=scene, rejected=hook, tone=tone
    )
    if better:
        return better, "regenerated"
    fallback = title_fallback(title)
    return (fallback or hook), "title"
