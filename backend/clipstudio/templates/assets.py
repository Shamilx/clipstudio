"""Bundled sound effects and emoji images for clip templates."""

from __future__ import annotations

from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets"

SFX_KINDS = ("whoosh", "pop", "ding")


def sfx_path(kind: str) -> Path | None:
    """Path to a bundled sound effect ("whoosh", "pop", "ding"), or None."""
    if kind not in SFX_KINDS:
        return None
    p = ASSETS / "sfx" / f"{kind}.wav"
    return p if p.is_file() else None


def emoji_png(char: str) -> Path | None:
    """Path to the 128px Noto Emoji PNG for ``char``, or None if not bundled."""
    if not char:
        return None
    # Drop variation selectors / ZWJ so "❤️" matches emoji_u2764.png.
    cps = [c for c in char if ord(c) not in (0xFE0F, 0x200D)]
    if not cps:
        return None
    name = "_".join(f"{ord(c):x}" for c in cps)
    p = ASSETS / "emoji" / f"emoji_u{name}.png"
    return p if p.is_file() else None
