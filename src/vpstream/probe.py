"""Read a media file's streams with ffprobe and describe them."""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vpstream.tools import Runner, require

# Subtitle codecs that can be converted to WebVTT and drawn by the player.
SOFT_TEXT_CODECS = frozenset({"subrip", "srt", "mov_text", "webvtt", "text"})
# Text formats whose positioning, fonts and effects would be lost as WebVTT.
STYLED_TEXT_CODECS = frozenset({"ass", "ssa"})
# Bitmap formats that can only be burned in.
IMAGE_CODECS = frozenset({"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"})

HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67"})


@dataclass(frozen=True)
class VideoTrack:
    number: int  # 1-based among video tracks
    index: int  # ffprobe stream index
    codec: str
    profile: str
    width: int
    height: int
    fps: float
    pix_fmt: str
    bit_depth: int
    hdr: bool
    bitrate: int | None


@dataclass(frozen=True)
class AudioTrack:
    number: int
    index: int
    codec: str
    language: str
    title: str
    channels: int
    default: bool


@dataclass(frozen=True)
class SubtitleTrack:
    number: int
    index: int
    codec: str
    language: str
    title: str
    default: bool

    @property
    def kind(self) -> str:
        """'text' or 'image'."""
        return "image" if self.codec in IMAGE_CODECS else "text"

    @property
    def treatment(self) -> str:
        """How `prepare` handles this track: 'soft', 'burned in' or 'unsupported'."""
        if self.codec in SOFT_TEXT_CODECS:
            return "soft"
        if self.codec in STYLED_TEXT_CODECS or self.codec in IMAGE_CODECS:
            return "burned in"
        return "unsupported"


@dataclass(frozen=True)
class MediaInfo:
    path: str
    format_name: str
    duration: float
    size: int
    bitrate: int | None
    video: list[VideoTrack] = field(default_factory=list)
    audio: list[AudioTrack] = field(default_factory=list)
    subtitles: list[SubtitleTrack] = field(default_factory=list)
    attachments: int = 0  # embedded fonts and other attachments


def ffprobe_args(path: str) -> list[str]:
    return [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        path,
    ]


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _rate(value: Any) -> float:
    if not isinstance(value, str) or "/" not in value:
        return 0.0
    num, _, den = value.partition("/")
    try:
        d = float(den)
        return round(float(num) / d, 3) if d else 0.0
    except ValueError:
        return 0.0


def _tags(stream: dict[str, Any]) -> dict[str, str]:
    tags = stream.get("tags") or {}
    return {str(k).lower(): str(v) for k, v in tags.items()}


def _stream_bitrate(stream: dict[str, Any]) -> int | None:
    rate = _int(stream.get("bit_rate"))
    if rate:
        return rate
    tags = _tags(stream)
    for key in ("bps", "bps-eng"):
        rate = _int(tags.get(key))
        if rate:
            return rate
    return None


def _bit_depth(stream: dict[str, Any]) -> int:
    raw = _int(stream.get("bits_per_raw_sample"))
    if raw:
        return raw
    pix = str(stream.get("pix_fmt", ""))
    for depth in (16, 14, 12, 10):
        if f"p{depth}" in pix:
            return depth
    return 8


def _is_hdr(stream: dict[str, Any]) -> bool:
    if stream.get("color_transfer") in HDR_TRANSFERS:
        return True
    for side in stream.get("side_data_list") or []:
        if "DOVI" in str(side.get("side_data_type", "")):
            return True
    return False


def parse_probe(data: dict[str, Any], path: str = "") -> MediaInfo:
    """Turn ffprobe's JSON into a MediaInfo. Pure, so it is unit tested."""
    fmt = data.get("format") or {}
    video: list[VideoTrack] = []
    audio: list[AudioTrack] = []
    subs: list[SubtitleTrack] = []
    attachments = 0
    for s in data.get("streams") or []:
        kind = s.get("codec_type")
        tags = _tags(s)
        disposition = s.get("disposition") or {}
        lang = tags.get("language", "und")
        title = tags.get("title", "")
        default = bool(disposition.get("default"))
        if kind == "video":
            if disposition.get("attached_pic"):
                attachments += 1
                continue
            video.append(
                VideoTrack(
                    number=len(video) + 1,
                    index=int(s.get("index", 0)),
                    codec=str(s.get("codec_name", "")),
                    profile=str(s.get("profile", "")),
                    width=int(s.get("width", 0)),
                    height=int(s.get("height", 0)),
                    fps=_rate(s.get("avg_frame_rate")) or _rate(s.get("r_frame_rate")),
                    pix_fmt=str(s.get("pix_fmt", "")),
                    bit_depth=_bit_depth(s),
                    hdr=_is_hdr(s),
                    bitrate=_stream_bitrate(s),
                )
            )
        elif kind == "audio":
            audio.append(
                AudioTrack(
                    number=len(audio) + 1,
                    index=int(s.get("index", 0)),
                    codec=str(s.get("codec_name", "")),
                    language=lang,
                    title=title,
                    channels=int(s.get("channels", 0)),
                    default=default,
                )
            )
        elif kind == "subtitle":
            subs.append(
                SubtitleTrack(
                    number=len(subs) + 1,
                    index=int(s.get("index", 0)),
                    codec=str(s.get("codec_name", "")),
                    language=lang,
                    title=title,
                    default=default,
                )
            )
        elif kind == "attachment":
            attachments += 1
    duration = 0.0
    with contextlib.suppress(TypeError, ValueError):
        duration = float(fmt.get("duration", 0.0))
    return MediaInfo(
        path=path,
        format_name=str(fmt.get("format_name", "")),
        duration=duration,
        size=_int(fmt.get("size")) or 0,
        bitrate=_int(fmt.get("bit_rate")),
        video=video,
        audio=audio,
        subtitles=subs,
        attachments=attachments,
    )


class ProbeError(RuntimeError):
    pass


def probe(runner: Runner, path: str | Path) -> MediaInfo:
    require(runner, "ffprobe")
    p = str(path)
    res = runner.run(ffprobe_args(p), timeout=120)
    if res.returncode != 0:
        raise ProbeError(f"ffprobe failed for {p}: {res.stderr.strip()}")
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe returned invalid JSON for {p}") from exc
    return parse_probe(data, p)
