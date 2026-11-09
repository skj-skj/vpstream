"""Generate the small test clips that the capability page plays."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from vpstream import __version__
from vpstream.ffargs import parse_ffmpeg_list
from vpstream.tools import Runner, require

CODEC_SECONDS = 4
LONG_SECONDS = 20
SUB_CUE_SECONDS = 2


@dataclass(frozen=True)
class ClipSpec:
    id: str
    label: str
    codec: str  # RFC 6381 codec string the browser is asked about
    encoders: tuple[str, ...]  # first one available is used
    pix_fmt: str
    extra: tuple[str, ...] = ()


CODEC_CLIPS: tuple[ClipSpec, ...] = (
    ClipSpec(
        "h264-8", "H.264 8-bit", "avc1.64001e", ("libx264",), "yuv420p", ("-profile:v", "high")
    ),
    ClipSpec(
        "h264-10",
        "H.264 10-bit",
        "avc1.6e001e",
        ("libx264",),
        "yuv420p10le",
        ("-profile:v", "high10"),
    ),
    ClipSpec(
        "hevc-8",
        "HEVC 8-bit",
        "hvc1.1.6.L90.B0",
        ("libx265",),
        "yuv420p",
        ("-tag:v", "hvc1", "-x265-params", "log-level=error"),
    ),
    ClipSpec(
        "hevc-10",
        "HEVC 10-bit",
        "hvc1.2.4.L90.B0",
        ("libx265",),
        "yuv420p10le",
        ("-tag:v", "hvc1", "-profile:v", "main10", "-x265-params", "log-level=error"),
    ),
    ClipSpec("av1-8", "AV1 8-bit", "av01.0.04M.08", ("libsvtav1", "libaom-av1"), "yuv420p"),
    ClipSpec("av1-10", "AV1 10-bit", "av01.0.04M.10", ("libsvtav1", "libaom-av1"), "yuv420p10le"),
)

ENCODER_SPEED: dict[str, tuple[str, ...]] = {
    "libx264": ("-preset", "veryfast"),
    "libx265": ("-preset", "fast"),
    "libsvtav1": ("-preset", "10"),
    "libaom-av1": ("-cpu-used", "8", "-row-mt", "1"),
}


@dataclass
class ClipInfo:
    id: str
    label: str
    codec: str
    mp4: str | None = None
    hls: str | None = None
    encoder: str = ""
    error: str = ""


def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "vpstream" / "caps" / __version__


def _lavfi_inputs(seconds: int, size: str, freqs: tuple[int, ...]) -> list[str]:
    args = ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration={seconds}"]
    for f in freqs:
        args += ["-f", "lavfi", "-i", f"sine=frequency={f}:duration={seconds}"]
    return args


def codec_clip_args(spec: ClipSpec, encoder: str, out: Path) -> list[str]:
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *_lavfi_inputs(CODEC_SECONDS, "640x360", (440,)),
        "-map",
        "0:v",
        "-map",
        "1:a",
        "-c:v",
        encoder,
        *ENCODER_SPEED.get(encoder, ()),
        "-pix_fmt",
        spec.pix_fmt,
        *spec.extra,
        "-g",
        "30",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-shortest",
        "-movflags",
        "+faststart",
        str(out),
    ]


def to_hls_args(src: Path, out_dir: Path, *, fmp4: bool = True, seconds: int = 2) -> list[str]:
    ext = "m4s" if fmp4 else "ts"
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-map",
        "0",
        "-c",
        "copy",
        "-f",
        "hls",
        "-hls_time",
        str(seconds),
        "-hls_playlist_type",
        "vod",
        "-hls_segment_type",
        "fmp4" if fmp4 else "mpegts",
        "-hls_segment_filename",
        str(out_dir / f"seg_%03d.{ext}"),
        str(out_dir / "index.m3u8"),
    ]


def long_clip_args(out: Path) -> list[str]:
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *_lavfi_inputs(LONG_SECONDS, "1280x720", (440,)),
        "-map",
        "0:v",
        "-map",
        "1:a",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "60",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-shortest",
        "-movflags",
        "+faststart",
        str(out),
    ]


def multi_audio_args(out_dir: Path) -> list[str]:
    """One H.264 video with two alternate AAC audio tracks: 440 Hz (eng), 880 Hz (jpn)."""
    stream_map = (
        "v:0,agroup:aud "
        "a:0,agroup:aud,language:eng,name:English,default:yes "
        "a:1,agroup:aud,language:jpn,name:Japanese"
    )
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *_lavfi_inputs(LONG_SECONDS, "1280x720", (440, 880)),
        "-map",
        "0:v",
        "-map",
        "1:a",
        "-map",
        "2:a",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "60",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-f",
        "hls",
        "-hls_time",
        "2",
        "-hls_playlist_type",
        "vod",
        "-hls_segment_type",
        "fmp4",
        "-master_pl_name",
        "master.m3u8",
        "-var_stream_map",
        stream_map,
        "-hls_segment_filename",
        str(out_dir / "s_%v_%03d.m4s"),
        str(out_dir / "v_%v.m3u8"),
    ]


def webvtt_text(seconds: int = LONG_SECONDS, step: int = SUB_CUE_SECONDS) -> str:
    lines = ["WEBVTT", ""]
    for n, start in enumerate(range(0, seconds, step), start=1):
        end = start + step
        lines += [f"00:00:{start:02d}.000 --> 00:00:{end:02d}.000", f"Subtitle line {n}", ""]
    return "\n".join(lines)


def generate(
    runner: Runner,
    out: Path,
    *,
    regen: bool = False,
    progress: Callable[[str], None] = lambda _m: None,
) -> dict[str, object]:
    """Make every clip in `out` (reused when already there) and return the manifest."""
    manifest_path = out / "clips.json"
    if manifest_path.exists() and not regen:
        data: dict[str, object] = json.loads(manifest_path.read_text())
        return data
    require(runner, "ffmpeg")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    encoders = parse_ffmpeg_list(runner.run(["ffmpeg", "-hide_banner", "-encoders"]).stdout)

    clips: list[ClipInfo] = []
    for spec in CODEC_CLIPS:
        info = ClipInfo(spec.id, spec.label, spec.codec)
        clips.append(info)
        enc = next((e for e in spec.encoders if e in encoders), None)
        if enc is None:
            info.error = "no encoder in this ffmpeg: " + " or ".join(spec.encoders)
            continue
        info.encoder = enc
        progress(f"clip {spec.label} ({enc})")
        mp4 = out / f"{spec.id}.mp4"
        res = runner.run(codec_clip_args(spec, enc, mp4), timeout=600)
        if res.returncode != 0:
            info.error = res.stderr.strip()[-300:] or "ffmpeg failed"
            continue
        info.mp4 = mp4.name
        hls_dir = out / spec.id
        hls_dir.mkdir()
        res = runner.run(to_hls_args(mp4, hls_dir), timeout=120)
        if res.returncode == 0:
            info.hls = f"{spec.id}/index.m3u8"
        else:
            info.error = "HLS: " + (res.stderr.strip()[-300:] or "ffmpeg failed")

    extras: dict[str, str | None] = {"ts": None, "long": None, "multi_audio": None}
    errors: dict[str, str] = {}

    h264 = out / "h264-8.mp4"
    if h264.exists():
        ts_dir = out / "h264-8-ts"
        ts_dir.mkdir()
        res = runner.run(to_hls_args(h264, ts_dir, fmp4=False), timeout=120)
        if res.returncode == 0:
            extras["ts"] = "h264-8-ts/index.m3u8"
        else:
            errors["ts"] = res.stderr.strip()[-300:]

    if "libx264" in encoders:
        progress("clip for subtitles (20 s)")
        res = runner.run(long_clip_args(out / "long.mp4"), timeout=600)
        if res.returncode == 0:
            extras["long"] = "long.mp4"
        else:
            errors["long"] = res.stderr.strip()[-300:]
        progress("clip with two audio tracks (20 s)")
        ma = out / "multi-audio"
        ma.mkdir()
        res = runner.run(multi_audio_args(ma), timeout=600)
        if res.returncode == 0 and (ma / "master.m3u8").exists():
            extras["multi_audio"] = "multi-audio/master.m3u8"
        else:
            errors["multi_audio"] = res.stderr.strip()[-300:] or "no master playlist"

    (out / "subs.vtt").write_text(webvtt_text())
    manifest: dict[str, object] = {
        "version": __version__,
        "clips": [asdict(c) for c in clips],
        "extras": extras,
        "vtt": "subs.vtt",
        "vtt_cue_seconds": SUB_CUE_SECONDS,
        "errors": errors,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest
