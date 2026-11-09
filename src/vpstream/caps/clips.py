"""Generate the small test clips that the capability page plays."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from vpstream import __version__
from vpstream.ffargs import aac_args, base_args, hls_output_args
from vpstream.tools import Result, Runner, ffmpeg_names, low_priority_prefix, require, tail

CODEC_SECONDS = 4
LONG_SECONDS = 20
SUB_CUE_SECONDS = 2
CLIP_SEGMENT_SECONDS = 2
AUDIO_BITRATE = "128k"
ENCODE_TIMEOUT = 600
REMUX_TIMEOUT = 120
# Marks a folder as made by vpstream, so it is the only kind that is ever deleted.
MARKER = ".vpstream-caps"


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


def _map_inputs(audio_tracks: int) -> list[str]:
    args = ["-map", "0:v"]
    for n in range(1, audio_tracks + 1):
        args += ["-map", f"{n}:a"]
    return args


def test_clip_args(
    out: Path,
    *,
    encoder: str,
    pix_fmt: str,
    seconds: int,
    size: str,
    gop: int,
    extra: tuple[str, ...] = (),
) -> list[str]:
    """A test pattern with a 440 Hz tone, encoded into a fast-start MP4."""
    return [
        *base_args(),
        *_lavfi_inputs(seconds, size, (440,)),
        *_map_inputs(1),
        "-c:v",
        encoder,
        *ENCODER_SPEED.get(encoder, ()),
        "-pix_fmt",
        pix_fmt,
        *extra,
        "-g",
        str(gop),
        *aac_args(AUDIO_BITRATE),
        "-shortest",
        "-movflags",
        "+faststart",
        str(out),
    ]


def codec_clip_args(spec: ClipSpec, encoder: str, out: Path) -> list[str]:
    return test_clip_args(
        out,
        encoder=encoder,
        pix_fmt=spec.pix_fmt,
        seconds=CODEC_SECONDS,
        size="640x360",
        gop=30,
        extra=spec.extra,
    )


def long_clip_args(out: Path) -> list[str]:
    return test_clip_args(
        out, encoder="libx264", pix_fmt="yuv420p", seconds=LONG_SECONDS, size="1280x720", gop=60
    )


def to_hls_args(src: Path, out_dir: Path, *, fmp4: bool = True) -> list[str]:
    return [
        *base_args(),
        "-i",
        str(src),
        "-map",
        "0",
        "-c",
        "copy",
        *hls_output_args(out_dir, fmp4=fmp4, seconds=CLIP_SEGMENT_SECONDS, segment="seg_%03d"),
    ]


def multi_audio_args(out_dir: Path) -> list[str]:
    """One H.264 video with two alternate AAC audio tracks: 440 Hz (eng), 880 Hz (jpn)."""
    stream_map = (
        "v:0,agroup:aud "
        "a:0,agroup:aud,language:eng,name:English,default:yes "
        "a:1,agroup:aud,language:jpn,name:Japanese"
    )
    return [
        *base_args(),
        *_lavfi_inputs(LONG_SECONDS, "1280x720", (440, 880)),
        *_map_inputs(2),
        "-c:v",
        "libx264",
        *ENCODER_SPEED["libx264"],
        "-pix_fmt",
        "yuv420p",
        "-g",
        "60",
        *aac_args(AUDIO_BITRATE),
        *hls_output_args(
            out_dir,
            seconds=CLIP_SEGMENT_SECONDS,
            segment="s_%v_%03d",
            playlist="v_%v.m3u8",
            extra=("-master_pl_name", "master.m3u8", "-var_stream_map", stream_map),
        ),
    ]


def webvtt_text(seconds: int = LONG_SECONDS, step: int = SUB_CUE_SECONDS) -> str:
    lines = ["WEBVTT", ""]
    for n, start in enumerate(range(0, seconds, step), start=1):
        end = start + step
        lines += [f"00:00:{start:02d}.000 --> 00:00:{end:02d}.000", f"Subtitle line {n}", ""]
    return "\n".join(lines)


class ClipDirError(ValueError):
    pass


def _read_manifest(path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def made_by_vpstream(out: Path) -> bool:
    """The marker file, or a clip manifest from a build that predates the marker."""
    if (out / MARKER).is_file():
        return True
    manifest = _read_manifest(out / "clips.json")
    return manifest is not None and {"version", "clips", "extras"} <= manifest.keys()


def prepare_dir(out: Path) -> None:
    """Make `out` empty, deleting it first only when vpstream made it."""
    if out.exists():
        if not out.is_dir():
            raise ClipDirError(f"{out} is not a folder")
        if made_by_vpstream(out):
            shutil.rmtree(out)
        elif any(out.iterdir()):
            raise ClipDirError(
                f"{out} is not empty and was not made by vpstream; "
                "choose an empty or new folder for --clips"
            )
    out.mkdir(parents=True, exist_ok=True)
    (out / MARKER).write_text("Test clips made by vpstream caps. Safe to delete.\n")


def generate(
    runner: Runner,
    out: Path,
    *,
    regen: bool = False,
    nice: bool = True,
    progress: Callable[[str], None] = lambda _m: None,
) -> dict[str, object]:
    """Make every clip in `out` (reused when already there) and return the manifest.

    Raises ClipDirError instead of deleting a folder that vpstream did not make.
    """
    manifest_path = out / "clips.json"
    if not regen and made_by_vpstream(out):
        cached = _read_manifest(manifest_path)
        if cached is not None:
            return cached
    require(runner, "ffmpeg")
    prepare_dir(out)
    encoders = ffmpeg_names(runner, "encoders")
    prefix = low_priority_prefix(runner) if nice else []

    def ffmpeg(args: list[str], timeout: float) -> Result:
        return runner.run([*prefix, *args], timeout=timeout)

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
        res = ffmpeg(codec_clip_args(spec, enc, mp4), ENCODE_TIMEOUT)
        if res.returncode != 0:
            info.error = tail(res, default="ffmpeg failed")
            continue
        info.mp4 = mp4.name
        hls_dir = out / spec.id
        hls_dir.mkdir()
        res = ffmpeg(to_hls_args(mp4, hls_dir), REMUX_TIMEOUT)
        if res.returncode == 0:
            info.hls = f"{spec.id}/index.m3u8"
        else:
            info.error = "HLS: " + tail(res, default="ffmpeg failed")

    extras: dict[str, str | None] = {"ts": None, "long": None, "multi_audio": None}
    errors: dict[str, str] = {}

    def extra(key: str, args: list[str], result: Path, timeout: float) -> None:
        """Make one extra clip; record its path relative to `out`, or the error."""
        res = ffmpeg(args, timeout)
        if res.returncode == 0 and result.is_file():
            extras[key] = result.relative_to(out).as_posix()
        else:
            errors[key] = tail(res, default=f"ffmpeg made no {result.name}")

    h264 = out / "h264-8.mp4"
    if h264.exists():
        ts_dir = out / "h264-8-ts"
        ts_dir.mkdir()
        extra("ts", to_hls_args(h264, ts_dir, fmp4=False), ts_dir / "index.m3u8", REMUX_TIMEOUT)

    if "libx264" in encoders:
        progress("clip for subtitles (20 s)")
        long = out / "long.mp4"
        extra("long", long_clip_args(long), long, ENCODE_TIMEOUT)
        progress("clip with two audio tracks (20 s)")
        ma = out / "multi-audio"
        ma.mkdir()
        extra("multi_audio", multi_audio_args(ma), ma / "master.m3u8", ENCODE_TIMEOUT)

    (out / "subs.vtt").write_text(webvtt_text())
    manifest: dict[str, object] = {
        "version": __version__,
        "clips": [asdict(c) for c in clips],
        "extras": extras,
        "vtt": "subs.vtt",
        "vtt_cue_seconds": SUB_CUE_SECONDS,
        "errors": errors,
    }
    # Written last and atomically, so a folder with a manifest always has every clip.
    tmp = manifest_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2))
    tmp.replace(manifest_path)
    return manifest
