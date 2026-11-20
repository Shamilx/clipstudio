"""Export stage — crop path, auto-edit, captions and audio into one MP4.

The whole render is a single ffmpeg pass. Per-shot crop segments are trimmed out
of the source, cropped independently, scaled to a common output size, and
concatenated in the filtergraph. Auto-edit cuts (silence, fillers) are expressed
as extra trims of those same segments, so jump cuts cost nothing extra.

After the concat: optional zoom (punch-ins, jump-cut nudge) -> burned captions ->
thin progress bar. Audio: the same cuts, optional music with sidechain ducking,
then loudness normalisation.
"""

from __future__ import annotations

import logging
import re
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from .. import resources
from ..config import EditSettings, ExportSettings
from ..system import report
from . import autoedit, ffmpeg, layout
from . import captions as captions_module
from .captions import CaptionStyle, HookSpec
from .editplan import EditPlanData
from .placement import ZoneSegment, build_zones
from .reframe.croppath import CropPath, CropSegment, segment_crop_filter
from .scoring import _NUMBER
from .transcript import Word

log = logging.getLogger(__name__)

#: Output dimensions per aspect ratio. 720 wide is light mode: sharp enough for
#: a phone, and about 2.2x cheaper to encode than 1080.
RATIOS: dict[str, tuple[int, int]] = {
    "9:16": (720, 1280),
    "1:1": (720, 720),
    "16:9": (1280, 720),
}

LOUDNESS_TRUE_PEAK = -1.5
LOUDNESS_RANGE = 11.0

AUDIO_BITRATE = "128k"
AUDIO_SAMPLE_RATE = 44_100

PROGRESS_BAR_COLOUR = "0xFFE500"
PROGRESS_BAR_HEIGHT = 6
MUSIC_EXTENSIONS = (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac")


class ExportError(RuntimeError):
    """Rendering a clip failed."""


@dataclass
class ExportRequest:
    source: Path
    destination: Path
    start_s: float
    end_s: float
    crop_path: CropPath
    words: list[Word]
    style: CaptionStyle
    ratio: str = "9:16"
    burn_captions: bool = True
    has_audio: bool = True
    language: str = ""
    #: Absolute (transcript) times of LLM-marked emphasis words.
    emphasis_times: list[float] = field(default_factory=list)
    music_file: Path | None = None
    #: Template (clipstudio.templates.Template) — colours, layout mode, zoom/sfx limits.
    template: object | None = None
    #: Zooms / emoji / sfx / hook for this clip (clip-relative source seconds).
    edit_plan: EditPlanData | None = None
    #: Extra padding around face boxes when keeping captions off faces (QA retry).
    caption_face_margin: float = 0.0

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


@dataclass
class RenderPlan:
    """Everything derived from the request + edit settings."""

    plan: autoedit.EditPlan
    crop_path: CropPath
    words: list[Word]
    keywords: set[int]
    zoom_expr: str | None
    no_zoom: list[tuple[float, float]]
    style: CaptionStyle | None = None
    seam_intervals: list[tuple[float, float]] = field(default_factory=list)
    hook: HookSpec | None = None
    #: (edited-timeline seconds, PNG path)
    emoji: list[tuple[float, Path]] = field(default_factory=list)
    #: (edited-timeline seconds, wav path)
    sfx: list[tuple[float, Path]] = field(default_factory=list)
    progress_colour: str = PROGRESS_BAR_COLOUR
    progress_bar: bool = True
    accent: str = "#FFD400"
    #: Where the picture / faces sit per stretch of the edited timeline.
    zones: list[ZoneSegment] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return self.plan.edited_duration_s


def ratio_dimensions(ratio: str) -> tuple[int, int]:
    if ratio not in RATIOS:
        raise ExportError(f"Unknown ratio {ratio!r}. Available: {', '.join(RATIOS)}")
    return RATIOS[ratio]


def slugify_title(title: str, *, max_length: int = 50) -> str:
    """Filename-safe slug for a clip title."""
    slug = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE).strip().lower()
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return slug[:max_length] or "clip"


def output_filename(title: str, ratio: str) -> str:
    return f"{slugify_title(title)}_{ratio.replace(':', 'x')}.mp4"


