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
    [
        "a",
        "an",
        "the",
        "this",
        "that",
        "it",
        "is",
        "was",
        "are",
        "what",
        "why",
        "how",
        "who",
        "when",
        "so",
        "just",
        "really",
        "very",
        "wow",
        "oh",
        "yes",
        "no",
        "ok",
        "okay",
        "well",
        "hey",
        "look",
        "watch",
        "see",
        "wait",
        "here",
        "there",
    ]
)


def _norm(text: str) -> str:
    text = text.replace("’", "'")
    return " ".join(re.sub(r"[^\w\s']", " ", text.lower()).split())


_SMALL = frozenset(
    ["a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or", "vs", "with"]
)


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


#: Words a complete hook never ends on (a cut-off phrase), plus abbreviations.
DANGLING_WORDS = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "nor",
        "than",
        "as",
        "if",
        "because",
        "while",
        "will",
        "would",
        "has",
        "have",
        "had",
        "be",
        "been",
        "being",
        "his",
        "my",
        "your",
        "our",
        "their",
        "its",
        "vs",
        "v",
        "st",
        "mr",
        "mrs",
        "ms",
        "dr",
        "jr",
        "sr",
        "mt",
        "ft",
        "etc",
    ]
)
#: Prepositions that dangle in a statement ("He wanted to") but are fine at the end
#: of a question ("Who is the last gift for?").
DANGLING_UNLESS_QUESTION = frozenset(
    ["of", "to", "in", "on", "at", "for", "with", "from", "by", "into", "onto"]
)
_EMOJI = re.compile("[🀀-🫿←-⇿⌀-⏿☀-➿⬀-⯿︀-️‍⃣™©®]")
_OPEN_CLOSE = (("(", ")"), ("[", "]"), ("“", "”"), ("«", "»"))


def has_emoji(text: str) -> bool:
    return bool(_EMOJI.search(text or ""))


def validate_hook(text: str, *, transcript: str = "", allow_generic: bool = False) -> str | None:
    """Why ``text`` is not an acceptable on-screen hook, or None when it is fine.

    Hooks are never truncated: an over-long or cut-off hook is rejected so the caller
    can fall back to another candidate. Checks: length (8 words / 64 chars), hashtags,
    @tags, emoji, unbalanced quotes, a dangling last word ("St?", "the", "and", trailing
    comma), generic bait and a copy of the transcript's opening words.
    """
    text = " ".join((text or "").split())
    if not text:
        return "empty"
    if len(text.split()) > HOOK_MAX_WORDS:
        return "too many words"
    if len(text) > HOOK_MAX_CHARS:
        return "too long"
    if "#" in text or re.search(r"(?:^|\s)@\w", text):
        return "hashtag or @tag"
    if _EMOJI.search(text):
        return "emoji"
    if text.count('"') % 2:
        return "unbalanced quotes"
    for open_c, close_c in _OPEN_CLOSE:
        if text.count(open_c) != text.count(close_c):
            return "unbalanced quotes"
    if re.search(r"[,;:\-–—&…/]\s*$", text) or text.endswith("..."):
        return "ends mid-phrase"
    last = re.sub(r"[^\w']", "", text.split()[-1]).lower().strip("'")
    if last in DANGLING_WORDS or (
        last in DANGLING_UNLESS_QUESTION and not text.rstrip().endswith("?")
    ):
        return "ends on a dangling word"
    if not allow_generic and is_generic_hook(text):
        return "generic"
    if transcript and _copies_opening(text, transcript):
        return "copies the transcript"
    return None


def _copies_opening(hook: str, transcript: str) -> bool:
    h = _norm(hook).split()
    t = _norm(transcript).split()[:30]
    if len(h) < 3 or len(t) < 3:
        return False
    if h[:4] == t[:4] or (len(h) >= 4 and h[:3] == t[:3] and len(h) <= len(t)):
        return True
    n = len(h)
    return n >= 4 and any(t[i : i + n] == h for i in range(len(t) - n + 1))


def clean_hook(text: str, *, allow_generic: bool = False) -> str:
    """The hook with whitespace normalised, or "" when :func:`validate_hook` rejects it."""
    text = " ".join((text or "").split())
    return text if validate_hook(text, allow_generic=allow_generic) is None else ""


def title_fallback(title: str) -> str:
    """A clip title as the hook of last resort: whole phrase, cut at a word boundary.

    Over-long titles are shortened by dropping trailing words (never characters) until
    the rest is a valid phrase; "" when no prefix of 3+ words qualifies.
    """
    base = " ".join((title or "").split())
    head = re.sub(r"\s*[|:–—-]\s*.*$", "", base) or base
    for candidate in (base, head):
        words = candidate.split()
        for n in range(min(len(words), HOOK_MAX_WORDS), 2, -1):
            phrase = " ".join(words[:n]).rstrip(",;:-")
            if validate_hook(phrase, allow_generic=True) is None:
                return phrase
    return ""


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
            provider._complete(SYSTEM, user, DetectionConfig(temperature=0.6)),
            timeout=LLM_TIMEOUT_S,
        )
        data = extract_json_object(raw)
        hook = clean_hook(str(data.get("hook") or "")) if isinstance(data, dict) else ""
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
    clip_id: str | None = None,
    brief: str = "",
    rules: Any = None,
    description: str = "",
    reason: str = "",
    language: str = "",
) -> tuple[str, str]:
    """Return ``(hook, how)`` where ``how`` is "kept", "regenerated" or "title".

    With an LLM the hook writers' room (:mod:`.hookroom`) writes five candidates, judges
    them and picks the best; the runner-up is stored as ``hook_alt`` when ``clip_id`` is
    given. If the room fails, the old behaviour applies: a valid hook is kept, a bad
    one is regenerated once, then the cleaned title is used. Nothing is ever truncated:
    over-long or cut-off hooks are rejected.
    """
    from . import hookroom

    original = " ".join((hook or "").split())
    ctx = hookroom.HookContext.build(
        title=title, transcript=transcript, scene=scene, tone=tone, brief=brief,
        rules=rules, description=description, reason=reason, language=language,
    )  # fmt: skip
    names = hookroom.brief_names(ctx.brief, ctx.vocabulary)
    hook = hookroom.fix_names(original, names)

    if provider is not None:
        try:
            ctx.examples = await asyncio.to_thread(hookroom.load_examples)
        except Exception as exc:  # noqa: BLE001 - examples are optional
            log.debug("Hook examples unavailable: %s", exc)
        room = await hookroom.run_room(provider, ctx, highlight_hook=hook, title=title)
        if room is not None and room.hook:
            if clip_id:
                await asyncio.to_thread(hookroom.persist, clip_id, room)
            return room.hook, ("kept" if room.hook == original else "regenerated")

    if hook and validate_hook(hook, transcript=transcript) is None:
        return hook, "kept"
    better = await specific_hook(
        provider, title=title, transcript=transcript, scene=scene, rejected=hook, tone=tone
    )
    if better:
        return hookroom.fix_names(better, names), "regenerated"
    fallback = title_fallback(title)
    return (fallback or ""), "title"
