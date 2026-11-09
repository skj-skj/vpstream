"""`vpstream bench FILE`: measure how fast this host prepares a file."""

from __future__ import annotations

import json
import os
import platform
import re
import resource
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from vpstream import __version__
from vpstream.ffargs import (
    BROWSER_COPY_CODECS,
    EncodeSettings,
    Window,
    burn_hls,
    copy_hls,
    escape_filter_path,
    transcode_hls,
)
from vpstream.probe import MediaInfo, SubtitleTrack, probe
from vpstream.tools import Runner, ffmpeg_names, low_priority_prefix, require

# Builds a step's ffmpeg command for the folder it should write into.
ArgsBuilder = Callable[[Path], list[str]]


@dataclass
class StepResult:
    name: str
    ok: bool
    media_seconds: float
    wall_seconds: float = 0.0
    cpu_seconds: float = 0.0
    output_bytes: int = 0
    note: str = ""
    error: str = ""
    skipped: bool = False
    font_warnings: list[str] = field(default_factory=list)

    @property
    def speed(self) -> float:
        return self.media_seconds / self.wall_seconds if self.wall_seconds > 0 else 0.0

    @property
    def output_mbps(self) -> float:
        if self.media_seconds <= 0:
            return 0.0
        return self.output_bytes * 8 / self.media_seconds / 1_000_000


@dataclass
class BenchReport:
    file: str
    duration: float
    size: int
    video: str
    window_start: float
    window_length: float
    host: dict[str, Any]
    steps: list[StepResult]

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        for step, raw in zip(self.steps, data["steps"], strict=True):
            raw["speed"] = round(step.speed, 3)
            raw["output_mbps"] = round(step.output_mbps, 3)
            raw["estimated_full_seconds"] = (
                round(self.duration / step.speed, 1)
                if step.speed > 0 and self.duration > 0
                else None
            )
        return data


def plan_window(duration: float, sample: float) -> Window:
    """Pick a sample from about 10% into the file, so intros and black frames are skipped."""
    if sample <= 0 or duration <= 0:
        return Window(0.0, None if sample <= 0 else sample)
    if duration <= sample:
        return Window(0.0, duration)
    start = min(duration * 0.1, duration - sample)
    return Window(round(start, 3), sample)


def pick_burn_track(info: MediaInfo, number: int | None) -> SubtitleTrack | None:
    """The subtitle track to burn: the one asked for, else the first image or ASS track."""
    if number is not None:
        for t in info.subtitles:
            if t.number == number:
                return t
        raise ValueError(f"no subtitle track {number}; this file has {len(info.subtitles)}")
    for t in info.subtitles:
        if t.treatment == "burned in":
            return t
    return None


# libass: "fontselect: (Arial, 400, 0) -> /usr/share/fonts/DejaVuSans.ttf, 0, DejaVuSans"
_FONTSELECT = re.compile(r"fontselect: \((?P<asked>[^,]*),.*?\) -> (?P<got>.*)$")
_FONT_PROBLEMS = ("glyph", "font not found", "no font", "fallback")


def _font_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def font_replaced(line: str) -> bool:
    """True for a libass fontselect line that picked a font other than the one asked for."""
    m = _FONTSELECT.search(line)
    if not m:
        return False
    asked = _font_key(m["asked"])
    got = m["got"].split(",")
    candidates = [Path(got[0].strip()).stem, got[-1]]
    return not any(_font_key(c).startswith(asked) for c in candidates if c.strip())


def font_warnings(stderr: str) -> list[str]:
    """libass lines that say a font was missing or replaced by another one."""
    out = []
    for line in stderr.splitlines():
        low = line.lower()
        if font_replaced(line) or (
            "fontselect" not in low and any(k in low for k in _FONT_PROBLEMS)
        ):
            out.append(line.strip())
    return out[:50]


