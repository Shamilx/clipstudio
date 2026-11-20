"""Edit plan: the finishing touches for one clip.

One LLM call per clip returns punch-in zooms, emphasis words, emoji, sound
effects and an on-screen hook, all keyed to word times. The reply is never
trusted: :func:`validate_plan` snaps every time to a real word, enforces the
template's limits and drops anything malformed. If the call fails or times out
(one retry), :func:`heuristic_plan` builds a deterministic plan from the words
alone, so a clip always renders.

All times are clip-relative *source* seconds (before silence/filler cuts); the
renderer maps them onto the edited timeline. That keeps a stored plan valid when
the same clip is re-rendered with another template.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .. import activity
from ..providers.base import DetectionConfig, extract_json_object, load_prompt
from . import hooks
from .captions import HOOK_DURATION_S
from .scoring import _NUMBER
from .transcript import Word

log = logging.getLogger(__name__)

PROMPT_VERSION = "editplan_v1"
LLM_TIMEOUT_S = 60.0
LLM_ATTEMPTS = 2  # first try + one retry

MIN_ZOOM_SCALE = 1.04
ZOOM_DUR_RANGE = (0.8, 1.6)
#: No zoom while the hook card is on screen (follows the hook duration, capped).
LEAD_IN_S = min(HOOK_DURATION_S, 3.0)
MAX_SFX = 6
MIN_SFX_GAP_S = 1.5
MIN_EMOJI_GAP_S = 3.0
MAX_EMPHASIS = 8
SFX_KINDS = ("whoosh", "pop", "ding")
HOOK_MAX_WORDS = 8
SNAP_TOLERANCE_S = 0.6

_STRIP = re.compile(r"[^\w$%]", re.UNICODE)

#: Keyword -> emoji for the deterministic fallback.
EMOJI_KEYWORDS = {
    "money": "\U0001f4b0", "dollars": "\U0001f4b0", "profit": "\U0001f4b0",
    "revenue": "\U0001f4b0", "cash": "\U0001f4b0", "fire": "\U0001f525",
    "hot": "\U0001f525", "love": "❤️", "brain": "\U0001f9e0",
    "mind": "\U0001f92f", "crazy": "\U0001f92f", "insane": "\U0001f92f",
    "secret": "\U0001f92b", "idea": "\U0001f4a1", "growth": "\U0001f4c8",
    "grow": "\U0001f4c8", "scale": "\U0001f4c8", "mistake": "⚠️",
    "warning": "⚠️", "wrong": "⚠️", "win": "\U0001f3c6",
    "best": "\U0001f3c6", "time": "⏰", "fast": "⚡", "stop": "\U0001f6d1",
}  # fmt: skip


@dataclass
class TemplateLimits:
    """The slice of a template the plan depends on."""

    zoom_strength: float = 1.1
    zoom_min_gap_s: float = 4.0
    max_emoji: int = 2
    sfx_enabled: bool = True

    @classmethod
    def of(cls, template: Any, *, brand_safe: bool = False) -> TemplateLimits:
        """``brand_safe`` (job-level campaign rule) forces no emoji and no sfx."""
        return cls(
            zoom_strength=float(getattr(template, "zoom_strength", 1.1)),
            zoom_min_gap_s=float(getattr(template, "zoom_min_gap_s", 4.0)),
            max_emoji=0 if brand_safe else int(getattr(template, "max_emoji", 2)),
            sfx_enabled=False if brand_safe else bool(getattr(template, "sfx_enabled", True)),
        )


@dataclass
class EditPlanData:
    template: str = ""
    source: str = "heuristic"  # "llm" | "heuristic"
    hook_text: str = ""
    zooms: list[dict[str, float]] = field(default_factory=list)  # t, dur, scale
    layout: list[dict[str, Any]] = field(default_factory=list)  # t0, t1, mode (informational)
    emphasis: list[float] = field(default_factory=list)
    emoji: list[dict[str, Any]] = field(default_factory=list)  # t, char
    sfx: list[dict[str, Any]] = field(default_factory=list)  # t, kind

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EditPlanData:
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**known)

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> EditPlanData | None:
        try:
            return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if f == f and abs(f) != float("inf") else default


def available_emoji() -> list[str]:
    """Emoji we have artwork for (from the bundled PNG file names)."""
    try:
        from ..templates.assets import ASSETS  # type: ignore[import-not-found]

        names = sorted((ASSETS / "emoji").glob("emoji_u*.png"))
    except Exception:  # noqa: BLE001 - assets package missing: no emoji
        return []
    out: list[str] = []
    for path in names:
        try:
            out.append("".join(chr(int(h, 16)) for h in path.stem[len("emoji_u") :].split("_")))
        except ValueError:
            continue
    return out


def _has_png(char: str) -> bool:
    try:
        from ..templates.assets import emoji_png  # type: ignore[import-not-found]

        return emoji_png(char) is not None
    except Exception:  # noqa: BLE001
        return False


def _items(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _snap(t: float | None, words: list[Word]) -> float | None:
    """Start time of the word nearest ``t``; None if nothing is close enough."""
    if t is None or not words:
        return None
    best = min(words, key=lambda w: abs(w.start - t))
    return round(best.start, 3) if abs(best.start - t) <= SNAP_TOLERANCE_S else None


def _clean_hook(text: Any) -> str:
    """The hook, or "" when it is over-long / cut off (never truncated: the caller
    falls back to another hook)."""
    # Generic bait is handled later (hooks.ensure_specific_hook); structure only here.
    return hooks.clean_hook(str(text or ""), allow_generic=True)


def _is_emoji(char: Any) -> bool:
    if not isinstance(char, str) or not char or len(char) > 8:
        return False
    return any(ord(c) > 0x2000 for c in char) and not any(c.isalnum() for c in char)


def constrain(plan: EditPlanData, limits: TemplateLimits, duration_s: float) -> EditPlanData:
    """Enforce template limits on an already-validated plan (idempotent)."""
    gap = max(0.0, limits.zoom_min_gap_s)
    zooms: list[dict[str, float]] = []
    last = -1e9
    for z in sorted(plan.zooms, key=lambda z: z["t"]):
        if z["t"] - last < gap or z["t"] < LEAD_IN_S or z["t"] > duration_s - 0.5:
            continue
        scale = min(max(z["scale"], MIN_ZOOM_SCALE), limits.zoom_strength)
        if scale < MIN_ZOOM_SCALE + 0.005:
            continue
        dur = min(z["dur"], max(0.4, duration_s - z["t"]))
        zooms.append({"t": z["t"], "dur": round(dur, 2), "scale": round(scale, 3)})
        last = z["t"]

    emoji: list[dict[str, Any]] = []
    last = -1e9
    # Only emoji we can actually draw count against the cap.
    for e in sorted((e for e in plan.emoji if _has_png(e["char"])), key=lambda e: e["t"]):
        if len(emoji) >= limits.max_emoji:
            break
        if e["t"] - last >= MIN_EMOJI_GAP_S:
            emoji.append(e)
            last = e["t"]

    sfx: list[dict[str, Any]] = []
    if limits.sfx_enabled:
        last = -1e9
        for s in sorted(plan.sfx, key=lambda s: s["t"]):
            if len(sfx) >= MAX_SFX:
                break
            if s["t"] - last >= MIN_SFX_GAP_S:
                sfx.append(s)
                last = s["t"]

    return EditPlanData(
        template=plan.template, source=plan.source, hook_text=plan.hook_text, zooms=zooms,
        layout=plan.layout, emphasis=plan.emphasis[:MAX_EMPHASIS], emoji=emoji, sfx=sfx,
    )  # fmt: skip


def validate_plan(
    raw: Any, words: list[Word], limits: TemplateLimits, duration_s: float
) -> EditPlanData:
    """Turn untrusted model output into a safe plan. Never raises."""
    plan = EditPlanData(source="llm")
    if not isinstance(raw, dict):
        return plan

    plan.hook_text = _clean_hook(raw.get("hook_text"))

    for item in _items(raw.get("zooms")):
        if not isinstance(item, dict):
            continue
        t = _snap(_num(item.get("t")), words)
        scale = _num(item.get("scale"))
        dur = _num(item.get("dur"), 1.2)
        if t is None or scale is None or dur is None:
            continue
        plan.zooms.append(
            {
                "t": t,
                "dur": round(min(max(dur, ZOOM_DUR_RANGE[0]), ZOOM_DUR_RANGE[1]), 2),
                "scale": scale,
            }
        )

    emphasis: list[float] = []
    for item in _items(raw.get("emphasis")):
        t = _snap(_num(item.get("t") if isinstance(item, dict) else item), words)
        if t is not None and t not in emphasis:
            emphasis.append(t)
    plan.emphasis = sorted(emphasis)[:MAX_EMPHASIS]

    for item in _items(raw.get("emoji")):
        if not isinstance(item, dict) or not _is_emoji(item.get("char")):
            continue
        t = _snap(_num(item.get("t")), words)
        if t is not None:
            plan.emoji.append({"t": t, "char": item["char"]})

    for item in _items(raw.get("sfx")):
        if not isinstance(item, dict) or item.get("kind") not in SFX_KINDS:
            continue
        t = _num(item.get("t"))
        # An sfx may lead a word slightly (a whoosh just before a zoom) but must
        # sit inside the clip.
        if t is None or not (0.0 <= t <= duration_s):
            continue
        plan.sfx.append({"t": round(t, 3), "kind": item["kind"]})

    for item in _items(raw.get("layout")):
        if not isinstance(item, dict) or item.get("mode") not in ("speaker", "split"):
            continue
        t0, t1 = _num(item.get("t0")), _num(item.get("t1"))
        if t0 is not None and t1 is not None and 0 <= t0 < t1 <= duration_s + 0.5:
            plan.layout.append({"t0": t0, "t1": t1, "mode": item["mode"]})

    return constrain(plan, limits, duration_s)


# --------------------------------------------------------------------------
# Heuristic fallback
# --------------------------------------------------------------------------


def heuristic_plan(
    words: list[Word],
    duration_s: float,
    limits: TemplateLimits,
    *,
    hook: str = "",
    emphasis_times: list[float] | None = None,
) -> EditPlanData:
    """Deterministic plan: zoom on numbers/emphasis words, spaced out."""
    plan = EditPlanData(source="heuristic", hook_text=_clean_hook(hook))
    emph = sorted(round(t, 3) for t in (emphasis_times or []))
    plan.emphasis = emph[:MAX_EMPHASIS]

    candidates: dict[float, float] = {}  # start time -> priority
    emph_set = {round(t, 2) for t in emph}
    for w in words:
        if w.start < LEAD_IN_S:
            continue
        if _NUMBER.search(w.text):
            candidates[round(w.start, 3)] = 2.0
        elif round(w.start, 2) in emph_set:
            candidates[round(w.start, 3)] = 1.5
    # Sentence openers fill the gaps so a long clip still breathes.
    for prev, w in zip(words, words[1:], strict=False):
        if prev.ends_sentence and w.start >= LEAD_IN_S:
            candidates.setdefault(round(w.start, 3), 0.5)

    strength = limits.zoom_strength
    gap = max(limits.zoom_min_gap_s, 1.5)
    chosen: list[dict[str, float]] = []
    for t, priority in sorted(candidates.items(), key=lambda kv: (-kv[1], kv[0])):
        if all(abs(t - c["t"]) >= gap for c in chosen):
            factor = 1.0 if priority >= 1.5 else 0.7
            chosen.append({"t": t, "dur": 1.2, "scale": round(1 + (strength - 1) * factor, 3)})
    plan.zooms = sorted(chosen, key=lambda z: z["t"])

    last = -1e9
    for w in words:
        char = EMOJI_KEYWORDS.get(_STRIP.sub("", w.text.lower()))
        if char and w.start - last >= 4.0 and w.start >= LEAD_IN_S:
            plan.emoji.append({"t": round(w.start, 3), "char": char})
            last = w.start

    for z in plan.zooms[:4]:
        plan.sfx.append({"t": round(max(0.0, z["t"] - 0.05), 3), "kind": "whoosh"})
    for e in plan.emoji:
        plan.sfx.append({"t": e["t"], "kind": "pop"})
    plan.sfx.sort(key=lambda s: s["t"])
    return constrain(plan, limits, duration_s)


# --------------------------------------------------------------------------
# LLM
# --------------------------------------------------------------------------


def render_user_prompt(
    words: list[Word], title: str, duration_s: float, limits: TemplateLimits
) -> str:
    transcript = " ".join(f"{w.text.strip()}@{w.start:.1f}" for w in words)
    return (
        f"Clip title: {title}\n"
        f"Clip length: {duration_s:.1f} s\n"
        f"Maximum zoom scale: {limits.zoom_strength:.2f}\n"
        f"Minimum gap between zoom starts: {limits.zoom_min_gap_s:.1f} s\n"
        f"Maximum emoji: {limits.max_emoji}\n"
        f"Allowed emoji (use only these): {' '.join(available_emoji()) or 'none'}\n"
        f"Sound effects allowed: {'yes' if limits.sfx_enabled else 'no'}\n\n"
        f"---\n{transcript}\n---\n\nRespond with ONLY the JSON object."
    )


async def plan_clip(
    words: list[Word],
    duration_s: float,
    limits: TemplateLimits,
    *,
    provider: Any | None,
    title: str = "",
    hook: str = "",
    emphasis_times: list[float] | None = None,
    template_name: str = "",
    label: str = "",
) -> EditPlanData:
    """Plan one clip: LLM first, heuristic on any failure. Always returns a plan.

    ``words`` have clip-relative times.
    """
    plan: EditPlanData | None = None
    if provider is not None and words:
        config = DetectionConfig(temperature=0.4)
        system = load_prompt(PROMPT_VERSION)
        user = render_user_prompt(words, title, duration_s, limits)
        for attempt in range(1, LLM_ATTEMPTS + 1):
            try:
                async with activity.waiting(
                    f"Edit plan for {label or 'clip'} ({activity.describe(provider)})"
                    + (f", attempt {attempt}" if attempt > 1 else "")
                ):
                    raw = await asyncio.wait_for(
                        provider._complete(system, user, config), timeout=LLM_TIMEOUT_S
                    )
                plan = validate_plan(extract_json_object(raw), words, limits, duration_s)
                break
            except Exception as exc:  # noqa: BLE001 - any failure falls back
                log.warning("Edit plan LLM attempt %d/%d failed: %s", attempt, LLM_ATTEMPTS, exc)

    fallback = heuristic_plan(words, duration_s, limits, hook=hook, emphasis_times=emphasis_times)
    if plan is None:
        plan = fallback
    else:
        # The model's picks stay; gaps are filled from the heuristics.
        if not plan.hook_text:
            plan.hook_text = fallback.hook_text
        if not plan.emphasis:
            plan.emphasis = fallback.emphasis
        if not plan.zooms:
            plan.zooms = fallback.zooms
    plan.template = template_name
    return plan
