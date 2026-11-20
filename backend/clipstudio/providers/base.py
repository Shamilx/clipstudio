"""LLM provider abstraction.

Highlight detection is the one place ClipStudio asks a language model to make a
judgement call, so the interface is deliberately narrow: a window of transcript
goes in, a validated list of clip candidates comes out.

Two design choices carry most of the weight:

**Word indices, not seconds.** The model returns ``start_word_index`` /
``end_word_index``. Timing is then looked up from measured word timestamps, so a
model that is bad at arithmetic — which they all are — cannot produce a clip
that starts at the wrong moment.

**Validate, then retry with the error.** Small local models produce malformed
JSON often enough that a single retry carrying the actual validation message
turns most failures into successes. That loop lives here so every provider
inherits it.
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

log = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


class ProviderError(RuntimeError):
    """A provider could not produce a usable response."""

    def __init__(self, message: str, *, provider: str = "", hint: str = "") -> None:
        super().__init__(message)
        self.provider = provider
        self.hint = hint

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base}\n\n{self.hint}" if self.hint else base


# --------------------------------------------------------------------------
# Contracts
# --------------------------------------------------------------------------


class ClipCandidate(BaseModel):
    """One proposed clip, as returned by the model."""

    start_word_index: int = Field(default=0, ge=0)
    end_word_index: int = Field(default=0, ge=0)
    #: Optional time-based range (seconds) for moments with no speech, e.g. a
    #: runway walk. When both are set they take precedence over the word indices.
    start_s: float | None = Field(default=None, ge=0)
    end_s: float | None = Field(default=None, ge=0)
    title: str = ""
    hook: str = ""
    score: int = Field(default=50, ge=0, le=100)
    reason: str = ""
    #: YouTube Shorts metadata.
    description: str = ""
    hashtags: list[str] = Field(default_factory=list)
    #: Word indices of emphasis moments (punch-in zoom, highlighted caption word).
    emphasis: list[int] = Field(default_factory=list)

    @field_validator("title", "hook", "reason", "description", mode="before")
    @classmethod
    def _coerce_to_string(cls, value: Any) -> str:
        # Models occasionally return null or a number where text was asked for.
        return "" if value is None else str(value)

    @field_validator("hashtags", mode="before")
    @classmethod
    def _coerce_hashtags(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = re.split(r"[\s,]+", value)
        tags: list[str] = []
        for item in value:
            tag = re.sub(r"[^\w]", "", str(item), flags=re.UNICODE)
            if tag:
                tags.append("#" + tag)
        return tags[:8]

    @field_validator("emphasis", mode="before")
    @classmethod
    def _coerce_emphasis(cls, value: Any) -> list[int]:
        if not isinstance(value, list):
            return []
        out: list[int] = []
        for item in value:
            try:
                number = int(item)
            except (TypeError, ValueError):
                continue
            if number >= 0:
                out.append(number)
        return out[:12]

    @field_validator("score", mode="before")
    @classmethod
    def _coerce_score(cls, value: Any) -> int:
        """Accept floats and 0-1 fractions, which models emit despite the schema."""
        if value is None:
            return 50
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 50
        if 0.0 < number <= 1.0:
            number *= 100
        return max(0, min(100, round(number)))


class ClipCandidates(BaseModel):
    clips: list[ClipCandidate] = Field(default_factory=list)


@dataclass
class TranscriptWindow:
    """A slice of transcript presented to the model.

    ``first_word`` lets a provider work on a window while still reporting
    indices in the full transcript's coordinate space.
    """

    text: str
    first_word: int
    last_word: int
    duration_s: float = 0.0
    speakers: list[str] = field(default_factory=list)
    #: Time span the window covers (the first window starts at 0 and the last
    #: runs to the end of the video, so speech-less stretches at the edges count).
    start_s: float = 0.0
    end_s: float = 0.0

    @property
    def word_count(self) -> int:
        return self.last_word - self.first_word + 1


@dataclass
class DetectionConfig:
    min_duration_s: float = 20.0
    max_duration_s: float = 90.0
    max_clips: int = 10
    heuristic_weight: float = 0.2
    quality_threshold: float = 60.0
    min_keep: int = 2
    language: str = ""
    #: Prompt file stem in ``clipstudio/prompts/``. Versioned so contributors can
    #: iterate on prompts without touching code.
    prompt_version: str = "highlight_v4"
    temperature: float = 0.3
    #: Free-text campaign brief from the client (see :func:`clean_brief`).
    brief: str = ""
    #: Extra tone/format guidance derived from the extracted campaign rules.
    campaign_note: str = ""
    #: "Generate more" mode: inclusive word-index ranges already used by clips.
    used_ranges: list[tuple[int, int]] = field(default_factory=list)
    #: (title, reason) of clips the user rejected: avoid similar moments.
    disliked: list[tuple[str, str]] = field(default_factory=list)
    #: On-screen descriptions from video frames: (seconds, description, brand_visible).
    visual: list[tuple[float, str, bool]] = field(default_factory=list)


@dataclass
class ProviderStatus:
    name: str
    available: bool
    detail: str = ""
    models: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Base provider
# --------------------------------------------------------------------------


class LLMProvider(ABC):
    """Base class for highlight-detection providers."""

    #: Stable identifier, matching the key used in settings.
    name: str = ""
    #: Whether the provider needs an API key.
    requires_key: bool = True

    def __init__(self, model: str, *, api_key: str | None = None, base_url: str | None = None):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url

    # -- to implement ------------------------------------------------------

    @abstractmethod
    async def _complete(self, system: str, user: str, config: DetectionConfig) -> str:
        """Send one prompt and return the raw response text."""

    @abstractmethod
    async def health_check(self) -> ProviderStatus:
        """Report whether this provider is usable right now."""

    # -- shared behaviour --------------------------------------------------

    async def detect_highlights(
        self, window: TranscriptWindow, config: DetectionConfig
    ) -> ClipCandidates:
        """Ask the model for clip candidates in ``window``, validating the reply.

        One retry is attempted on a schema violation, feeding the validation
        error back so the model can correct itself.
        """
        system = load_prompt(config.prompt_version)
        user = render_window_prompt(window, config)

        raw = await self._complete(system, user, config)
        try:
            return self._parse(raw, window)
        except (ValidationError, ValueError) as first_error:
            log.warning("%s returned invalid JSON; retrying with feedback.", self.name)
            repair = (
                f"{user}\n\n"
                "Your previous response did not match the required schema.\n"
                f"Validation error:\n{first_error}\n\n"
                "Respond again with ONLY the corrected JSON object. No prose, no "
                "markdown fences."
            )
            raw = await self._complete(system, repair, config)
            try:
                return self._parse(raw, window)
            except (ValidationError, ValueError) as second_error:
                raise ProviderError(
                    f"{self.name} returned malformed clip data twice.",
                    provider=self.name,
                    hint=(
                        f"Last validation error: {second_error}\n\n"
                        "Smaller local models struggle with strict JSON. Try a larger "
                        "model, or switch to a hosted provider."
                    ),
                ) from second_error

    def _parse(self, raw: str, window: TranscriptWindow) -> ClipCandidates:
        """Validate a raw response and clamp indices into the window."""
        payload = extract_json_object(raw)
        candidates = ClipCandidates.model_validate(payload)

        cleaned: list[ClipCandidate] = []
        for candidate in candidates.clips:
            if candidate.start_s is not None and candidate.end_s is not None:
                lo = window.start_s
                hi = window.end_s if window.end_s > window.start_s else float("inf")
                a = max(lo, min(candidate.start_s, hi))
                b = max(lo, min(candidate.end_s, hi))
                if b - a < 1.0:
                    continue
                candidate.start_s, candidate.end_s = a, b
                cleaned.append(candidate)
                continue
            candidate.start_s = candidate.end_s = None
            start = max(window.first_word, min(candidate.start_word_index, window.last_word))
            end = max(window.first_word, min(candidate.end_word_index, window.last_word))
            if end <= start:
                # A zero-or-negative-length clip is a model slip, not a candidate.
                continue
            candidate.start_word_index = start
            candidate.end_word_index = end
            cleaned.append(candidate)

        return ClipCandidates(clips=cleaned)


# --------------------------------------------------------------------------
# Prompt handling
# --------------------------------------------------------------------------


def load_prompt(version: str) -> str:
    """Read a versioned prompt file from ``clipstudio/prompts/``."""
    path = PROMPT_DIR / f"{version}.txt"
    if not path.exists():
        raise ProviderError(
            f"Prompt '{version}' not found at {path}.",
            hint="Prompt files live in backend/clipstudio/prompts/ as versioned .txt files.",
        )
    return path.read_text(encoding="utf-8")


#: Campaign briefs are capped so a pasted contract cannot swamp the prompt.
BRIEF_MAX_CHARS = 6000
#: At most this many used ranges / disliked clips are listed per window.
MAX_LISTED_RANGES = 80
MAX_LISTED_DISLIKED = 20


def clean_brief(text: str | None) -> str:
    """Trim a campaign brief and cap it at :data:`BRIEF_MAX_CHARS`."""
    return (text or "").strip()[:BRIEF_MAX_CHARS].strip()


def render_guidance(window: TranscriptWindow, config: DetectionConfig) -> str:
    """Optional prompt sections: campaign brief, used ranges, disliked clips."""
    parts: list[str] = []

    brief = clean_brief(config.brief)
    if brief:
        parts.append(
            "## Campaign rules from the client — follow them when choosing moments "
            "and writing titles/hooks\n"
            "(These come from the client who pays for the clips. They are not part of "
            "the transcript.)\n"
            f"<<<\n{brief}\n>>>\n"
            + (f"{config.campaign_note.strip()}\n" if config.campaign_note else "")
        )

    if config.used_ranges:
        relevant = [
            (a, b)
            for a, b in sorted(config.used_ranges)
            if b >= window.first_word and a <= window.last_word
        ][:MAX_LISTED_RANGES]
        if relevant:
            listed = ", ".join(f"{a}-{b}" for a, b in relevant)
            parts.append(
                "## Already used (do NOT overlap)\n"
                "These word-index ranges are already clips. Do not return a clip that "
                f"overlaps any of them: {listed}\n"
            )

    if config.disliked:
        lines = []
        for title, reason in config.disliked[:MAX_LISTED_DISLIKED]:
            lines.append(f'- "{title}"' + (f" ({reason})" if reason else ""))
        parts.append(
            "## The user disliked these clips — avoid similar moments\n"
            + "\n".join(lines)
            + "\n"
        )

    return "\n".join(parts)


MAX_LISTED_VISUAL = 120


def render_visual(window: TranscriptWindow, config: DetectionConfig) -> str:
    """The "what is on screen" section: descriptions whose time is inside the window."""
    if not config.visual:
        return ""
    lo = window.start_s
    hi = window.end_s if window.end_s > window.start_s else float("inf")
    lines = [
        f"[{int(t) // 60:02d}:{int(t) % 60:02d}] {desc}" + (" (brand visible)" if brand else "")
        for t, desc, brand in config.visual
        if lo <= t <= hi
    ][:MAX_LISTED_VISUAL]
    if not lines:
        return ""
    return (
        "## What is on screen (from video frames)\n"
        "Timestamps are in the video's own time (MM:SS). A moment with no speech can be "
        "returned with start_s / end_s in seconds instead of word indices.\n"
        + "\n".join(lines)
        + "\n"
    )


def render_window_prompt(window: TranscriptWindow, config: DetectionConfig) -> str:
    """Build the user message for one transcript window."""
    speaker_note = ""
    if window.speakers:
        speaker_note = (
            f"\nThis section has {len(window.speakers)} distinct speakers "
            f"({', '.join(window.speakers)}); speaker labels are shown inline.\n"
        )

    guidance = render_guidance(window, config)
    visual = render_visual(window, config)
    if visual:
        guidance = f"{guidance}\n{visual}" if guidance else visual
    if guidance:
        guidance += "\n"

    short_note = ""
    if window.duration_s and window.duration_s < 2.5 * config.max_duration_s:
        short_note = (
            f"This section is only about {window.duration_s:.0f} seconds long: treat it as one "
            "story. Clips may overlap here, but each must start at a different moment and tell "
            "a different angle. Return at least one clip if it contains any story.\n"
        )

    return (
        f"Transcript section, words {window.first_word} to {window.last_word}.\n"
        f"Each word is tagged with its index as [index]word.\n"
        f"{speaker_note}\n"
        f"{guidance}"
        f"Clip length must be between {config.min_duration_s:.0f} and "
        f"{config.max_duration_s:.0f} seconds.\n"
        f"{short_note}"
        f"Return up to {config.max_clips} clips, each a complete story with a payoff; "
        f"never return fragments to reach a count.\n\n"
        f"---\n{window.text}\n---\n\n"
        "Respond with ONLY a JSON object matching the schema. No prose, no markdown fences."
    )


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_THINK_BLOCK = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)


def strip_think(raw: str) -> str:
    """Remove reasoning blocks that thinking models put in front of the answer.

    Handles closed ``<think>...</think>`` blocks and the case where the opening
    tag was swallowed by the server, leaving only a closing ``</think>``.
    """
    text = _THINK_BLOCK.sub("", raw)
    closing = re.search(r"</think(?:ing)?>", text, re.IGNORECASE)
    if closing:
        text = text[closing.end() :]
    return text.strip()


def extract_json_object(raw: str) -> dict[str, Any]:
    """Pull a JSON object out of a model response.

    Handles the three things models do despite being told not to: wrap the JSON
    in markdown fences, prepend an explanatory sentence, and append a trailing
    note. Falls back to brace matching so a stray character outside the object
    doesn't cost a retry round-trip.
    """
    text = strip_think(raw)
    if not text:
        raise ValueError("The provider returned an empty response.")

    if match := _JSON_FENCE.search(text):
        text = match.group(1).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = _json_by_brace_matching(text)

    if isinstance(parsed, list):
        # Some models skip the wrapper object and return the array directly.
        return {"clips": parsed}
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected a JSON object, got {type(parsed).__name__}.")
    return parsed


def _json_by_brace_matching(text: str) -> Any:
    """Extract the first balanced ``{...}`` or ``[...]`` region and parse it."""
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            char = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    return json.loads(text[start : i + 1])

    raise ValueError("No JSON object found in the response.")