def describe_video(info: MediaInfo) -> str:
    if not info.video:
        return "no video track"
    v = info.video[0]
    dyn = "HDR" if v.hdr else "SDR"
    return f"{v.codec} {v.profile} {v.width}x{v.height} {v.bit_depth}-bit {dyn} {v.fps:g} fps"


def host_info(runner: Runner) -> dict[str, Any]:
    model = ""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith(("model name", "hardware")):
                model = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    ff = runner.run(["ffmpeg", "-hide_banner", "-version"], timeout=30)
    return {
        "vpstream": __version__,
        "python": platform.python_version(),
        "cpus": os.cpu_count(),
        "cpu_model": model,
        "ffmpeg": ff.stdout.splitlines()[0] if ff.stdout else "",
    }


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _children_cpu() -> float:
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


def skipped_step(name: str, media_seconds: float, note: str) -> StepResult:
    return StepResult(name=name, ok=False, media_seconds=media_seconds, skipped=True, note=note)


def run_step(
    runner: Runner,
    name: str,
    build: ArgsBuilder,
    work: Path,
    media_seconds: float,
    prefix: list[str],
    note: str = "",
    check_fonts: bool = False,
) -> StepResult:
    out = work / name.replace(" ", "_")
    out.mkdir(parents=True, exist_ok=True)
    cpu0 = _children_cpu()
    t0 = time.monotonic()
    res = runner.run([*prefix, *build(out)])
    wall = time.monotonic() - t0
    step = StepResult(
        name=name,
        ok=res.returncode == 0,
        media_seconds=media_seconds,
        wall_seconds=wall,
        cpu_seconds=round(_children_cpu() - cpu0, 3),
        output_bytes=_dir_size(out),
        note=note,
    )
    if not step.ok:
        step.error = "\n".join(res.stderr.strip().splitlines()[-15:])
    if check_fonts:
        step.font_warnings = font_warnings(res.stderr)
    shutil.rmtree(out, ignore_errors=True)
    return step


def filter_source(work: Path, path: Path) -> Path:
    """Link the source to a plain name, because the subtitles filter reads it by name.

    Raises ValueError or OSError when the temporary folder cannot be used for this.
    """
    link = work / ("source" + path.suffix.lower())
    if not re.fullmatch(r"\.[a-z0-9]+", link.suffix):
        link = work / "source"
    escape_filter_path(link)
    os.symlink(path.resolve(), link)
    return link


