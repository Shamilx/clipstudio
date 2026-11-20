"""Vocabulary: proper nouns the transcript must get right.

Sources: the campaign brief (titles, character / actor / brand names) and capitalised
names or on-screen text quoted in the vision descriptions. Used two ways:

* as the Whisper ``prompt`` (biases recognition towards the spellings),
* as a fuzzy post-correction of the transcript words (difflib ratio >= 0.8 and the
  same first letter), so a misheard "Quanta Parker" becomes "Quanah Parker".
"""

from __future__ import annotations

import difflib
import re

from .transcript import Word

MIN_RATIO = 0.8
#: Groq's prompt limit is ~224 tokens; stay well under.
PROMPT_MAX_CHARS = 600
MIN_TOKEN_LEN = 4

_QUOTED = re.compile(r"[\"“'‘]([^\"”'’]{3,40})[\"”'’]")
_CAP_RUN = re.compile(r"\b(?:[A-Z][a-z]{2,}|[A-Z]{3,})(?:\s+(?:[A-Z][a-z]{2,}|[A-Z]{3,})){0,2}\b")
_COMMON = frozenset(
    "the this that they them then there here scene shot frame close wide medium camera view "
    "man woman men women person people group title text card reads reading says shows showing "
    "and with from into onto over under his her their its one two three four five brand visible "
    "yes no clothing action emotion who what whether visible night day outdoor indoor".split()
)


def _norm(text: str) -> str:
    return re.sub(r"[^\w]", "", text.lower(), flags=re.UNICODE)


def names_from_descriptions(descriptions: list[str], limit: int = 20) -> list[str]:
    """Capitalised names and quoted on-screen text found in vision descriptions."""
    found: dict[str, int] = {}
    for desc in descriptions:
        for m in _QUOTED.finditer(desc):
            name = " ".join(m.group(1).split())
            if any(c.isalpha() for c in name):
                found[name.title() if name.isupper() else name] = found.get(name, 0) + 1
        for m in _CAP_RUN.finditer(desc):
            name = m.group(0)
            if all(w.lower() in _COMMON for w in name.split()):
                continue
            if m.start() == 0 and len(name.split()) == 1:  # sentence-initial single word
                continue
            name = name.title() if name.isupper() else name
            found[name] = found.get(name, 0) + 1
    ranked = sorted(found, key=lambda n: (-found[n], n))
    return ranked[:limit]


def merge(*lists: list[str]) -> list[str]:
    """Union keeping order, de-duplicated case-insensitively."""
    seen: set[str] = set()
    out: list[str] = []
    for items in lists:
        for item in items:
            key = _norm(item)
            if key and key not in seen:
                seen.add(key)
                out.append(item.strip())
    return out


def whisper_prompt(vocabulary: list[str]) -> str:
    """Prompt text for Groq Whisper (spelling hints), capped to a safe length."""
    out = ""
    for term in vocabulary:
        nxt = f"{out}, {term}" if out else term
        if len(nxt) > PROMPT_MAX_CHARS:
            break
        out = nxt
    return out


def _split_punct(text: str) -> tuple[str, str, str]:
    m = re.match(r"^(\W*)(.*?)(\W*)$", text, re.DOTALL)
    return (m.group(1), m.group(2), m.group(3)) if m else ("", text, "")


def _similar(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    if not a or not b or a == b or a[0] != b[0]:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= MIN_RATIO


def correct_words(words: list[Word], vocabulary: list[str]) -> int:
    """Fuzzy-correct ``words`` in place towards ``vocabulary``; returns the number changed.

    Multi-word entries are matched as same-length word windows; single tokens of
    multi-word entries (e.g. a surname) and single-word entries match single words
    of at least four letters.
    """
    if not words or not vocabulary:
        return 0
    entries = [e.split() for e in vocabulary if e.strip()]
    exact = {_norm(t) for tokens in entries for t in tokens}
    changed = 0
    consumed: set[int] = set()

    # 1. multi-word windows
    for tokens in sorted((e for e in entries if len(e) > 1), key=len, reverse=True):
        n = len(tokens)
        target = " ".join(tokens)
        for i in range(len(words) - n + 1):
            if any(j in consumed for j in range(i, i + n)):
                continue
            window = words[i : i + n]
            heard = " ".join(_split_punct(w.text)[1] for w in window)
            if _norm(heard) == _norm(target) and heard == target:
                consumed.update(range(i, i + n))
                continue
            if _similar(heard, target):
                for w, tok in zip(window, tokens, strict=True):
                    pre, _, post = _split_punct(w.text)
                    w.text = f"{pre}{tok}{post}"
                consumed.update(range(i, i + n))
                changed += 1

    # 2. single words against every token
    singles = {t for tokens in entries for t in tokens if len(_norm(t)) >= MIN_TOKEN_LEN}
    for i, w in enumerate(words):
        if i in consumed:
            continue
        pre, core, post = _split_punct(w.text)
        if len(_norm(core)) < MIN_TOKEN_LEN or _norm(core) in exact:
            continue
        best, best_ratio = None, 0.0
        for tok in singles:
            if _similar(core, tok):
                ratio = difflib.SequenceMatcher(None, _norm(core), _norm(tok)).ratio()
                if ratio > best_ratio:
                    best, best_ratio = tok, ratio
        if best:
            w.text = f"{pre}{best}{post}"
            changed += 1
    return changed
