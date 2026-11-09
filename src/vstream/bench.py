"""`vstream bench FILE`: measure how fast this host prepares a file."""

from __future__ import annotations

import json
import os
import platform
import resource
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from vstream import __version__
from vstream.ffargs import (
    BROWSER_COPY_CODECS,
    EncodeSettings,
    Window,
    burn_hls,
    copy_hls,
    parse_ffmpeg_list,
    transcode_hls,
)
from vstream.probe import MediaInfo, SubtitleTrack, probe
from vstream.tools import Runner, low_priority_prefix, require


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
                round(self.duration / step.speed, 1) if step.speed > 0 else None
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


def font_warnings(stderr: str) -> list[str]:
    """libass lines that suggest a font was missing or replaced."""
    keys = ("fontselect", "glyph", "font not found", "no font", "fallback")
    out = []
    for line in stderr.splitlines():
        low = line.lower()
        if any(k in low for k in keys):
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
        "vstream": __version__,
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


def run_step(
    runner: Runner,
    name: str,
    args: list[str],
    work: Path,
    media_seconds: float,
    prefix: list[str],
    note: str = "",
    check_fonts: bool = False,
) -> StepResult:
    out = work / name.replace(" ", "_")
    out.mkdir(parents=True, exist_ok=True)
    # Builders write into a placeholder folder; point them at this step's own folder.
    args = [a.replace("__OUT__", str(out)) for a in args]
    cpu0 = _children_cpu()
    t0 = time.monotonic()
    res = runner.run([*prefix, *args])
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
    enc = runner.run(["ffmpeg", "-hide_banner", "-encoders"], timeout=30)
    flt = runner.run(["ffmpeg", "-hide_banner", "-filters"], timeout=30)
    encoders = parse_ffmpeg_list(enc.stdout)
    filters = parse_ffmpeg_list(flt.stdout)
    if "libx264" not in encoders:
        raise ValueError("this ffmpeg has no libx264 encoder")
    tonemap = {"zscale", "tonemap"} <= filters
    prefix = low_priority_prefix(runner) if nice else []
    burn = pick_burn_track(info, sub)
    steps: list[StepResult] = []
    with tempfile.TemporaryDirectory(prefix="vstream-bench-", dir=tmp_dir) as tmp:
        work = Path(tmp)
        src = str(path)

        progress("copy to HLS")
        note = "" if v.codec in BROWSER_COPY_CODECS else f"{v.codec} copy is unlikely to play"
        steps.append(
            run_step(
                runner,
                "copy",
                copy_hls(src, "__OUT__", video_codec=v.codec, window=window),
                work,
                media_seconds,
                prefix,
                note,
            )
        )

        hdr_note = ""
        if v.hdr:
            hdr_note = "HDR tone mapped" if tonemap else "HDR, no zscale: not tone mapped"
        for preset in presets:
            progress(f"transcode to H.264 ({preset})")
            s = EncodeSettings(preset=preset)
            steps.append(
                run_step(
                    runner,
                    f"transcode {preset}",
                    transcode_hls(
                        src, "__OUT__", hdr=v.hdr, tonemap=tonemap, window=window, settings=s
                    ),
                    work,
                    media_seconds,
                    prefix,
                    hdr_note,
                )
            )

        if burn is None:
            steps.append(
                StepResult(
                    name="burn-in",
                    ok=False,
                    media_seconds=media_seconds,
                    skipped=True,
                    note="skipped: no image, ASS or SSA subtitle track",
                )
            )
        else:
            image = burn.kind == "image"
            if not image and "subtitles" not in filters:
                steps.append(
                    StepResult(
                        name="burn-in",
                        ok=False,
                        media_seconds=media_seconds,
                        skipped=True,
                        note="skipped: this ffmpeg has no subtitles filter",
                    )
                )
            else:
                # The subtitles filter reads the file by name; link it to a safe name.
                link = work / ("source" + path.suffix.lower())
                if not link.suffix[1:].isalnum():
                    link = work / "source"
                os.symlink(path.resolve(), link)
                preset = presets[0]
                progress(f"burn in subtitle {burn.number} ({burn.codec}, {preset})")
                steps.append(
                    run_step(
                        runner,
                        f"burn-in {preset}",
                        burn_hls(
                            src,
                            "__OUT__",
                            sub_number=burn.number,
                            image=image,
                            filter_src=link,
                            hdr=v.hdr,
                            tonemap=tonemap,
                            window=window,
                            settings=EncodeSettings(preset=preset),
                            loglevel="error" if image else "info",
                        ),
                        work,
                        media_seconds,
                        prefix,
                        f"subtitle {burn.number} {burn.codec} {burn.language}"
                        + (f", {info.attachments} embedded fonts" if not image else ""),
                        check_fonts=not image,
                    )
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