def benchmark(
    runner: Runner,
    path: Path,
    *,
    sample: float = 120.0,
    presets: tuple[str, ...] = ("veryfast", "medium"),
    sub: int | None = None,
    tmp_dir: str | None = None,
    nice: bool = True,
    progress: Callable[[str], None] = lambda _m: None,
) -> BenchReport:
    require(runner, "ffmpeg")
    info = probe(runner, path)
    if not info.video:
        raise ValueError(f"{path} has no video track")
    v = info.video[0]
    window = plan_window(info.duration, sample)
    media_seconds = window.length if window.length is not None else info.duration
    encoders = ffmpeg_names(runner, "encoders")
    filters = ffmpeg_names(runner, "filters")
    if "libx264" not in encoders:
        raise ValueError("this ffmpeg has no libx264 encoder")
    tonemap = {"zscale", "tonemap"} <= filters
    prefix = low_priority_prefix(runner) if nice else []
    burn = pick_burn_track(info, sub)
    src = str(path)
    steps: list[StepResult] = []

    def step(name: str, build: ArgsBuilder, note: str = "", check_fonts: bool = False) -> None:
        steps.append(run_step(runner, name, build, work, media_seconds, prefix, note, check_fonts))

    with tempfile.TemporaryDirectory(prefix="vpstream-bench-", dir=tmp_dir) as tmp:
        work = Path(tmp)

        progress("copy to HLS")
        note = "" if v.codec in BROWSER_COPY_CODECS else f"{v.codec} copy is unlikely to play"
        step("copy", lambda out: copy_hls(src, out, video_codec=v.codec, window=window), note)

        hdr_note = ""
        if v.hdr:
            hdr_note = "HDR tone mapped" if tonemap else "HDR, no zscale: not tone mapped"
        for preset in presets:
            progress(f"transcode to H.264 ({preset})")
            build = partial(
                transcode_hls,
                src,
                hdr=v.hdr,
                tonemap=tonemap,
                window=window,
                settings=EncodeSettings(preset=preset),
            )
            step(f"transcode {preset}", build, hdr_note)

        preset = presets[0]
        if burn is None:
            steps.append(
                skipped_step(
                    "burn-in", media_seconds, "skipped: no image, ASS or SSA subtitle track"
                )
            )
        elif burn.kind != "image" and "subtitles" not in filters:
            steps.append(
                skipped_step(
                    "burn-in", media_seconds, "skipped: this ffmpeg has no subtitles filter"
                )
            )
        else:
            image = burn.kind == "image"
            try:
                link = filter_source(work, path)
            except (OSError, ValueError) as exc:
                steps.append(
                    skipped_step(
                        f"burn-in {preset}",
                        media_seconds,
                        f"skipped: the temporary folder cannot be used ({exc}); "
                        "pass --tmp with a plain path on a disk that allows links",
                    )
                )
            else:
                progress(f"burn in subtitle {burn.number} ({burn.codec}, {preset})")
                fonts = "" if image else f", {info.attachments} embedded fonts"
                step(
                    f"burn-in {preset}",
                    lambda out: burn_hls(
                        src,
                        out,
                        sub_number=burn.number,
                        image=image,
                        filter_src=link,
                        hdr=v.hdr,
                        tonemap=tonemap,
                        window=window,
                        settings=EncodeSettings(preset=preset),
                        loglevel="error" if image else "info",
                    ),
                    f"subtitle {burn.number} {burn.codec} {burn.language}{fonts}",
                    check_fonts=not image,
                )

    return BenchReport(
        file=str(path),
        duration=info.duration,
        size=info.size,
        video=describe_video(info),
        window_start=window.start,
        window_length=media_seconds,
        host=host_info(runner),
        steps=steps,
    )


def fmt_duration(seconds: float) -> str:
    s = round(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"


def format_report(r: BenchReport) -> str:
    gb = r.size / 1e9
    lines = [
        f"{Path(r.file).name} · {fmt_duration(r.duration)} · {gb:.1f} GB",
        f"Video: {r.video}",
        f"Sample: {r.window_length:g} s from {fmt_duration(r.window_start)}"
        f" · {r.host.get('cpus')} CPUs · {r.host.get('cpu_model', '')}".rstrip(" ·"),
        "",
        f"{'Step':<20} {'Speed':>8} {'Full film':>11} {'Output':>13}  Note",
    ]
    for s in r.steps:
        if s.skipped:
            lines.append(f"{s.name:<20} {'-':>8} {'-':>11} {'-':>13}  {s.note}")
            continue
        if not s.ok:
            lines.append(f"{s.name:<20} {'FAILED':>8} {'-':>11} {'-':>13}  {s.note}")
            lines += ["    " + e for e in s.error.splitlines()]
            continue
        full = fmt_duration(r.duration / s.speed) if s.speed and r.duration else "-"
        lines.append(
            f"{s.name:<20} {s.speed:>7.2f}x {full:>11} {s.output_mbps:>8.1f} Mb/s  {s.note}"
        )
        for w in s.font_warnings[:10]:
            lines.append(f"    font: {w}")
    slow = [s for s in r.steps if s.ok and 0 < s.speed < 1]
    if slow:
        lines += [
            "",
            "Steps below 1x take longer than the film itself: " + ", ".join(s.name for s in slow),
        ]
    return "\n".join(lines)


def write_json(report: BenchReport, path: Path) -> None:
    path.write_text(json.dumps(report.to_json(), indent=2) + "\n")
