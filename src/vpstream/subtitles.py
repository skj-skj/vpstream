"""Turn a plain text subtitle track into a WebVTT file, the format browsers draw."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vpstream.ffargs import base_args
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
    if track.treatment != "soft":
        raise ValueError(f"subtitle {track.number} ({track.codec or 'unknown'}) cannot be soft")
    needed = f"subtitle {track.number} ({track.codec})"
    if track.reader == MKVEXTRACT:
        require(runner, "mkvextract", needed)
        require(runner, "mkvmerge", needed)
        track_id = mkvextract_track_id(_identify_mkv(runner, src, track), track)
        args = mkvextract_args(src, track_id, out)
        # mkvtoolnix prints its errors on stdout, and exits 1 when it only warned (the file
        # is then complete).
        ok_codes: tuple[int, ...] = (0, 1)
        errors_on_stdout = True
    else:
        require(runner, "ffmpeg", needed)
        args = ffmpeg_webvtt_args(src, track, out)
        ok_codes, errors_on_stdout = (0,), False
    out.unlink(missing_ok=True)  # a file left by an earlier run must not pass for a result
    res = runner.run(args, timeout=EXTRACT_TIMEOUT)
    if res.returncode not in ok_codes or not out.is_file():
        why = tail(res, default="no file written", stdout=errors_on_stdout)
        raise SubtitleError(f"subtitle {track.number}: {why}")
