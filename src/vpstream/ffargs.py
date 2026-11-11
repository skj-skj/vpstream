"""Pure builders for ffmpeg command lines.

Nothing here runs a program, so every builder is unit tested. `vpstream prepare` and
`vpstream bench` use the same builders, which keeps the benchmark's numbers honest.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

HLS_SEGMENT_SECONDS = 6
# Codecs a browser may play without a transcode (subject to `vpstream caps` on the TV).
BROWSER_COPY_CODECS = frozenset({"h264", "hevc", "av1"})

# ffmpeg drops subtitle packets from before an input's -ss point, so an image subtitle that
# is already on screen there would be lost. Image subtitles are therefore read from a second
# input that starts this much earlier; a cue shown for longer than this is still cut.
SUBTITLE_PREROLL_SECONDS = 30.0

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


def aac_args(bitrate: str) -> list[str]:
    return ["-c:a", "aac", "-b:a", bitrate]


def subtitle_input_args(src: str, window: Window) -> list[str]:
    """A second input of `src` for image subtitles, starting SUBTITLE_PREROLL_SECONDS early.

    No -fix_sub_duration: it holds each cue back until the next subtitle event, which drops
    the first cue this input reads (often the one already on screen) and, for VobSub, the
    last cue before the end of the window. No -t either: this input is read far ahead of
    the video, and once it ends the overlay (eof_action=pass) drops cues it has not drawn
    yet. ffmpeg stops reading it when the output is finished.
    """
    return input_args(src, Window(max(0.0, window.start - SUBTITLE_PREROLL_SECONDS)))


def hls_output_args(
    out_dir: str | Path,
    *,
    fmp4: bool = True,
    seconds: int = HLS_SEGMENT_SECONDS,
    segment: str = "seg_%05d",
    playlist: str = "index.m3u8",
    extra: Sequence[str] = (),
) -> list[str]:
    """VOD HLS output into `out_dir`. `extra` goes before the file names, for variant maps."""
    out = Path(out_dir)
    ext = "m4s" if fmp4 else "ts"
    return [
        "-f",
        "hls",
        "-hls_time",
        str(seconds),
        "-hls_playlist_type",
        "vod",
        "-hls_flags",
        "independent_segments",
        "-hls_segment_type",
        "fmp4" if fmp4 else "mpegts",
        *extra,
        "-hls_segment_filename",
        str(out / f"{segment}.{ext}"),
        str(out / playlist),
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


# The files of a prepared HLS version: one master playlist, one playlist for the video and
# one per audio track ("a1", "a2" ... in file order), each with its own segments.
MASTER = "master.m3u8"
VIDEO = "video"
_LANGUAGE = re.compile(r"[a-z]{2,3}")


def audio_name(number: int) -> str:
    """The rendition name of audio track `number` (1-based)."""
    return f"a{number}"


def hls_language(language: str) -> str:
    """A track's language as it can go into -var_stream_map, which splits on spaces and
    commas: a 2 or 3 letter code, else "und"."""
    lang = language.strip().lower()
    return lang if _LANGUAGE.fullmatch(lang) else "und"


def variant_map(languages: Sequence[str], default: int = 0) -> str:
    """-var_stream_map for one video and these audio tracks as alternates of it. `default`
    is the 0-based audio track players start with."""
    if not languages:
        return f"v:0,name:{VIDEO}"
    group = "agroup:aud"
    parts = [f"v:0,{group},name:{VIDEO}"]
    for i, lang in enumerate(languages):
        flag = ",default:yes" if i == default else ""
        parts.append(f"a:{i},{group},language:{hls_language(lang)},name:{audio_name(i + 1)}{flag}")
    return " ".join(parts)


def rendition_output_args(out_dir: str | Path, stream_map: str) -> list[str]:
    """HLS output of named renditions (see MASTER) into `out_dir`.

    ffmpeg puts the rendition's name in place of %v in the segment and playlist names
    always, but in the init segment's name only when there are two renditions or more. A
    video alone (a burned-in version) gets the name written out, else its playlist names
    a file "%v_init.mp4", which works from a disk and not over HTTP.
    """
    variants = stream_map.split()
    init = "%v_init.mp4" if len(variants) > 1 else f"{VIDEO}_init.mp4"
    return hls_output_args(
        out_dir,
        segment="%v_%05d",
        playlist="%v.m3u8",
        extra=(
            "-hls_fmp4_init_filename",
            init,
            "-master_pl_name",
            MASTER,
            "-var_stream_map",
            stream_map,
        ),
    )


def copy_ok(codec: str, pix_fmt: str, bit_depth: int) -> bool:
    """Whether a browser can play this video as it is: H.264 in 8-bit 4:2:0 only (browsers
    have no 10-bit or 4:2:2 H.264 decoder), HEVC and AV1 in 4:2:0 at 8 or 10 bits."""
    if codec not in BROWSER_COPY_CODECS or "420" not in pix_fmt:
        return False
    return bit_depth <= (8 if codec == "h264" else 10)


def base_hls(
    src: str,
    out_dir: str | Path,
    *,
    copy: bool,
    video_codec: str,
    audio: Sequence[str] = (),
    default_audio: int = 0,
    hdr: bool = False,
    tonemap: bool = True,
    window: Window = WHOLE_FILE,
    settings: EncodeSettings = DEFAULT_SETTINGS,
) -> list[str]:
    """The base version: the video copied (`copy`) or transcoded to H.264 at the source's own
    resolution, and every audio track as an AAC alternate. `audio` holds the language of
    each audio track, in file order."""
    if copy:
        tag = ["-tag:v", "hvc1"] if video_codec == "hevc" else []
        video = ["-c:v", "copy", *tag]
    else:
        vf = ["-vf", f"{TONEMAP_CHAIN},format=yuv420p"] if hdr and tonemap else []
        video = [*vf, *h264_video_args(settings)]
    audio_maps = [arg for i in range(len(audio)) for arg in ("-map", f"0:a:{i}")]
    sound = [*aac_args(settings.audio_bitrate), "-ac", str(settings.audio_channels)]
    return [
        *base_args(),
        *input_args(src, window),
        "-map",
        "0:V:0",  # V: not cover art, which ffprobe lists as video too
        *audio_maps,
        *video,
        *(sound if audio else []),
        "-sn",
        "-dn",
        *rendition_output_args(out_dir, variant_map(audio, default_audio)),
    ]


def with_progress(args: Sequence[str]) -> list[str]:
    """An ffmpeg command that reports how far it got on standard output (out_time_us=...)."""
    return [args[0], "-progress", "pipe:1", "-nostats", *args[1:]]


def progress_seconds(line: str) -> float | None:
    """The media time reached, from one line of -progress output, or None."""
    key, _, value = line.partition("=")
    if key != "out_time_us":
        return None
    try:
        return max(0.0, int(value) / 1_000_000)
    except ValueError:  # "N/A" before the first frame
        return None


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
    *,
    sub_number: int,
    image: bool,
    filter_src: str | Path,
    hdr: bool,
    tonemap: bool,
    sub_input: int = 0,
) -> str:
    """Filter graph that burns subtitle track `sub_number` (1-based) into video 1.

    Image subtitles come from input `sub_input`. Timestamps are kept from the source
    (-copyts) so the subtitles line up after a seek, then reset to zero for the output.
    """
    si = sub_number - 1
    pre = f"{TONEMAP_CHAIN}," if hdr and tonemap else ""
    tail = "setpts=PTS-STARTPTS,format=yuv420p[v]"
    if image:
        # Image subtitles are drawn for their own canvas (often 1080p over a 4K video), so
        # they are scaled to the video's size before the overlay.
        return (
            f"[0:V:0]{pre}null[base];"
            f"[{sub_input}:s:{si}][base]scale2ref=w=rw:h=rh[sub][ref];"
            f"[ref][sub]overlay=eof_action=pass,{tail}"
        )
    path = escape_filter_path(filter_src)
    return f"[0:V:0]{pre}subtitles=filename={path}:si={si},{tail}"


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
    """Burn one subtitle track into an H.264 transcode. Video only: a player takes the audio
    from the base version (see `prepared.burn_master`)."""
    inputs = input_args(src, window, copyts=True)
    sub_input = 0
    if image and window.start > 0:
        inputs += subtitle_input_args(src, window)
        sub_input = 1
    graph = burn_filter(
        sub_number=sub_number,
        image=image,
        filter_src=filter_src,
        hdr=hdr,
        tonemap=tonemap,
        sub_input=sub_input,
    )
    return [
        *base_args(loglevel),
        *inputs,
        "-filter_complex",
        graph,
        "-map",
        "[v]",
        *h264_video_args(settings),
        "-an",
        "-sn",
        "-dn",
        *rendition_output_args(out_dir, variant_map(())),
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
