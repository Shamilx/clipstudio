"""Campaign rules: turn a pasted clipping brief into structured, enforceable rules.

The brief is free text from the client (e.g. a Whop campaign). One small LLM
call extracts the hard requirements; a regex fallback keeps hashtags when the
model is unavailable. Nothing here may ever fail a job.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import json
import logging
import re
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .providers.base import DetectionConfig, clean_brief, extract_json_object

log = logging.getLogger(__name__)

LLM_TIMEOUT_S = 60.0
MAX_TAGS = 8
MAX_VOCAB = 20
_HASHTAG = re.compile(r"#\w[\w]*", re.UNICODE)
_NO_AD = re.compile(
    r"no[ -]?ad|not (?:look|feel|sound)[^.\n]{0,20}\bad|look(?:s|ing)? like an? ad|"
    r"brand[- ]?safe|no brand|organic|no (?:promo|sponsor)",
    re.IGNORECASE,
)

SYSTEM_PROMPT = (
    "You extract hard posting requirements from a video clipping campaign brief. "
    "Respond with ONLY one JSON object, no prose, no markdown."
)

USER_PROMPT = """Today's date is {today}.

Extract the requirements from this campaign brief as JSON with exactly these keys:
- "required_hashtags": hashtags every post must include (strings starting with #).
- "required_caption_text": sentences/lines that must appear in every post caption (release or streaming info, platforms, credits), worded as the brief wants. If a date or "coming soon" info is already outdated relative to today's date, rewrite it to be accurate (e.g. "Out now on digital") while keeping platforms and hashtags.
- "caption_style": short guidance on caption tone (e.g. "funny and organic").
- "forbidden": short list of things the brief forbids (e.g. "no ad look", "no tagging accounts", "no brand callouts").
- "footage_type": "film" if clips are cut from a movie/series/scripted footage, "talking" for podcasts/interviews/talking heads, "event" for live events/shows, "gaming" for gameplay/streams, else "other".
- "vocabulary": proper nouns that speech recognition may misspell and that appear in the brief: titles, character / actor / artist / brand / place names, exactly as written (max 20).
- "onscreen_text_required": true only if the brief requires text overlaid on the video.
- "warnings": list of brief info that is outdated or contradictory relative to today's date (e.g. "Brief says 'coming November 17' but that date has passed").
Use empty lists / empty string when the brief says nothing. Do not invent requirements.

BRIEF:
<<<
{brief}
>>>"""


class CampaignRules(BaseModel):
    required_hashtags: list[str] = Field(default_factory=list)
    required_caption_text: list[str] = Field(default_factory=list)
    caption_style: str = ""
    forbidden: list[str] = Field(default_factory=list)
    footage_type: Literal["film", "talking", "event", "gaming", "other"] = "other"
    onscreen_text_required: bool = False
    warnings: list[str] = Field(default_factory=list)
    #: Proper nouns (titles, character / actor / brand names) from the brief; fed to
    #: Whisper as a prompt and used to correct misheard names in the transcript.
    vocabulary: list[str] = Field(default_factory=list)

    @property
    def brand_safe(self) -> bool:
        """True when the brief forbids an ad look (no emoji / sfx)."""
        return any(_NO_AD.search(f) for f in self.forbidden)


def _tag(raw: str) -> str:
    tag = re.sub(r"[^\w]", "", raw.strip().lstrip("#"), flags=re.UNICODE)
    return f"#{tag}" if tag else ""


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def fallback_rules(brief: str) -> CampaignRules:
    """Deterministic extraction: hashtags only (preferring a required/caption section)."""
    text = brief or ""
    section = ""
    match = re.search(r"(?im)^.*(?:required|must[- ]use|caption|hashtag).*$((?:\n.*){0,6})", text)
    if match:
        section = match.group(0)
    tags = _HASHTAG.findall(section) or _HASHTAG.findall(text)
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        t = _tag(t)
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return CampaignRules(required_hashtags=out[:MAX_TAGS])


def parse_rules(raw: str) -> CampaignRules:
    data = extract_json_object(raw)
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    footage = str(data.get("footage_type") or "other").strip().lower()
    tags: list[str] = []
    for t in _str_list(data.get("required_hashtags")):
        t = _tag(t)
        if t and t.lower() not in {x.lower() for x in tags}:
            tags.append(t)
    return CampaignRules(
        required_hashtags=tags,
        required_caption_text=_str_list(data.get("required_caption_text")),
        caption_style=str(data.get("caption_style") or "").strip()[:200],
        forbidden=_str_list(data.get("forbidden")),
        footage_type=footage
        if footage in ("film", "talking", "event", "gaming", "other")
        else "other",
        onscreen_text_required=bool(data.get("onscreen_text_required")),
        warnings=_str_list(data.get("warnings")),
        vocabulary=_str_list(data.get("vocabulary"))[:MAX_VOCAB],
    )


async def extract_rules(brief: str, today: date, provider: Any | None = None) -> CampaignRules:
    """Extract rules with the LLM; fall back to regex hashtags. Never raises."""
    brief = clean_brief(brief)
    if not brief:
        return CampaignRules()
    try:
        if provider is None:
            raise RuntimeError("no provider")
        user = USER_PROMPT.format(today=today.isoformat(), brief=brief)
        from . import activity

        async with activity.waiting(f"Reading campaign brief with {activity.describe(provider)}"):
            raw = await asyncio.wait_for(
                provider._complete(SYSTEM_PROMPT, user, DetectionConfig(temperature=0.0)),
                timeout=LLM_TIMEOUT_S,
            )
        rules = parse_rules(raw)
        rules.required_caption_text = _union(
            rules.required_caption_text, required_section_lines(brief)
        )
        return sanitize_rules(rules, today)
    except Exception as exc:  # noqa: BLE001 - never fail the job
        log.warning("Campaign rule extraction failed (%s); using regex fallback.", exc)
        rules = fallback_rules(brief)
        rules.required_caption_text = required_section_lines(brief)
        return sanitize_rules(rules, today)


async def ensure_campaign(
    job: Any, settings: Any, *, today: date | None = None
) -> CampaignRules | None:
    """Extract and store ``job.settings["campaign"]`` once. Returns the rules (or None)."""
    from .db import store

    brief = clean_brief((job.settings or {}).get("brief"))
    if not brief:
        return None
    existing = rules_from_job(job)
    if existing is not None:
        return existing
    provider = None
    try:
        from .providers import build_provider

        provider = build_provider(job.provider or settings.active_provider, settings)
    except Exception as exc:  # noqa: BLE001
        log.warning("No LLM for campaign extraction (%s).", exc)
    rules = await extract_rules(brief, today or date.today(), provider)
    job.settings = {**job.settings, "campaign": rules.model_dump()}
    try:
        store.update_job_settings(job.id, job.settings)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not store campaign rules: %s", exc)
    return rules


_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july", "august",
         "september", "october", "november", "december"]
    )
}  # fmt: skip
_MONTH_RE = "|".join(
    [*_MONTHS, "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sept", "sep", "oct", "nov", "dec"]
)
_DATE_RES = [
    # November 17, 2026 / Nov 17th / November 17
    re.compile(
        rf"\b(?P<m>{_MONTH_RE})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<y>\d{{4}}))?", re.I
    ),
    # 17 November 2026 / 17th of November
    re.compile(
        rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<m>{_MONTH_RE})\b\.?(?:,?\s+(?P<y>\d{{4}}))?",
        re.I,
    ),
]
_ISO_DATE = re.compile(r"(?P<y>\d{4})-(?P<mo>\d{1,2})-(?P<d>\d{1,2})")
_PREORDER = re.compile(r"pre[- ]?order|coming soon|arriving soon|releasing soon", re.I)
_COMING_TO = re.compile(
    r"(?:coming|arriving|releasing|launching|available)\s+(?:soon\s+)?(?:to|on|at)\s+"
    r"(?P<where>[^.\n]*?)\s*(?:on\s+|from\s+)?\b(?:"
    + _MONTH_RE
    + r")\b\.?\s*\d{0,2}(?:st|nd|rd|th)?"
    r"(?:,?\s+\d{4})?",
    re.I,
)
_RELEASED_WARNING = re.compile(
    r"\b(out now|now out|now (?:playing|streaming|available)|already (?:out|released|available)|"
    r"(?:has|have) (?:been )?released|is (?:out|released|available)|passed|in the past|outdated)\b",
    re.I,
)


def _find_dates(text: str, today: date) -> list[date]:
    found: list[date] = []
    for m in _ISO_DATE.finditer(text):
        with contextlib.suppress(ValueError):
            found.append(date(int(m["y"]), int(m["mo"]), int(m["d"])))
    for rx in _DATE_RES:
        for m in rx.finditer(text):
            key = m["m"].lower()
            month = _MONTHS.get(key) or next(
                (v for k, v in _MONTHS.items() if k.startswith(key[:3])), None
            )
            if not month:
                continue
            with contextlib.suppress(ValueError):
                found.append(date(int(m["y"]) if m["y"] else today.year, month, int(m["d"])))
    return found


def sanitize_caption_text(
    lines: list[str], warnings: list[str] | None = None, today: date | None = None
) -> list[str]:
    """Drop / rewrite outdated release lines in the brief's required caption text.

    * lines with "pre-order" / "coming soon" are dropped when their date is before
      ``today`` or when the brief warnings say the title is already out;
    * "coming to <platform> <past date>" becomes "Out now on <platform>".
    """
    today = today or date.today()
    released = any(_RELEASED_WARNING.search(w) for w in (warnings or []))
    out: list[str] = []
    for raw in lines:
        line = " ".join(str(raw).split())
        if not line:
            continue
        dates = _find_dates(line, today)
        past = any(d < today for d in dates)
        if _PREORDER.search(line) and (past or released):
            continue
        m = _COMING_TO.search(line)
        if m and (past or released):
            where = re.sub(r"\s+", " ", m["where"]).strip(" ,-")
            line = (line[: m.start()] + f"Out now on {where}".strip() + line[m.end() :]).strip()
        if line.lower() not in {x.lower() for x in out}:
            out.append(line)
    return out


#: Tags nobody wants on a clip post: body-part / gross words that topical tag
#: generation sometimes produces. Kept deliberately small.
TAG_BLOCKLIST = frozenset(
    [
        "butt",
        "ass",
        "arse",
        "boob",
        "boobs",
        "breast",
        "breasts",
        "nipple",
        "nipples",
        "belly",
        "armpit",
        "armpits",
        "poop",
        "pee",
        "fart",
        "farts",
        "sex",
        "sexy",
        "naked",
        "nude",
        "nudes",
        "penis",
        "vagina",
        "crotch",
        "toilet",
        "vomit",
        "puke",
        "booty",
        "thong",
    ]
)


_BULLET = re.compile(r"^\s*(?:[-*\u2022\u25cf\u25aa>]|\d+[.)])\s+")


def required_section_lines(brief: str) -> list[str]:
    """Lines under any heading containing "Required" (release info, caption text...).

    Deterministic companion to the LLM extraction: the section runs from the heading
    to the next blank line / next heading; bullets (or plain lines) become entries.
    Lines that are only hashtags are skipped (hashtags are handled separately).
    """
    out: list[str] = []
    lines = (brief or "").splitlines()
    i = 0
    while i < len(lines):
        head = lines[i].strip()
        if (
            head
            and "required" in head.lower()
            and "hashtag" not in head.lower()
            and (head.endswith(":") or len(head) < 70)
        ):
            inline = head.split(":", 1)[1].strip() if ":" in head else ""
            if inline and not _BULLET.match(inline):
                out.append(inline)
            j = i + 1
            while j < len(lines):
                raw = lines[j]
                text = _BULLET.sub("", raw).strip()
                if not text:
                    if out and j > i + 1:
                        break
                    j += 1
                    continue
                if not _BULLET.match(raw) and text.endswith(":"):
                    break  # next heading
                if not _BULLET.match(raw) and 0 < text.find(":") < 40:
                    break  # next "Label: value" line
                out.append(text)
                j += 1
            i = j
        else:
            i += 1
    cleaned = []
    for line in out:
        line = " ".join(line.split())
        if re.match(
            r"(?i)(includes?|no|not|do not|don't|must|avoid|never|ensure|make sure|should)", line
        ):
            continue  # an instruction about the post, not text to paste
        if re.match(
            r"(?i)(includes?|no|not|do not|don't|must|avoid|never|ensure|make sure|should)\b", line
        ):
            continue  # an instruction about the post, not text to paste
        if line and re.sub(r"#\w+|\s", "", line):
            cleaned.append(line)
    return cleaned


def _union(a: list[str], b: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for line in [*a, *b]:
        key = " ".join(line.lower().split())
        if key and key not in seen:
            seen.add(key)
            out.append(line)
    return out


def clean_hashtags(required: list[str], topical: list[str], max_topical: int = 4) -> list[str]:
    """Required tags first, then up to ``max_topical`` relevant topical ones.

    Topical tags are dropped when shorter than 3 characters, duplicated, numeric-only
    or on the small blocklist.
    """
    tags: list[str] = []
    seen: set[str] = set()
    for raw in required:
        t = _tag(raw)
        if t and t.lower() not in seen:
            seen.add(t.lower())
            tags.append(t)
    seen_required = list(seen)
    added = 0
    for raw in topical or []:
        t = _tag(raw)
        body = t[1:].lower()
        if len(body) < 3 or body.isdigit() or body in TAG_BLOCKLIST or t.lower() in seen:
            continue
        if any(
            difflib.SequenceMatcher(None, body, r[1:].lower()).ratio() >= 0.85
            for r in tags[: len(seen_required)]
        ):
            continue
        if added >= max_topical:
            break
        seen.add(t.lower())
        tags.append(t)
        added += 1
    return tags


def sanitize_rules(rules: CampaignRules, today: date | None = None) -> CampaignRules:
    """Deterministic fixes applied after (LLM or regex) extraction."""
    rules.required_caption_text = sanitize_caption_text(
        rules.required_caption_text, rules.warnings, today
    )
    rules.required_hashtags = clean_hashtags(rules.required_hashtags, [], 0)
    rules.vocabulary = list(dict.fromkeys(v.strip() for v in rules.vocabulary if v.strip()))[
        :MAX_VOCAB
    ]
    return rules


def merge_metadata(
    rules: CampaignRules, description: str, hashtags: list[str], *, max_topical: int = 4
) -> tuple[str, list[str]]:
    """Required caption lines appended (no duplicates); required hashtags first."""
    description = (description or "").strip()
    lower = " ".join(description.lower().split())
    extra = [
        t
        for t in rules.required_caption_text
        if t.strip() and " ".join(t.lower().split()) not in lower
    ]
    if extra:
        block = "\n".join(extra)
        description = f"{description}\n\n{block}" if description else block
    return description, clean_hashtags(rules.required_hashtags, hashtags, max_topical)


def validate_metadata(
    rules: CampaignRules | None, description: str, hashtags: list[str]
) -> list[str]:
    """Missing campaign requirements in a clip's final metadata (empty = all present)."""
    if rules is None:
        return []
    missing: list[str] = []
    have = {_tag(t).lower() for t in hashtags or []}
    for t in rules.required_hashtags:
        if _tag(t).lower() not in have:
            missing.append(f"missing required hashtag {t}")
    norm = " ".join((description or "").lower().split())
    for line in rules.required_caption_text:
        if line.strip() and " ".join(line.lower().split()) not in norm:
            missing.append(f"missing required caption text: {line.strip()}")
    return missing


def strip_trailing_tags(description: str) -> str:
    """Remove a trailing paragraph / line that consists only of hashtags."""
    lines = (description or "").rstrip().split("\n")
    while lines and lines[-1].strip() and not re.sub(r"#\w+|\s", "", lines[-1]):
        lines.pop()
    return "\n".join(lines).rstrip()


def with_hashtags(description: str, hashtags: list[str]) -> str:
    """Description ending with its hashtags line (after a blank line). Idempotent."""
    body = strip_trailing_tags(description)
    tags = " ".join(hashtags or [])
    if not tags:
        return body
    return f"{body}\n\n{tags}" if body else tags


def enforce_metadata(rules: CampaignRules | None, clip: Any, *, max_topical: int = 4) -> list[str]:
    """Merge, validate, then force-append anything still missing. Returns the problems found."""
    if rules is None:
        return []
    clip.description, clip.hashtags = merge_metadata(
        rules, clip.description, clip.hashtags, max_topical=max_topical
    )
    problems = validate_metadata(rules, clip.description, clip.hashtags)
    for problem in problems:
        if problem.startswith("missing required hashtag "):
            clip.hashtags = [problem.rsplit(" ", 1)[-1], *clip.hashtags]
        elif problem.startswith("missing required caption text: "):
            clip.description = (
                f"{strip_trailing_tags(clip.description)}\n{problem.split(': ', 1)[1]}".strip()
            )
    clip.description = with_hashtags(clip.description, clip.hashtags)
    return problems


def apply_to_clips(rules: CampaignRules | None, clips: list[Any], *, max_topical: int = 4) -> None:
    if rules is None:
        return
    for clip in clips:
        enforce_metadata(rules, clip, max_topical=max_topical)


def rules_from_job(job: Any) -> CampaignRules | None:
    data = (job.settings or {}).get("campaign")
    if not data:
        return None
    try:
        return CampaignRules.model_validate(data)
    except ValidationError:
        return None


def prompt_section(rules: CampaignRules | None) -> str:
    """Extra guidance appended to the highlight prompt's brief section."""
    if rules is None:
        return ""
    parts = []
    if rules.caption_style:
        parts.append(f"Write titles, hooks and descriptions in this tone: {rules.caption_style}.")
    parts.append(
        "Do NOT put hashtags, release dates, streaming/platform info or credits in the hook "
        "or description; they are added automatically."
    )
    return " ".join(parts)


def to_json(rules: CampaignRules) -> str:
    return json.dumps(rules.model_dump(), indent=2, ensure_ascii=False)
