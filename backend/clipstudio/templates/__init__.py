"""Clip style templates: built-in JSON presets plus user overrides.

User templates live in ``<clipstudio root>/templates/*.json`` and override
built-ins of the same name. Loading never raises: invalid values are clamped
or replaced by defaults, and an unknown name falls back to ``talk``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .. import paths

log = logging.getLogger(__name__)

__all__ = [
    "Template", "load_template", "list_templates", "save_template",
    "delete_template", "is_builtin", "is_user", "auto_pick", "validate",
]  # fmt: skip

BUILTIN_DIR = Path(__file__).parent / "builtin"
DEFAULT_NAME = "talk"
CAPTION_PRESETS = ("yellow_pop", "box", "karaoke", "one_word", "clean", "film")
_NAME_RE = re.compile(r"[^a-z0-9_-]+")
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


@dataclass
class Template:
    name: str = DEFAULT_NAME
    label: str = "Talk"
    description: str = ""
    caption_preset: str = "yellow_pop"
    primary_color: str = "#FFFFFF"
    accent_color: str = "#FFD400"
    font: str = "Montserrat ExtraBold"
    hook_card: bool = True
    hook_style: str = "plain"
    hook_duration_s: float = 4.5
    zoom_strength: float = 1.1
    zoom_min_gap_s: float = 4.0
    max_emoji: int = 2
    sfx_enabled: bool = True
    sfx_volume: float = 0.3
    progress_bar: bool = True
    progress_color: str = "#FFD400"
    layout_mode: str = "auto"
    # Campaign rules (see templates/rules.py). Video-side enforcement of
    # max_duration_s / watermark_text is wired into the pipeline separately.
    max_duration_s: float | None = None
    required_hashtags: list[str] = field(default_factory=list)
    credit_text: str = ""
    watermark_text: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def safe_name(name: str) -> str:
    return _NAME_RE.sub("-", str(name).strip().lower()).strip("-")[:40]


def _num(v: Any, default: float, lo: float, hi: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN
        return default
    return max(lo, min(hi, f))


def _bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(v, (int, float)):
        return bool(v)
    return default


def _color(v: Any, default: str) -> str:
    return v.upper() if isinstance(v, str) and _HEX_RE.match(v) else default


def validate(data: dict[str, Any], fallback_name: str = DEFAULT_NAME) -> Template:
    """Build a Template from arbitrary data, clamping bad values."""
    d = Template()
    if not isinstance(data, dict):
        data = {}
    name = safe_name(data.get("name") or fallback_name) or fallback_name
    label = data.get("label")
    font = data.get("font")
    preset = data.get("caption_preset")
    return Template(
        name=name,
        label=str(label).strip()[:40] if label else name.title(),
        description=str(data.get("description") or "")[:200],
        caption_preset=preset if preset in CAPTION_PRESETS else d.caption_preset,
        primary_color=_color(data.get("primary_color"), d.primary_color),
        accent_color=_color(data.get("accent_color"), d.accent_color),
        font=str(font).strip()[:60] if font and str(font).strip() else d.font,
        hook_card=_bool(data.get("hook_card"), d.hook_card),
        hook_style=data.get("hook_style") if data.get("hook_style") in ("box", "plain") else d.hook_style,
        hook_duration_s=_num(data.get("hook_duration_s"), d.hook_duration_s, 1.5, 8.0),
        zoom_strength=_num(data.get("zoom_strength"), d.zoom_strength, 1.0, 1.3),
        zoom_min_gap_s=_num(data.get("zoom_min_gap_s"), d.zoom_min_gap_s, 0.0, 60.0),
        max_emoji=int(_num(data.get("max_emoji"), d.max_emoji, 0, 10)),
        sfx_enabled=_bool(data.get("sfx_enabled"), d.sfx_enabled),
        sfx_volume=_num(data.get("sfx_volume"), d.sfx_volume, 0.0, 1.0),
        progress_bar=_bool(data.get("progress_bar"), d.progress_bar),
        progress_color=_color(data.get("progress_color"), d.progress_color),
        layout_mode=data.get("layout_mode") if data.get("layout_mode") in ("auto", "speaker", "split") else d.layout_mode,
        max_duration_s=(
            None if data.get("max_duration_s") in (None, "", 0)
            else _num(data.get("max_duration_s"), 60.0, 5.0, 600.0)
        ),
        required_hashtags=_hashtags(data.get("required_hashtags")),
        credit_text=_opt_text(data.get("credit_text"), 120) or "",
        watermark_text=_opt_text(data.get("watermark_text"), 60),
    )  # fmt: skip


def _hashtags(v: Any) -> list[str]:
    """Normalise to unique '#tag' strings (max 15, each <= 40 chars)."""
    if isinstance(v, str):
        v = re.split(r"[\s,]+", v)
    if not isinstance(v, (list, tuple)):
        return []
    out: list[str] = []
    for item in v:
        tag = re.sub(r"[^\w]", "", str(item), flags=re.UNICODE)[:40]
        if tag and f"#{tag}".lower() not in [t.lower() for t in out]:
            out.append(f"#{tag}")
    return out[:15]


def _opt_text(v: Any, limit: int) -> str | None:
    s = str(v).strip()[:limit] if v is not None else ""
    return s or None


def user_dir() -> Path:
    return paths.root() / "templates"


def _read(path: Path) -> Template | None:
    try:
        return validate(json.loads(path.read_text(encoding="utf-8")), path.stem)
    except Exception as exc:  # noqa: BLE001 - a bad file must not break loading
        log.warning("Ignoring invalid template %s: %s", path, exc)
        return None


def _builtins() -> dict[str, Template]:
    out: dict[str, Template] = {}
    for p in sorted(BUILTIN_DIR.glob("*.json")):
        t = _read(p)
        if t:
            out[t.name] = t
    return out


def _users() -> dict[str, Template]:
    out: dict[str, Template] = {}
    d = user_dir()
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            t = _read(p)
            if t:
                out[t.name] = t
    return out


def is_builtin(name: str) -> bool:
    return safe_name(name) in _builtins()


def is_user(name: str) -> bool:
    return safe_name(name) in _users()


def list_templates() -> list[Template]:
    merged = _builtins()
    merged.update(_users())
    return list(merged.values())


def load_template(name: str | None) -> Template:
    key = safe_name(name or "")
    merged = _builtins()
    merged.update(_users())
    if key in merged:
        return merged[key]
    if DEFAULT_NAME in merged:
        return merged[DEFAULT_NAME]
    return Template()


def save_template(t: Template) -> Template:
    t = validate(asdict(t), t.name)
    d = user_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{t.name}.json").write_text(json.dumps(t.to_dict(), indent=2), encoding="utf-8")
    return t


def delete_template(name: str) -> bool:
    p = user_dir() / f"{safe_name(name)}.json"
    if p.is_file():
        p.unlink()
        return True
    return False


from .classify import auto_pick  # noqa: E402