def find_music(music_dir: Path, seed: str) -> Path | None:
    """Pick a track from ``music_dir`` deterministically per clip."""
    if not music_dir.is_dir():
        return None
    tracks = sorted(p for p in music_dir.iterdir() if p.suffix.lower() in MUSIC_EXTENSIONS)
    if not tracks:
        return None
    return tracks[zlib.crc32(seed.encode()) % len(tracks)]


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


def make_plan(request: ExportRequest, edit: EditSettings) -> RenderPlan:
    """Compute cuts, remapped caption words, keyword flags and the zoom schedule."""
    duration = request.duration_s
    rel = [
        Word(w.text, max(0.0, w.start - request.start_s), max(0.0, w.end - request.start_s))
        for w in request.words
        if w.end > request.start_s and w.start < request.end_s
    ]
    plan, kept = autoedit.plan_cuts(rel, duration, edit)
    out_w, out_h = ratio_dimensions(request.ratio)
    template = request.template
    edit_plan = request.edit_plan
    resolved = layout.resolve_layout(
        request.crop_path,
        getattr(template, "layout_mode", "auto"),
        out_w,
        out_h,
        request.ratio,
    )
    crop = resolved if plan.is_noop else autoedit.apply_cuts(resolved, plan.keep)

    emph = {round(t - request.start_s, 3) for t in request.emphasis_times}
    if edit_plan:
        emph |= {round(t, 3) for t in edit_plan.emphasis}
    mapped: list[Word] = []
    keywords: set[int] = set()
    for w in kept:
        mapped.append(Word(w.text, plan.map_time(w.start), plan.map_time(w.end), w.speaker))
        if edit.keyword_emphasis and (
            any(abs(w.start - t) < 0.02 for t in emph) or _NUMBER.search(w.text)
        ):
            keywords.add(len(mapped) - 1)

    emph_rel = sorted(emph)
    bumps, steps = autoedit.zoom_intervals(
        plan,
        [] if edit_plan else emph_rel,
        punch=edit.punch_zoom and not edit_plan,
        jump=edit.punch_zoom and not plan.is_noop,
    )
    total = plan.edited_duration_s
    if edit_plan and edit.punch_zoom:
        for z in edit_plan.zooms:
            t0 = plan.map_time(max(0.0, z["t"] - 0.1))
            t1 = min(total, t0 + z["dur"])
            if t1 - t0 > 0.4 and z["scale"] > 1.0:
                bumps.append((round(t0, 3), round(t1, 3), round(z["scale"] - 1.0, 4)))
    no_zoom: list[tuple[float, float]] = []
    seams: list[tuple[float, float]] = []
    cursor = 0.0
    for seg in crop.segments:
        if seg.fit or seg.layout == "split":
            no_zoom.append((cursor, cursor + seg.duration_s))
        if seg.layout == "split":
            seams.append((round(cursor, 3), round(cursor + seg.duration_s, 3)))
        cursor += seg.duration_s

    style = request.style
    accent = "#FFD400"
    progress_colour, progress_bar = PROGRESS_BAR_COLOUR, edit.progress_bar
    hook = None
    emoji: list[tuple[float, Path]] = []
    sfx: list[tuple[float, Path]] = []
    if template is not None:
        accent = getattr(template, "accent_color", accent)
        style = replace(
            style,
            primary=getattr(template, "primary_color", style.primary),
            accent=accent if style.accent else None,
        )
        progress_colour = "0x" + getattr(template, "progress_color", "#FFD400").lstrip("#")
        progress_bar = bool(getattr(template, "progress_bar", progress_bar))
        if getattr(template, "hook_card", False) and edit_plan and edit_plan.hook_text:
            hook = HookSpec(
                text=edit_plan.hook_text,
                style=getattr(template, "hook_style", "plain"),
                primary=getattr(template, "primary_color", "#FFFFFF"),
                accent=accent,
                duration_s=float(getattr(template, "hook_duration_s", captions_module.HOOK_DURATION_S)),
            )
    if edit_plan:
        emoji, sfx = _resolve_assets(edit_plan, plan, template, total)
    return RenderPlan(
        plan=plan,
        crop_path=crop,
        words=mapped,
        keywords=keywords,
        zoom_expr=autoedit.zoom_expression(bumps, steps, no_zoom),
        no_zoom=no_zoom,
        style=style,
        seam_intervals=seams,
        hook=hook,
        emoji=emoji,
        sfx=sfx,
        progress_colour=progress_colour,
        progress_bar=progress_bar,
        accent=accent,
        zones=build_zones(crop, out_w, out_h),
    )


