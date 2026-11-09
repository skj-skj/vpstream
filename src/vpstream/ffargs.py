"""Pure builders for ffmpeg command lines.

Nothing here runs a program, so every builder is unit tested. The same settings will be
used by `vpstream prepare` in a later milestone, which keeps `bench` numbers honest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

HLS_SEGMENT_SECONDS = 6
# Codecs a browser may play without a transcode (subject to `vpstream caps` on the TV).
BROWSER_COPY_CODECS = frozenset({"h264", "hevc", "av1"})

_SAFE_FILTER_PATH = re.compile(r"^[A-Za-z0-9_./+-]+$")

# Tone mapping from HDR (PQ or HLG) to standard-range BT.709, the usual zscale recipe.
TONEMAP_CHAIN = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv"
)


@dataclass(frozen=True)
class EncodeSettings:
    preset: str = "veryfast"
    crf: int = 21
    maxrate: str = "8M"
    bufsize: str = "16M"
    audio_bitrate: str = "160k"
    audio_channels: int = 2


@dataclass(frozen=True)
class Window:
    """Part of the source to read: start and length in seconds. length None means all."""

    start: float = 0.0
    length: float | None = None


# Frozen, so they are safe to share as defaults.
WHOLE_FILE = Window()
DEFAULT_SETTINGS = EncodeSettings()


def base_args(loglevel: str = "error") -> list[str]:
    return ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", loglevel, "-y"]


def input_args(src: str, window: Window, *, copyts: bool = False) -> list[str]:
    args: list[str] = []
    if window.start > 0:
        args += ["-ss", f"{window.start:.3f}"]
    if window.length is not None:
        args += ["-t", f"{window.length:.3f}"]
    if copyts:
        args += ["-copyts"]
    return [*args, "-i", src]


def audio_args(s: EncodeSettings) -> list[str]:
    return [
        "-map",
        "0:a?",
        "-c:a",
        "aac",
        "-b:a",
        s.audio_bitrate,
        "-ac",
        str(s.audio_channels),
    ]


def hls_output_args(out_dir: str | Path, *, fmp4: bool = True) -> list[str]:
    out = Path(out_dir)
    ext = "m4s" if fmp4 else "ts"
    return [
        "-f",
        "hls",
        "-hls_time",
        str(HLS_SEGMENT_SECONDS),
        "-hls_playlist_type",
        "vod",
        "-hls_flags",
        "independent_segments",
        "-hls_segment_type",
        "fmp4" if fmp4 else "mpegts",
        "-hls_segment_filename",
        str(out / f"seg_%05d.{ext}"),
        str(out / "index.m3u8"),
    ]


def keyframe_args() -> list[str]:
    return ["-force_key_frames", f"expr:gte(t,n_forced*{HLS_SEGMENT_SECONDS})"]


def h264_video_args(s: EncodeSettings) -> list[str]:
    return [
        "-c:v",
        "libx264",
        "-preset",
        s.preset,
        "-crf",
        str(s.crf),
        "-maxrate",
        s.maxrate,
        "-bufsize",
        s.bufsize,
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
        *keyframe_args(),
    ]


def copy_hls(
    src: str,
    out_dir: str | Path,
    *,
    video_codec: str,
    window: Window = WHOLE_FILE,
    settings: EncodeSettings = DEFAULT_SETTINGS,
) -> list[str]:
    """Copy the video into fMP4 HLS. Audio becomes AAC, as browser mode needs."""
    tag = ["-tag:v", "hvc1"] if video_codec == "hevc" else []
    return [
        *base_args(),
        *input_args(src, window),
        "-map",
        "0:v:0",
        "-c:v",
        "copy",
        *tag,
        *audio_args(settings),
        "-sn",
        "-dn",
        *hls_output_args(out_dir),
    ]


def transcode_hls(
    src: str,
    out_dir: str | Path,
    *,
    hdr: bool = False,
    tonemap: bool = True,
    window: Window = WHOLE_FILE,
    settings: EncodeSettings = DEFAULT_SETTINGS,
) -> list[str]:
    """Transcode to H.264 at the source's own resolution."""
    vf = ["-vf", f"{TONEMAP_CHAIN},format=yuv420p"] if hdr and tonemap else []
    return [
        *base_args(),
        *input_args(src, window),
        "-map",
        "0:v:0",
        *vf,
        *h264_video_args(settings),
        *audio_args(settings),
        "-sn",
        "-dn",
        *hls_output_args(out_dir),
    ]


def escape_filter_path(path: str | Path) -> str:
    """Return a path usable inside an ffmpeg filter option.

    Only plain characters are accepted, because ffmpeg's two levels of filter escaping are
    easy to get wrong. Callers link the source to a safe temporary name first.
    """
    p = str(path)
    if not _SAFE_FILTER_PATH.match(p):
        raise ValueError(f"path is not safe for an ffmpeg filter: {p!r}")
    return p


def burn_filter(
    *, sub_number: int, image: bool, filter_src: str | Path, hdr: bool, tonemap: bool
) -> str:
    """Filter graph that burns subtitle track `sub_number` (1-based) into video 1.

    Timestamps are kept from the source (-copyts) so the subtitles line up after a seek,
    then reset to zero for the output.
    """
    si = sub_number - 1
    pre = f"{TONEMAP_CHAIN}," if hdr and tonemap else ""
    tail = "setpts=PTS-STARTPTS,format=yuv420p[v]"
    if image:
        base = f"[0:v:0]{pre}null[base];" if pre else ""
        src_label = "[base]" if pre else "[0:v:0]"
        return f"{base}{src_label}[0:s:{si}]overlay=eof_action=pass,{tail}"
    path = escape_filter_path(filter_src)
    return f"[0:v:0]{pre}subtitles=filename={path}:si={si},{tail}"


def burn_hls(
    src: str,
    out_dir: str | Path,
    *,
    sub_number: int,
    image: bool,
    filter_src: str | Path,
    hdr: bool = False,
    tonemap: bool = True,
    window: Window = WHOLE_FILE,
    settings: EncodeSettings = DEFAULT_SETTINGS,
    loglevel: str = "error",
) -> list[str]:
    """Burn one subtitle track into an H.264 transcode. Video only, audio is left out."""
    graph = burn_filter(
        sub_number=sub_number, image=image, filter_src=filter_src, hdr=hdr, tonemap=tonemap
    )
    return [
        *base_args(loglevel),
        *input_args(src, window, copyts=True),
        "-filter_complex",
        graph,
        "-map",
        "[v]",
        *h264_video_args(settings),
        "-an",
        "-sn",
        "-dn",
        *hls_output_args(out_dir),
    ]


def parse_ffmpeg_list(text: str) -> set[str]:
    """Names from `ffmpeg -encoders` or `ffmpeg -filters` output."""
    names: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        # Lines look like " V....D libx264  description" or " TS overlay  VV->V  description".
        if len(parts) >= 2 and re.fullmatch(r"[A-Z.]{2,6}", parts[0]):
            names.add(parts[1])
    return names
