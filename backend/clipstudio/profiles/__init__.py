"""Content profiles: data-driven editing defaults per kind of footage.

A profile (``profiles/*.json``) sets the edit switches, caption preset, effects,
hook and layout preference that suit one kind of footage (film, talking head,
live event, gaming). Nothing here knows about any particular campaign.

Selection: when the job has a campaign brief, the brief's ``footage_type`` picks
the profile; otherwise (or when the brief doesn't say) ``detect_profile`` decides
cheaply from the source (speech density, letterbox bars, shot cut rate). The
choice is stored in ``job.settings["profile"]``; explicit job / template
settings still override the profile.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PROFILE_DIR = Path(__file__).parent
DEFAULT_PROFILE = "talking"

#: Campaign ``footage_type`` -> profile name. "other" means "decide from the source".
FOOTAGE_TO_PROFILE = {"film": "film", "talking": "talking", "event": "event", "gaming": "gaming"}

#: Auto-detect thresholds.
FILM_MIN_CUTS_PER_MIN = 8.0
FILM_WPM_RANGE = (30.0, 130.0)
EVENT_MAX_WPM = 35.0
EVENT_MIN_CUTS_PER_MIN = 4.0


@dataclass(frozen=True)
class Profile:
    name: str = DEFAULT_PROFILE
    label: str = "Talking"
    description: str = ""
    # edit switches
    trim_silence: bool = True
    remove_fillers: bool = True
    punch_zoom: bool = True
    keyword_emphasis: bool = True
    # look
    caption_preset: str = "yellow_pop"
    max_emoji: int = 2
    sfx: bool = True
    progress_bar: bool = True
    hook_card: bool = True
    hook_style: str = "plain"
    hook_duration_s: float = 2.8
    layout_mode: str = "auto"
    hashtag_count: int = 4

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def _read(path: Path) -> Profile | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {k: v for k, v in data.items() if k in Profile.__dataclass_fields__}
        known["name"] = str(known.get("name") or path.stem)
        return Profile(**known)
    except Exception as exc:  # noqa: BLE001 - a bad file must not break loading
        log.warning("Ignoring invalid profile %s: %s", path, exc)
        return None


def list_profiles() -> list[Profile]:
    return [p for f in sorted(PROFILE_DIR.glob("*.json")) if (p := _read(f))]


def load_profile(name: str | None) -> Profile:
    """Profile by name; unknown names fall back to the default."""
    key = (name or "").strip().lower()
    profiles = {p.name: p for p in list_profiles()}
    return profiles.get(key) or profiles.get(DEFAULT_PROFILE) or Profile()


def is_known(name: str | None) -> bool:
    return (name or "").strip().lower() in {p.name for p in list_profiles()}


def detect_profile(
    *,
    words_per_min: float | None = None,
    letterbox: bool = False,
    cuts_per_min: float | None = None,
) -> str:
    """Cheap content-type guess from source signals (no brief available).

    Film-like = baked-in letterbox bars, or a high shot-cut rate with moderate
    speech. Very little speech with some cuts = event. Otherwise talking.
    """
    if letterbox:
        return "film"
    wpm = words_per_min if words_per_min is not None else 120.0
    cuts = cuts_per_min if cuts_per_min is not None else 0.0
    if cuts >= FILM_MIN_CUTS_PER_MIN and FILM_WPM_RANGE[0] <= wpm <= FILM_WPM_RANGE[1]:
        return "film"
    if wpm < EVENT_MAX_WPM and cuts >= EVENT_MIN_CUTS_PER_MIN:
        return "event"
    return DEFAULT_PROFILE


def profile_from_footage(footage_type: str | None) -> str | None:
    return FOOTAGE_TO_PROFILE.get((footage_type or "").strip().lower())


def choose_profile(
    *,
    footage_type: str | None = None,
    has_brief: bool = False,
    words_per_min: float | None = None,
    letterbox: bool = False,
    cuts_per_min: float | None = None,
) -> str:
    """Brief's footage type wins; otherwise detect from the source."""
    if has_brief:
        named = profile_from_footage(footage_type)
        if named:
            return named
    return detect_profile(
        words_per_min=words_per_min, letterbox=letterbox, cuts_per_min=cuts_per_min
    )


def apply_to_edit(
    profile: Profile, edit: Any, explicit: set[str] | frozenset[str] = frozenset()
) -> None:
    """Set the profile's edit switches on ``settings.edit`` (skipping explicit ones)."""
    for name in (
        "trim_silence",
        "remove_fillers",
        "punch_zoom",
        "keyword_emphasis",
        "progress_bar",
    ):
        if name not in explicit:
            setattr(edit, name, getattr(profile, name))


def apply_to_template(profile: Profile, template: Any) -> Any:
    """Return ``template`` re-skinned with the profile's look (caller decides when)."""
    return replace(
        template,
        caption_preset=profile.caption_preset,
        max_emoji=profile.max_emoji,
        sfx_enabled=profile.sfx,
        progress_bar=profile.progress_bar,
        hook_card=profile.hook_card,
        hook_style=profile.hook_style,
        hook_duration_s=profile.hook_duration_s,
        layout_mode=profile.layout_mode,
    )