def _resolve_assets(
    edit_plan: EditPlanData, plan: autoedit.EditPlan, template: object | None, total: float
) -> tuple[list[tuple[float, Path]], list[tuple[float, Path]]]:
    """Map plan emoji/sfx onto the edited timeline and onto real asset files."""
    try:
        from ..templates.assets import emoji_png, sfx_path  # type: ignore[import-not-found]
    except ImportError:  # assets package not available: render without them
        return [], []
    emoji: list[tuple[float, Path]] = []
    for item in edit_plan.emoji:
        png = emoji_png(item["char"])
        t = plan.map_time(item["t"])
        if png is not None and t < total - 0.6:
            emoji.append((round(t, 3), Path(png)))
    sfx: list[tuple[float, Path]] = []
    if getattr(template, "sfx_enabled", True):
        for item in edit_plan.sfx:
            wav = sfx_path(item["kind"])
            t = plan.map_time(item["t"])
            if wav is not None and t < total - 0.2:
                sfx.append((round(t, 3), Path(wav)))
    return emoji, sfx


# --------------------------------------------------------------------------
# Filtergraph construction
# --------------------------------------------------------------------------


def build_video_filtergraph(
    request: ExportRequest,
    *,
    subtitle_name: str | None,
    fonts_name: str = "fonts",
    crop_path: CropPath | None = None,
    zoom_expr: str | None = None,
    progress_bar_s: float | None = None,
    progress_colour: str = PROGRESS_BAR_COLOUR,
    accent: str = "#FFD400",
    emoji_inputs: list[tuple[int, float]] | None = None,
) -> str:
    """Build the video part of ``-filter_complex``; the final label is ``[vout]``."""
    out_w, out_h = ratio_dimensions(request.ratio)
    crop_path = crop_path or request.crop_path
    segments = crop_path.segments
    if not segments:
        raise ExportError("The crop path has no segments.")

    parts: list[str] = []
    labels: list[str] = []
    total = request.duration_s
    # Baked-in black bars: every segment first crops the active picture area, so all
    # crop / fit / split maths below works in active-area coordinates.
    active = crop_path.active
    active_crop = f"crop={active[2]}:{active[3]}:{active[0]}:{active[1]}" if active else None

    for index, segment in enumerate(segments):
        label = f"v{index}"
        labels.append(f"[{label}]")

        whole = len(segments) == 1 and segment.start_s <= 0.001 and segment.end_s >= total - 0.001
        if whole and active_crop is None:
            source_label = "[0:v]"
        else:
            source_label = f"[s{index}]"
            trim = (
                ""
                if whole
                else f"trim=start={segment.start_s:.4f}:end={segment.end_s:.4f},setpts=PTS-STARTPTS"
            )
            chain_parts = [p for p in (trim, active_crop) if p]
            parts.append(f"[0:v]{','.join(chain_parts)}{source_label}")

        if segment.layout == "split":
            parts.extend(_split_chain(source_label, index, segment, out_w, out_h, accent))
            continue
        if segment.fit:
            parts.extend(_fit_chain(source_label, index, out_w, out_h))
            continue

        chain = [segment_crop_filter(segment)]
        chain.append(f"scale={out_w}:{out_h}:flags=bilinear")
        chain.append("setsar=1,format=yuv420p")
        parts.append(f"{source_label}{','.join(chain)}[{label}]")

    if len(segments) == 1:
        current = "[v0]"
    else:
        parts.append(f"{''.join(labels)}concat=n={len(segments)}:v=1:a=0[vcat]")
        current = "[vcat]"

    if zoom_expr:
        parts.append(
            f"{current}scale=w='trunc({out_w}*{zoom_expr}/2)*2':"
            f"h='trunc({out_h}*{zoom_expr}/2)*2':eval=frame:flags=bilinear,"
            f"crop={out_w}:{out_h}:x='(iw-ow)/2':y='(ih-oh)*0.3',"
            f"setsar=1[vz]"
        )
        current = "[vz]"

    # Emoji pop in over the picture, under the captions. Each input is a
    # pre-rendered PNG sequence (see ``emoji_sequence``): animating with
    # ``scale eval=frame`` cost ~35% extra render time, a plain overlay is free.
    for k, (input_index, t0) in enumerate(emoji_inputs or []):
        parts.append(f"[{input_index}:v]format=rgba,setpts=PTS+{t0:.3f}/TB[em{k}]")
        parts.append(
            f"{current}[em{k}]overlay=x='{out_w * 0.80:.0f}-w/2':y='{out_h * 0.50:.0f}-h/2':"
            f"eof_action=pass[ve{k}]"
        )
        current = f"[ve{k}]"

    if subtitle_name is not None:
        # Bare relative names: ffmpeg runs with its cwd in the render workspace.
        parts.append(f"{current}ass=filename={subtitle_name}:fontsdir={fonts_name}[vsub]")
        current = "[vsub]"

    if progress_bar_s:
        parts.append(
            f"color=c={progress_colour}:s={out_w}x{PROGRESS_BAR_HEIGHT}:r=30:"
            f"d={progress_bar_s + 1:.3f}[bar]"
        )
        parts.append(
            f"{current}[bar]overlay=x='-w+w*t/{progress_bar_s:.3f}':y=0:shortest=1[vout]"
        )
    else:
        parts.append(f"{current}null[vout]")

    return ";".join(parts)


