"""Turn a plain text subtitle track into a WebVTT file, the format browsers draw."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from vpstream import probe
from vpstream.ffargs import base_args, escape_filter_path
from vpstream.probe import MKVEXTRACT, SubtitleTrack
from vpstream.tools import Runner, require, tail

EXTRACT_TIMEOUT = 1800  # the whole file is read; a long film on a slow disk takes a while
IDENTIFY_TIMEOUT = 120
MKV_WEBVTT = "S_TEXT/WEBVTT"


class SubtitleError(RuntimeError):
    pass


def ffmpeg_webvtt_args(src: str, track: SubtitleTrack, out: Path) -> list[str]:
    return [*base_args(), "-i", src, "-map", f"0:{track.index}", "-c:s", "webvtt", str(out)]


def mkvextract_args(src: str, track_id: int, out: Path) -> list[str]:
    return ["mkvextract", "-q", src, "tracks", f"{track_id}:{out}"]


def mkvextract_track_id(identify: dict[str, Any], track: SubtitleTrack) -> int:
    """mkvextract's ID for `track`, from `mkvmerge -J` output. Pure, so it is unit tested.

    It is not ffprobe's stream index: ffmpeg skips Matroska tracks it cannot use (no codec
    ID, or a type such as buttons) and mkvextract still counts them. Both list the same
    subtitle tracks in file order, so the track is found by its place among them, then
    checked by codec and language.
    """
    subs = [t for t in identify.get("tracks") or [] if t.get("type") == "subtitles"]
    if not 0 < track.number <= len(subs):
        raise SubtitleError(
            f"subtitle {track.number}: mkvmerge lists {len(subs)} subtitle tracks in this file"
        )
    found = subs[track.number - 1]
    props = found.get("properties") or {}
    lang = str(props.get("language") or "und")
    same_lang = "und" in (lang, track.language) or lang == track.language
    if props.get("codec_id") != MKV_WEBVTT or not same_lang:
        raise SubtitleError(
            f"subtitle {track.number}: mkvmerge and ffprobe do not agree on this track "
            f"(mkvmerge track {found.get('id')}: {props.get('codec_id')}, {lang})"
        )
    return int(found["id"])


def _identify_mkv(runner: Runner, src: str, track: SubtitleTrack) -> dict[str, Any]:
    res = runner.run(["mkvmerge", "-J", src], timeout=IDENTIFY_TIMEOUT)
    try:
        identify = json.loads(res.stdout)
    except json.JSONDecodeError:
        identify = None
    if res.returncode != 0 or not isinstance(identify, dict):
        errors = identify.get("errors") if isinstance(identify, dict) else None
        why = " ".join(errors).strip() if errors else tail(res, stdout=True)
        raise SubtitleError(f"subtitle {track.number}: mkvmerge cannot read the file: {why}")
    return identify


def extract_webvtt(runner: Runner, src: str, track: SubtitleTrack, out: Path) -> None:
    """Write subtitle `track` of `src` to `out` as WebVTT.

    Raises ValueError for a track that cannot be soft, ToolMissing when the reader is not
    installed, and SubtitleError when extraction fails.
    """
    if track.treatment != probe.SOFT:
        raise ValueError(f"subtitle {track.number} ({track.codec or 'unknown'}) cannot be soft")
    needed = f"subtitle {track.number} ({track.codec})"
    # Written under another name and renamed when complete, so `out` is never half a file.
    # The suffix is kept: ffmpeg picks the format from it.
    part = out.with_name(f"{out.stem}.part{out.suffix}")
    if track.reader == MKVEXTRACT:
        require(runner, "mkvextract", needed)
        require(runner, "mkvmerge", needed)
        track_id = mkvextract_track_id(_identify_mkv(runner, src, track), track)
        args = mkvextract_args(src, track_id, part)
        # mkvtoolnix prints its errors on stdout, and exits 1 when it only warned (the file
        # is then complete).
        ok_codes: tuple[int, ...] = (0, 1)
        errors_on_stdout = True
    else:
        require(runner, "ffmpeg", needed)
        args = ffmpeg_webvtt_args(src, track, part)
        ok_codes, errors_on_stdout = (0,), False
    out.unlink(missing_ok=True)  # a file left by an earlier run must not pass for a result
    part.unlink(missing_ok=True)
    res = runner.run(args, timeout=EXTRACT_TIMEOUT)
    if res.returncode not in ok_codes or not part.is_file():
        part.unlink(missing_ok=True)
        why = tail(res, default="no file written", stdout=errors_on_stdout)
        raise SubtitleError(f"subtitle {track.number}: {why}")
    os.replace(part, out)


def filter_source(work: Path, path: Path) -> Path:
    """Link the source to a plain name in `work`, because ffmpeg's subtitles filter reads
    the file by a name that goes into the filter graph (see `ffargs.escape_filter_path`).

    Raises ValueError or OSError when `work` cannot be used for this.
    """
    link = work / ("source" + path.suffix.lower())
    if not re.fullmatch(r"\.[a-z0-9]+", link.suffix):
        link = work / "source"
    escape_filter_path(link)
    link.unlink(missing_ok=True)
    os.symlink(path.resolve(), link)
    return link


_CUE_TIME = re.compile(r"(?:(\d+):)?([0-5]\d):([0-5]\d)\.(\d{3})")


def _vtt_time(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def shift_webvtt(text: str, seconds: float) -> str:
    """WebVTT with every cue `seconds` earlier (a cue cannot start before 0). Only cue timing
    lines change. Pure, so it is unit tested."""

    def shift(m: re.Match[str]) -> str:
        h, mm, ss, ms = (int(g or 0) for g in m.groups())
        return _vtt_time(h * 3600 + mm * 60 + ss + ms / 1000 - seconds)

    lines = text.split("\n")
    for i, line in enumerate(lines):
        start, arrow, rest = line.partition("-->")
        if arrow and _CUE_TIME.fullmatch(start.strip()):
            end, space, settings = rest.strip().partition(" ")
            if _CUE_TIME.fullmatch(end):
                lines[i] = (
                    f"{_CUE_TIME.sub(shift, start.strip())} --> {_CUE_TIME.sub(shift, end)}"
                    f"{space}{settings}"
                )
    return "\n".join(lines)
