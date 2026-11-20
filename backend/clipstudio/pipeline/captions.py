"""Caption generation — word timings to burned-in ASS subtitles.

Five presets (bold yellow-highlight word, box, karaoke, one-word pop, clean), all
in bundled Montserrat ExtraBold (SIL OFL).

Layout is done here, not left to libass: each caption group is measured with the
real font (Pillow) and broken into lines that fit inside the side margins. If a
group still does not fit, the font shrinks. This is what prevents the classic bug
of long lines running off both edges of a 720 px frame.

Per-word animation is done by emitting one Dialogue event per word, each showing
the whole group with the active word overridden — libass has no per-glyph
timeline. Keyword emphasis (numbers, LLM-marked words) adds colour and size.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path

import pysubs2

from .placement import ZoneSegment, choose_bottom_y, zone_at
from .transcript import Word

log = logging.getLogger(__name__)

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
FONT_FAMILY = "Montserrat ExtraBold"
FONT_FILE = "Montserrat-ExtraBold.ttf"

DEFAULT_MAX_WORDS = 3
DEFAULT_MAX_GAP_S = 0.45

#: Styles are authored against this height and scaled to the real output.
REFERENCE_HEIGHT = 1920

#: Keyword words get this colour and size.
KEYWORD_COLOUR = "#3CFF8C"
KEYWORD_SCALE = 115

STYLE_NAME = "Cap"
SEAM_STYLE_NAME = "CapSeam"
HOOK_STYLE_NAME = "Hook"

#: YouTube Shorts / Reels draw their UI over the bottom of the frame; captions
#: stay above this fraction of the height (measured from the bottom edge).
SAFE_BOTTOM_RATIO = 0.20
#: Hook card: top edge of the text, as a fraction of the frame height (below the
#: platform's top bar, inside the top third).
HOOK_TOP_RATIO = 0.16
HOOK_DURATION_S = 4.5
HOOK_FADE_OUT_MS = 400
#: Clips shorter than this get a hook of clip length minus HOOK_SHORT_MARGIN_S.
HOOK_SHORT_CLIP_S = 10.0
HOOK_SHORT_MARGIN_S = 1.0
HOOK_MIN_DURATION_S = 2.0


@dataclass
class HookSpec:
    """Opening title card, shown for the first ~4.5 s then faded out."""

    text: str
    style: str = "plain"  # "plain" | "box"
    primary: str = "#FFFFFF"
    accent: str = "#FFD400"
    duration_s: float = HOOK_DURATION_S


def hook_duration(hook: HookSpec, clip_duration_s: float | None = None) -> float:
    """Visible time for the hook: short clips get clip length minus a second (min 2 s)."""
    dur = hook.duration_s
    if clip_duration_s and clip_duration_s < HOOK_SHORT_CLIP_S:
        dur = min(dur, max(HOOK_MIN_DURATION_S, clip_duration_s - HOOK_SHORT_MARGIN_S))
    return dur


def hex_to_ass(colour: str, alpha: int = 0) -> pysubs2.Color:
    r, g, b = _rgb(colour)
    return pysubs2.Color(r, g, b, alpha)


def _rgb(colour: str) -> tuple[int, int, int]:
    value = colour.lstrip("#")
    if len(value) != 6:
        raise ValueError(f"Expected #RRGGBB, got {colour!r}")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def ass_colour_override(colour: str) -> str:
    r"""Inline ``\c`` colour value, e.g. ``&H00E5FF&`` (BGR, no alpha)."""
    r, g, b = _rgb(colour)
    return f"&H{b:02X}{g:02X}{r:02X}&"


@dataclass
class CaptionStyle:
    key: str
    label: str
    description: str

    font: str = FONT_FAMILY
    font_file: str = FONT_FILE
    #: Font size as a fraction of frame height.
    size_ratio: float = 0.05
    primary: str = "#FFFFFF"
    #: Colour of the word being spoken. None disables per-word highlighting.
    accent: str | None = "#FFE500"
    outline: str = "#000000"
    outline_width: float = 7.0
    shadow: float = 0.0
    bold: bool = False  # the font file is already ExtraBold
    all_caps: bool = True
    #: Distance from the bottom, as a fraction of height (clear of Shorts UI).
    margin_v_ratio: float = 0.24
    #: Left/right margin as a fraction of frame width.
    side_margin_ratio: float = 0.08
    max_words: int = DEFAULT_MAX_WORDS
    max_lines: int = 2
    #: "none" | "scale" | "karaoke" | "pop"
    animation: str = "scale"
    scale_percent: int = 112
    boxed: bool = False
    box_colour: str = "#000000"
    box_alpha: int = 60
    #: "words" (few words per chunk, per-word animation) or "phrase" (whole phrases
    #: split on punctuation / pauses, no per-word effects: subtitle style).
    grouping: str = "words"
    #: Phrase grouping: soft limit per line (characters), pause that ends a phrase,
    #: and the minimum time a caption stays on screen.
    max_chars_per_line: int = 0
    phrase_gap_s: float = 0.35
    min_on_screen_s: float = 0.0
    #: Fade in/out in milliseconds (0 = none).
    fade_ms: int = 0
    #: Opacity of the drop shadow (ASS alpha, 0 = opaque .. 255 = invisible).
    shadow_alpha: int = 60

    @property
    def font_path(self) -> Path:
        return FONT_DIR / self.font_file


PRESETS: dict[str, CaptionStyle] = {
    "yellow_pop": CaptionStyle(
        key="yellow_pop",
        label="Bold Yellow",
        description="White caps with a heavy outline; the spoken word turns yellow and pops.",
        size_ratio=0.056,
        accent="#FFE500",
        outline_width=7.0,
        animation="scale",
        scale_percent=112,
    ),
    "box": CaptionStyle(
        key="box",
        label="Box",
        description="White text on a dark block. Readable on any footage.",
        size_ratio=0.048,
        accent="#FFD400",
        outline_width=14.0,
        outline="#000000",
        boxed=True,
        box_alpha=70,
        animation="scale",
        scale_percent=108,
        max_words=3,
    ),
    "karaoke": CaptionStyle(
        key="karaoke",
        label="Karaoke",
        description="Words fill with colour exactly as they are spoken.",
        size_ratio=0.052,
        accent="#31E981",
        outline_width=7.0,
        animation="karaoke",
        max_words=4,
    ),
    "one_word": CaptionStyle(
        key="one_word",
        label="One Word",
        description="One big word at a time with a quick pop-in.",
        size_ratio=0.095,
        accent=None,
        primary="#FFFFFF",
        outline_width=9.0,
        animation="pop",
        max_words=1,
        max_lines=1,
        margin_v_ratio=0.30,
    ),
    "clean": CaptionStyle(
        key="clean",
        label="Clean",
        description="Smaller, calm lower caption with a soft shadow. No animation.",
        size_ratio=0.038,
        accent=None,
        outline_width=3.0,
        shadow=2.0,
        all_caps=False,
        animation="none",
        margin_v_ratio=0.18,
        max_words=6,
    ),
    "film": CaptionStyle(
        key="film",
        label="Film",
        description="Cinematic subtitles: sentence case, white, soft outline and shadow, no box, phrase by phrase.",
        font="Inter",
        font_file="Inter-Variable.ttf",
        size_ratio=0.043,
        accent=None,
        outline="#0A0A0A",
        outline_width=2.4,
        shadow=3.0,
        shadow_alpha=90,
        bold=True,
        all_caps=False,
        animation="none",
        margin_v_ratio=0.24,
        side_margin_ratio=0.09,
        max_lines=2,
        grouping="phrase",
        max_chars_per_line=34,
        phrase_gap_s=0.35,
        min_on_screen_s=0.8,
        fade_ms=120,
    ),
}

DEFAULT_STYLE = "yellow_pop"

#: Keys used by earlier versions.
LEGACY_KEYS = {
    "bold_pop": "yellow_pop",
    "karaoke_fill": "karaoke",
    "clean_lower": "clean",
    "boxed": "box",
}


def get_style(key: str) -> CaptionStyle:
    key = LEGACY_KEYS.get(key, key)
    style = PRESETS.get(key)
    if style is None:
        raise ValueError(f"Unknown caption style {key!r}. Available: {', '.join(PRESETS)}")
    return style


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------


def upper_loc(text: str, lang: str = "") -> str:
    """``str.upper()`` that follows Azerbaijani/Turkish dotted-i rules."""
    if (lang or "").lower()[:2] in ("az", "tr"):
        text = text.replace("i", "İ").replace("ı", "I")
    return text.upper()


@lru_cache(maxsize=64)
def _font(path: str, size: int):
    from PIL import ImageFont

    return ImageFont.truetype(path, size)


def measure(text: str, style: CaptionStyle, font_px: int) -> float:
    """Rendered width of ``text`` in pixels at ``font_px``."""
    try:
        # libass sizes the font by line height (ascent+descent ~ 1.22 em), so the
        # visible em is ~0.82 of the ASS font size. Measure at that size, with a
        # little safety margin so rendered text never exceeds the budget.
        em = max(4, round(font_px * 0.86))
        return float(_font(str(style.font_path), em).getlength(text))
    except OSError:  # font missing: be conservative
        return len(text) * font_px * 0.75


def wrap_lines(
    texts: list[str], style: CaptionStyle, font_px: int, max_width: float
) -> list[list[int]]:
    """Greedy line breaking. Returns lists of indices into ``texts``."""
    space = measure(" ", style, font_px)
    lines: list[list[int]] = []
    current: list[int] = []
    width = 0.0
    for index, text in enumerate(texts):
        w = measure(text, style, font_px)
        extra = w if not current else space + w
        if current and width + extra > max_width:
            lines.append(current)
            current, width = [index], w
        else:
            current.append(index)
            width += extra
    if current:
        lines.append(current)
    return lines


def fit_group(
    texts: list[str], style: CaptionStyle, base_px: int, max_width: float
) -> tuple[list[list[int]], int]:
    """Find a font size (<= ``base_px``) at which the group fits the width.

    The budget leaves room for the active-word scale-up, so the highlighted
    state also stays inside the margins.
    """
    growth = max(style.scale_percent, KEYWORD_SCALE) / 100 if style.animation != "none" else 1.0
    budget = max_width / growth
    px = base_px
    floor = max(10, int(base_px * 0.45))
    while True:
        lines = wrap_lines(texts, style, px, budget)
        widest = max(
            (
                sum(measure(texts[i], style, px) for i in line)
                + measure(" ", style, px) * (len(line) - 1)
                for line in lines
            ),
            default=0.0,
        )
        if len(lines) <= style.max_lines and widest <= budget:
            return lines, px
        if px <= floor:
            # Line count can't be met any more; width still must be. A single
            # enormous word shrinks to exactly fit rather than overflow.
            if widest <= budget or px <= 10:
                return lines, px
            px = max(10, int(px * budget / widest) - 1)
        else:
            px = max(floor, int(px * 0.92))


# --------------------------------------------------------------------------
# Grouping
# --------------------------------------------------------------------------


@dataclass
class CaptionGroup:
    words: list[Word] = field(default_factory=list)
    #: Index of each word in the source list (for keyword lookup).
    indices: list[int] = field(default_factory=list)

    @property
    def start(self) -> float:
        return self.words[0].start if self.words else 0.0

    @property
    def end(self) -> float:
        return self.words[-1].end if self.words else 0.0

    @property
    def text(self) -> str:
        return " ".join(w.text.strip() for w in self.words)


def group_words(
    words: list[Word],
    *,
    max_words: int = DEFAULT_MAX_WORDS,
    max_gap_s: float = DEFAULT_MAX_GAP_S,
) -> list[CaptionGroup]:
    """Split words into caption groups: sentence end, long pause, word cap."""
    groups: list[CaptionGroup] = []
    current = CaptionGroup()
    for index, word in enumerate(words):
        if current.words and (
            word.start - current.words[-1].end > max_gap_s or len(current.words) >= max_words
        ):
            groups.append(current)
            current = CaptionGroup()
        current.words.append(word)
        current.indices.append(index)
        if word.ends_sentence and index != len(words) - 1:
            groups.append(current)
            current = CaptionGroup()
    if current.words:
        groups.append(current)
    return groups


def group_phrases(
    words: list[Word],
    *,
    max_chars_per_line: int = 34,
    max_lines: int = 2,
    max_gap_s: float = 0.35,
    min_clause_chars: int = 14,
) -> list[CaptionGroup]:
    """Phrase-based groups for subtitle-style captions.

    A phrase ends at sentence punctuation, at a comma/dash once it has some body,
    at a pause longer than ``max_gap_s``, or when it would exceed
    ``max_lines * max_chars_per_line`` characters.
    """
    budget = max(1, max_chars_per_line) * max(1, max_lines)
    groups: list[CaptionGroup] = []
    current = CaptionGroup()
    chars = 0
    for index, word in enumerate(words):
        text = word.text.strip()
        add = len(text) + (1 if current.words else 0)
        if current.words and (word.start - current.words[-1].end > max_gap_s or chars + add > budget):
            groups.append(current)
            current, chars = CaptionGroup(), 0
            add = len(text)
        current.words.append(word)
        current.indices.append(index)
        chars += add
        if word.ends_sentence or (word.ends_clause and chars >= min_clause_chars):
            groups.append(current)
            current, chars = CaptionGroup(), 0
    if current.words:
        groups.append(current)
    return groups


def balanced_lines(texts: list[str], max_chars: int, max_lines: int = 2) -> list[list[int]]:
    """Split words into at most ``max_lines`` lines of similar length (<= ``max_chars`` where possible)."""
    total = sum(len(t) for t in texts) + max(0, len(texts) - 1)
    if len(texts) <= 1 or total <= max_chars or max_lines < 2:
        return [list(range(len(texts)))]
    best, best_key = 1, None
    for cut in range(1, len(texts)):
        a = sum(len(t) for t in texts[:cut]) + cut - 1
        b = sum(len(t) for t in texts[cut:]) + len(texts) - cut - 1
        key = (max(a, b) > max_chars, max(a, b))
        if best_key is None or key < best_key:
            best, best_key = cut, key
    return [list(range(best)), list(range(best, len(texts)))]


# --------------------------------------------------------------------------
# ASS generation
# --------------------------------------------------------------------------


def build_ass(
    words: list[Word],
    style: CaptionStyle,
    *,
    width: int,
    height: int,
    time_offset_s: float = 0.0,
    keywords: set[int] | None = None,
    lang: str = "",
    seam_intervals: list[tuple[float, float]] | None = None,
    seam_y_ratio: float = 0.40,
    hook: HookSpec | None = None,
    clip_duration_s: float | None = None,
    zones: list[ZoneSegment] | None = None,
    face_margin: float = 0.0,
) -> pysubs2.SSAFile:
    """Build the ASS file. ``keywords`` holds indices into ``words``.

    ``seam_intervals`` are spans (output seconds) where the split layout is on
    screen; captions there sit on the seam between the two halves instead of at
    the bottom. ``hook`` adds the opening title card in the top third.
    """
    subs = pysubs2.SSAFile()
    subs.info["PlayResX"] = str(width)
    subs.info["PlayResY"] = str(height)
    subs.info["ScaledBorderAndShadow"] = "yes"
    subs.info["WrapStyle"] = "0"

    scale = height / REFERENCE_HEIGHT
    base_px = round(height * style.size_ratio)
    side = round(width * style.side_margin_ratio)
    # Captions never enter the bottom safe zone, whatever the preset says.
    style = replace(style, margin_v_ratio=max(style.margin_v_ratio, SAFE_BOTTOM_RATIO + 0.04))
    subs.styles[STYLE_NAME] = _build_ass_style(style, height=height, scale=scale, side=side)
    if seam_intervals:
        seam = _build_ass_style(style, height=height, scale=scale, side=side)
        seam.alignment = pysubs2.Alignment.MIDDLE_CENTER
        seam.marginv = 0
        subs.styles[SEAM_STYLE_NAME] = seam

    # Box style pads the text by the outline width on each side.
    pad = style.outline_width * scale * 2 if style.boxed else style.outline_width * scale
    max_width = width - 2 * side - pad
    keywords = keywords or set()

    phrase = style.grouping == "phrase"
    if phrase:
        groups = group_phrases(
            words,
            max_chars_per_line=style.max_chars_per_line or 34,
            max_lines=style.max_lines,
            max_gap_s=style.phrase_gap_s,
        )
    else:
        groups = group_words(words, max_words=style.max_words)
    default_bottom = height * (1 - style.margin_v_ratio)

    for gi, group in enumerate(groups):
        texts = [_text_of(w, style, lang) for w in group.words]
        if phrase:
            lines = balanced_lines(texts, style.max_chars_per_line or 34, style.max_lines)
            px = base_px
            if _widest(texts, lines, style, px) > max_width:
                lines, px = fit_group(texts, style, base_px, max_width)
        else:
            lines, px = fit_group(texts, style, base_px, max_width)
        size_tag = f"\\fs{px}" if px != base_px else ""
        flags = [i in keywords for i in group.indices]
        first_event = len(subs.events)
        if style.animation == "karaoke":
            subs.events.append(_karaoke_event(group, texts, lines, flags, size_tag, time_offset_s))
        elif style.animation == "pop":
            subs.events.extend(_pop_events(group, texts, size_tag, flags, time_offset_s))
        elif style.animation == "scale" and style.accent:
            subs.events.extend(
                _per_word_events(group, style, texts, lines, flags, size_tag, time_offset_s)
            )
        else:
            end = group.end
            if style.min_on_screen_s and end - group.start < style.min_on_screen_s:
                limit = groups[gi + 1].start if gi + 1 < len(groups) else group.start + style.min_on_screen_s
                end = max(end, min(group.start + style.min_on_screen_s, limit))
            subs.events.append(
                _event(
                    group.start,
                    end,
                    _compose(texts, lines, flags, None, style, size_tag),
                    time_offset_s,
                )
            )
        prefix = f"\\fad({style.fade_ms},{style.fade_ms})" if style.fade_ms else ""
        if zones:
            t0, t1 = group.start - time_offset_s, group.end - time_offset_s
            box_h = len(lines) * px * 1.22 + style.outline_width * scale * 2
            box_w = _widest(texts, lines, style, px) + pad
            y = choose_bottom_y(
                zone_at(zones, (t0 + t1) / 2),
                t0=t0, t1=t1, out_w=width, out_h=height,
                default_bottom=default_bottom, box_w=box_w, box_h=box_h,
                face_margin=face_margin,
            )  # fmt: skip
            if y is not None:
                prefix += f"\\an2\\pos({width // 2},{round(y)})"
        if prefix:
            for event in subs.events[first_event:]:
                event.text = "{" + prefix + "}" + event.text
    _remove_overlaps(subs)
    if seam_intervals:
        _move_to_seam(subs, seam_intervals, x=width // 2, y=round(height * seam_y_ratio))
    if hook is not None and hook.text.strip():
        _add_hook(
            subs, hook, style, width=width, height=height, scale=scale, lang=lang,
            clip_duration_s=clip_duration_s,
        )
    return subs


def _widest(texts: list[str], lines: list[list[int]], style: CaptionStyle, px: int) -> float:
    space = measure(" ", style, px)
    return max(
        (sum(measure(texts[i], style, px) for i in line) + space * (len(line) - 1) for line in lines),
        default=0.0,
    )


def _move_to_seam(
    subs: pysubs2.SSAFile, intervals: list[tuple[float, float]], *, x: int, y: int
) -> None:
    for event in subs.events:
        mid = (event.start + event.end) / 2000.0
        if any(a <= mid < b for a, b in intervals):
            event.style = SEAM_STYLE_NAME
            event.text = f"{{\\pos({x},{y})}}" + event.text


def _add_hook(
    subs: pysubs2.SSAFile,
    hook: HookSpec,
    style: CaptionStyle,
    *,
    width: int,
    height: int,
    scale: float,
    lang: str,
    clip_duration_s: float | None = None,
) -> None:
    boxed = hook.style == "box"
    caps = style.grouping != "phrase"  # film-style captions keep sentence case
    hook_style = replace(style, max_lines=2 if not boxed else 3, animation="none", boxed=boxed, all_caps=caps)
    raw_words = hook.text.replace("{", "").replace("}", "").split()
    words = [upper_loc(w, lang) if caps else w for w in raw_words]
    if not words:
        return
    base_px = round(height * (0.062 if style.grouping == "phrase" else 0.052))
    pad = (22 if boxed else 10) * scale * 2
    lines, px = fit_group(words, hook_style, base_px, width * 0.84 - pad)

    ass = pysubs2.SSAStyle()
    ass.fontname = style.font
    ass.fontsize = px
    ass.bold = True
    ass.alignment = pysubs2.Alignment.TOP_CENTER
    ass.marginv = round(height * HOOK_TOP_RATIO)
    ass.marginl = ass.marginr = round(width * 0.08)
    if boxed:
        # Opaque box in the accent colour with dark text.
        ass.borderstyle = 3
        ass.primarycolor = hex_to_ass("#111111")
        ass.outlinecolor = hex_to_ass(hook.accent)
        ass.backcolor = hex_to_ass(hook.accent)
        ass.outline = 22 * scale
        ass.shadow = 0
    else:
        ass.borderstyle = 1
        ass.primarycolor = hex_to_ass(hook.primary)
        ass.outlinecolor = hex_to_ass("#000000")
        ass.backcolor = hex_to_ass("#000000", alpha=150)
        ass.outline = 10 * scale
        ass.shadow = 4 * scale
    subs.styles[HOOK_STYLE_NAME] = ass

    body = "\\N".join(" ".join(words[i] for i in line) for line in lines)
    if not boxed and len(words) > 1:
        # Accent word: the last one, coloured via an inline override.
        body = _accent_last(body, words[-1], hook.accent)
    anim = f"{{\\fad(160,{HOOK_FADE_OUT_MS})\\fscx88\\fscy88\\t(0,240,\\fscx100\\fscy100)}}"
    subs.events.append(
        pysubs2.SSAEvent(
            start=0,
            end=pysubs2.make_time(s=hook_duration(hook, clip_duration_s)),
            text=anim + body,
            style=HOOK_STYLE_NAME,
            layer=2,
        )
    )


def _accent_last(body: str, last: str, accent: str) -> str:
    head, sep, tail = body.rpartition(last)
    if not sep:
        return body
    r, g, b = _rgb(accent)
    return f"{head}{{\\c&H{b:02X}{g:02X}{r:02X}&}}{last}{{\\c&HFFFFFF&}}{tail}"


def _remove_overlaps(subs: pysubs2.SSAFile) -> None:
    """Make events strictly sequential.

    Whisper word times can overlap (a word ending after the next begins), and
    cuts remap times further. Overlapping events render both groups at once,
    which shows up as doubled caption lines on screen.
    """
    events = sorted(subs.events, key=lambda e: e.start)
    kept: list[pysubs2.SSAEvent] = []
    for event in events:
        if kept:
            prev = kept[-1]
            if event.start <= prev.start:
                kept.pop()  # same start: the later event wins
            elif prev.end > event.start:
                prev.end = event.start
        kept.append(event)
    subs.events = kept


def _build_ass_style(
    style: CaptionStyle, *, height: int, scale: float, side: int
) -> pysubs2.SSAStyle:
    ass_style = pysubs2.SSAStyle()
    ass_style.fontname = style.font
    ass_style.fontsize = round(height * style.size_ratio)
    ass_style.primarycolor = hex_to_ass(style.primary)
    ass_style.secondarycolor = hex_to_ass(style.accent or style.primary)
    ass_style.outlinecolor = hex_to_ass(style.outline)
    ass_style.backcolor = hex_to_ass(
        style.box_colour, alpha=style.box_alpha if style.boxed else style.shadow_alpha
    )
    ass_style.bold = style.bold
    ass_style.outline = style.outline_width * scale
    ass_style.shadow = style.shadow * scale
    ass_style.borderstyle = 3 if style.boxed else 1
    ass_style.alignment = pysubs2.Alignment.BOTTOM_CENTER
    ass_style.marginv = round(height * style.margin_v_ratio)
    ass_style.marginl = ass_style.marginr = side
    return ass_style


def _text_of(word: Word, style: CaptionStyle, lang: str) -> str:
    text = word.text.strip().replace("{", "(").replace("}", ")").replace("\\", "")
    return upper_loc(text, lang) if style.all_caps else text


def _event(start_s: float, end_s: float, text: str, offset: float) -> pysubs2.SSAEvent:
    return pysubs2.SSAEvent(
        start=pysubs2.make_time(s=max(0.0, start_s - offset)),
        end=pysubs2.make_time(s=max(0.0, end_s - offset)),
        text=text,
        style=STYLE_NAME,
    )


def _render_word(
    text: str, *, active: bool, keyword: bool, style: CaptionStyle, size_tag: str
) -> str:
    tags = ""
    if active and style.accent:
        tags += f"\\c{ass_colour_override(style.accent)}\\fscx{style.scale_percent}"
        tags += f"\\fscy{style.scale_percent}"
    elif keyword and style.animation != "none":
        tags += f"\\c{ass_colour_override(KEYWORD_COLOUR)}\\fscx{KEYWORD_SCALE}\\fscy{KEYWORD_SCALE}"
    if not tags:
        return text
    return f"{{{tags}}}{text}{{\\r{size_tag}}}"


def _compose(
    texts: list[str],
    lines: list[list[int]],
    flags: list[bool],
    active: int | None,
    style: CaptionStyle,
    size_tag: str,
) -> str:
    rendered = [
        _render_word(t, active=(i == active), keyword=flags[i], style=style, size_tag=size_tag)
        for i, t in enumerate(texts)
    ]
    body = "\\N".join(" ".join(rendered[i] for i in line) for line in lines)
    return f"{{{size_tag}}}{body}" if size_tag else body


def _per_word_events(
    group: CaptionGroup,
    style: CaptionStyle,
    texts: list[str],
    lines: list[list[int]],
    flags: list[bool],
    size_tag: str,
    offset: float,
) -> list[pysubs2.SSAEvent]:
    events: list[pysubs2.SSAEvent] = []
    last = len(group.words) - 1
    for active, word in enumerate(group.words):
        end = group.end if active == last else group.words[active + 1].start
        events.append(
            _event(
                word.start,
                max(end, word.end),
                _compose(texts, lines, flags, active, style, size_tag),
                offset,
            )
        )
    return events


def _karaoke_event(
    group: CaptionGroup,
    texts: list[str],
    lines: list[list[int]],
    flags: list[bool],
    size_tag: str,
    offset: float,
) -> pysubs2.SSAEvent:
    r"""``\kf`` sweep per word; keyword words get a colour/size override."""
    parts: list[str] = []
    for index, word in enumerate(group.words):
        cs = max(1, round(word.duration * 100))
        if index + 1 < len(group.words):
            gap = group.words[index + 1].start - word.end
            if 0 < gap < 0.5:
                cs += round(gap * 100)
        extra = ""
        if flags[index]:
            extra = (
                f"\\1c{ass_colour_override(KEYWORD_COLOUR)}"
                f"\\fscx{KEYWORD_SCALE}\\fscy{KEYWORD_SCALE}"
            )
        tail = "{\\fscx100\\fscy100}" if flags[index] else ""
        parts.append(f"{{\\kf{cs}{extra}}}{texts[index]}{tail}")
    body = "\\N".join(" ".join(parts[i] for i in line) for line in lines)
    return _event(group.start, group.end, f"{{{size_tag}}}{body}" if size_tag else body, offset)


def _pop_events(
    group: CaptionGroup,
    texts: list[str],
    size_tag: str,
    flags: list[bool],
    offset: float,
) -> list[pysubs2.SSAEvent]:
    events: list[pysubs2.SSAEvent] = []
    for index, word in enumerate(group.words):
        colour = f"\\c{ass_colour_override(KEYWORD_COLOUR)}" if flags[index] else ""
        text = (
            f"{{\\fscx70\\fscy70{size_tag}{colour}"
            f"\\t(0,110,\\fscx100\\fscy100)}}{texts[index]}"
        )
        events.append(_event(word.start, max(word.end, word.start + 0.12), text, offset))
    return events


def write_ass(
    path: Path,
    words: list[Word],
    style: CaptionStyle,
    *,
    width: int,
    height: int,
    time_offset_s: float = 0.0,
    keywords: set[int] | None = None,
    lang: str = "",
    seam_intervals: list[tuple[float, float]] | None = None,
    hook: HookSpec | None = None,
    clip_duration_s: float | None = None,
    zones: list[ZoneSegment] | None = None,
    face_margin: float = 0.0,
) -> Path:
    subs = build_ass(
        words,
        style,
        width=width,
        height=height,
        time_offset_s=time_offset_s,
        keywords=keywords,
        lang=lang,
        seam_intervals=seam_intervals,
        hook=hook,
        clip_duration_s=clip_duration_s,
        zones=zones,
        face_margin=face_margin,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    subs.save(str(path), encoding="utf-8")
    return path


def write_srt(path: Path, words: list[Word], *, time_offset_s: float = 0.0) -> Path:
    """Plain .srt sidecar."""
    subs = pysubs2.SSAFile()
    for group in group_words(words, max_words=8):
        subs.events.append(
            pysubs2.SSAEvent(
                start=pysubs2.make_time(s=max(0.0, group.start - time_offset_s)),
                end=pysubs2.make_time(s=max(0.0, group.end - time_offset_s)),
                text=group.text,
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    subs.save(str(path), encoding="utf-8", format_="srt")
    return path