EMOJI_FPS = 30
EMOJI_SECONDS = 1.5


def emoji_sequence(png: Path, directory: Path, size: int) -> Path:
    """Render a pop-in/hold/fade-out PNG sequence for one emoji; returns the %03d pattern."""
    import math

    from PIL import Image

    directory.mkdir(parents=True, exist_ok=True)
    base = Image.open(png).convert("RGBA")
    canvas = round(size * 1.3)
    frames = round(EMOJI_SECONDS * EMOJI_FPS)
    for i in range(frames):
        t = i / EMOJI_FPS
        p = min(1.0, t / 0.22)
        scale = (0.35 + 0.65 * p) * (1 + 0.22 * math.sin(math.pi * p))
        fade = min(1.0, max(0.0, (EMOJI_SECONDS - t) / 0.25))
        side = max(2, round(size * scale))
        img = base.resize((side, side), Image.LANCZOS)
        if fade < 1.0:
            alpha = img.getchannel("A").point(lambda v, f=fade: int(v * f))
            img.putalpha(alpha)
        frame = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
        frame.paste(img, ((canvas - side) // 2, (canvas - side) // 2), img)
        frame.save(directory / f"{i:03d}.png")
    return directory / "%03d.png"


def _split_chain(
    source_label: str, index: int, segment: CropSegment, out_w: int, out_h: int, accent: str
) -> list[str]:
    """Slide/screen on top (~40%), face-tracked speaker crop below (~60%)."""
    top_h, bot_h = layout.panel_heights(out_h)
    qw, qh = out_w // 4 // 2 * 2, top_h // 4 // 2 * 2
    seam_colour = "0x" + accent.lstrip("#")
    return [
        f"{source_label}split=3[sa{index}][sb{index}][sc{index}]",
        (
            f"[sa{index}]scale={qw}:{qh}:force_original_aspect_ratio=increase,"
            f"crop={qw}:{qh},boxblur=5:1,scale={out_w}:{top_h}:flags=bilinear[tbg{index}]"
        ),
        (
            f"[sb{index}]scale={out_w}:{top_h}:force_original_aspect_ratio=decrease:"
            f"flags=bilinear[tfg{index}]"
        ),
        f"[tbg{index}][tfg{index}]overlay=(W-w)/2:(H-h)/2[top{index}]",
        (
            f"[sc{index}]{segment_crop_filter(segment)},"
            f"scale={out_w}:{bot_h}:flags=bilinear[bot{index}]"
        ),
        (
            f"[top{index}][bot{index}]vstack=inputs=2,"
            f"drawbox=x=0:y={top_h - 3}:w=iw:h=6:color={seam_colour}:t=fill,"
            f"setsar=1,format=yuv420p[v{index}]"
        ),
    ]


def _fit_chain(source_label: str, index: int, out_w: int, out_h: int) -> list[str]:
    """Fit the whole frame over a blurred copy of itself."""
    return [
        f"{source_label}split=2[fitbg{index}][fitfg{index}]",
        (
            # Blur a quarter-size copy, then scale back up: ~16x fewer pixels to blur.
            f"[fitbg{index}]scale={out_w // 4 // 2 * 2}:{out_h // 4 // 2 * 2}:"
            f"force_original_aspect_ratio=increase,crop={out_w // 4 // 2 * 2}:{out_h // 4 // 2 * 2},"
            f"boxblur=6:2,scale={out_w}:{out_h}:flags=bilinear[bg{index}]"
        ),
        (
            f"[fitfg{index}]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:"
            f"flags=bilinear[fg{index}]"
        ),
        (f"[bg{index}][fg{index}]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p[v{index}]"),
    ]


def build_audio_filtergraph(
    keep: list[tuple[float, float]],
    *,
    whole: bool,
    normalize: bool,
    loudness_lufs: float,
    music_input: int | None = None,
    music_volume: float = 0.12,
    edited_s: float = 0.0,
    sfx: list[tuple[int, float, float]] | None = None,
) -> str:
    """Audio chain ending in ``[aout]``: cuts -> (music duck) -> loudnorm."""
    parts: list[str] = []
    if whole:
        parts.append("[0:a]anull[acat]")
    else:
        for i, (a, b) in enumerate(keep):
            fade = min(0.012, (b - a) / 4)
            parts.append(
                f"[0:a]atrim=start={a:.4f}:end={b:.4f},asetpts=PTS-STARTPTS,"
                f"afade=t=in:d={fade:.3f},afade=t=out:st={max(0.0, b - a - fade):.4f}:d={fade:.3f}"
                f"[a{i}]"
            )
        parts.append("".join(f"[a{i}]" for i in range(len(keep))) + f"concat=n={len(keep)}:v=0:a=1[acat]")
    current = "[acat]"
    if music_input is not None:
        parts.append(f"{current}asplit=2[vo][vsc]")
        parts.append(
            f"[{music_input}:a]atrim=0:{edited_s + 1:.3f},asetpts=PTS-STARTPTS,"
            f"volume={music_volume}[mus]"
        )
        parts.append("[mus][vsc]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=350[duck]")
        parts.append("[vo][duck]amix=inputs=2:duration=first:normalize=0[amix]")
        current = "[amix]"
    if sfx:
        # sfx: (input index, start seconds on the edited timeline, volume)
        fmt = "aformat=sample_rates=44100:channel_layouts=stereo"
        parts.append(f"{current}{fmt}[vox]")
        labels = ["[vox]"]
        for k, (input_index, t, volume) in enumerate(sfx):
            ms = max(0, round(t * 1000))
            parts.append(f"[{input_index}:a]{fmt},adelay={ms}:all=1,volume={volume:.3f}[sfx{k}]")
            labels.append(f"[sfx{k}]")
        parts.append(
            "".join(labels) + f"amix=inputs={len(labels)}:duration=first:normalize=0[amixfx]"
        )
        current = "[amixfx]"
    if normalize:
        parts.append(
            f"{current}loudnorm=I={loudness_lufs}:TP={LOUDNESS_TRUE_PEAK}:LRA={LOUDNESS_RANGE}[aout]"
        )
    else:
        parts.append(f"{current}anull[aout]")
    return ";".join(parts)


def encoder_args(settings: ExportSettings) -> list[str]:
    """Pick the video encoder. Default is libx264 veryfast for weak CPUs."""
    if settings.prefer_hardware_encoder and report().ffmpeg.nvenc_works:
        return [
            "-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
            "-cq", str(settings.crf), "-b:v", "0", "-profile:v", "high",
        ]  # fmt: skip
    return [
        "-c:v", "libx264", "-preset", settings.x264_preset,
        "-crf", str(settings.crf), "-profile:v", "high",
        "-x264-params", f"threads={resources.render_threads()}",
    ]  # fmt: skip


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def export_clip(
    request: ExportRequest,
    *,
    work_dir: Path,
    settings: ExportSettings | None = None,
    edit: EditSettings | None = None,
    on_progress: Callable[[float], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Path:
    """Render one clip to an MP4 and return the output path."""
    settings = settings or ExportSettings()
    edit = edit or EditSettings()
    out_w, out_h = ratio_dimensions(request.ratio)
    render_started = time.monotonic()

    workspace = work_dir / request.destination.stem
    workspace.mkdir(parents=True, exist_ok=True)

    plan = make_plan(request, edit)
    edited_s = plan.duration_s

    subtitle_name: str | None = None
    fonts_name = "fonts"
    render_cwd: Path | None = None
    if (request.burn_captions and plan.words) or plan.hook:
        ass_path = captions_module.write_ass(
            workspace / "captions.ass",
            plan.words if request.burn_captions else [],
            plan.style or request.style,
            width=out_w,
            height=out_h,
            keywords=plan.keywords,
            lang=request.language,
            seam_intervals=plan.seam_intervals,
            hook=plan.hook,
            clip_duration_s=request.duration_s,
            zones=plan.zones,
            face_margin=request.caption_face_margin,
        )
        render_cwd, subtitle_name, fonts_name = ffmpeg.relative_filter_workspace(
            ass_path, captions_module.FONT_DIR
        )

    use_music = bool(edit.background_music and request.music_file and request.has_audio)
    inputs = ["-ss", f"{request.start_s:.4f}", "-t", f"{request.duration_s:.4f}",
              "-i", str(request.source)]  # fmt: skip
    next_input = 1
    if use_music:
        inputs += ["-stream_loop", "-1", "-i", str(request.music_file)]
        next_input += 1
    emoji_inputs: list[tuple[int, float]] = []
    for k, (t, png) in enumerate(plan.emoji):
        sequence = emoji_sequence(png, workspace / f"emoji{k}", round(out_w * 0.2))
        inputs += ["-framerate", str(EMOJI_FPS), "-i", str(sequence)]
        emoji_inputs.append((next_input, t))
        next_input += 1
    sfx_inputs: list[tuple[int, float, float]] = []
    sfx_volume = float(getattr(request.template, "sfx_volume", 0.3))
    if request.has_audio:
        for t, wav in plan.sfx:
            inputs += ["-i", str(wav)]
            sfx_inputs.append((next_input, t, sfx_volume))
            next_input += 1

    video = build_video_filtergraph(
        request,
        subtitle_name=subtitle_name,
        fonts_name=fonts_name,
        crop_path=plan.crop_path,
        zoom_expr=plan.zoom_expr,
        progress_bar_s=edited_s if plan.progress_bar else None,
        progress_colour=plan.progress_colour,
        accent=plan.accent,
        emoji_inputs=emoji_inputs,
    )

    filtergraph = video
    maps = ["-map", "[vout]"]
    if request.has_audio:
        filtergraph += ";" + build_audio_filtergraph(
            plan.plan.keep,
            whole=plan.plan.is_noop,
            normalize=edit.normalize_audio,
            loudness_lufs=settings.loudness_lufs,
            music_input=1 if use_music else None,
            music_volume=edit.music_volume,
            edited_s=edited_s,
            sfx=sfx_inputs,
        )
        maps += ["-map", "[aout]"]

    request.destination.parent.mkdir(parents=True, exist_ok=True)
    # Measured on the target i7-3770 (one 50 s jump-cut clip): letting ffmpeg pick
    # the thread counts took 64-68 s, forcing all logical cores 47 s.
    threads = str(resources.render_threads())
    args = [
        "-threads", threads, "-filter_threads", threads, "-filter_complex_threads", threads,
        *inputs,
        "-filter_complex", filtergraph,
        *maps,
        *encoder_args(settings),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", str(AUDIO_SAMPLE_RATE),
        "-t", f"{edited_s:.4f}",
        "-movflags", "+faststart",
        str(request.destination),
    ]  # fmt: skip

    try:
        # One render at a time, even when an API re-export overlaps a running job.
        with resources.render_lock:
            ffmpeg.run(
                args,
                total_duration_s=edited_s,
                on_progress=on_progress,
                cancelled=cancelled,
                cwd=render_cwd,
            )
    except ffmpeg.FFmpegError as exc:
        raise ExportError(f"Could not render {request.destination.name}.\n{exc}") from exc

    if not request.destination.exists() or request.destination.stat().st_size == 0:
        raise ExportError(f"ffmpeg reported success but {request.destination.name} is empty.")

    if settings.write_srt and plan.words:
        captions_module.write_srt(request.destination.with_suffix(".srt"), plan.words)

    log.info(
        "Exported %s in %.1fs (%.1fs of %.1fs, %s, %d cuts, %d split, %d zooms, "
        "%d emoji, %d sfx)",
        request.destination.name,
        time.monotonic() - render_started,
        edited_s,
        request.duration_s,
        request.ratio,
        max(0, len(plan.plan.keep) - 1),
        sum(1 for s in plan.crop_path.segments if s.layout == "split"),
        len(request.edit_plan.zooms) if request.edit_plan else 0,
        len(emoji_inputs),
        len(sfx_inputs),
    )
    return request.destination
