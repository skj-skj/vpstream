"""Turn a plain text subtitle track into a WebVTT file, the format browsers draw."""

from __future__ import annotations

from pathlib import Path

from vpstream.ffargs import base_args
from vpstream.probe import MKVEXTRACT, SubtitleTrack
from vpstream.tools import Runner, require, tail

EXTRACT_TIMEOUT = 1800  # the whole file is read; a long film on a slow disk takes a while


class SubtitleError(RuntimeError):
    pass


def webvtt_args(src: str, track: SubtitleTrack, out: Path) -> list[str]:
    if track.reader == MKVEXTRACT:
        # mkvextract numbers tracks like ffprobe numbers streams: in file order, from 0.
        return ["mkvextract", "-q", src, "tracks", f"{track.index}:{out}"]
    return [*base_args(), "-i", src, "-map", f"0:{track.index}", "-c:s", "webvtt", str(out)]


def extract_webvtt(runner: Runner, src: str, track: SubtitleTrack, out: Path) -> None:
    """Write subtitle `track` of `src` to `out` as WebVTT.

    Raises ValueError for a track that cannot be soft, ToolMissing when the reader is not
    installed, and SubtitleError when extraction fails.
    """
    if track.treatment != "soft":
        raise ValueError(f"subtitle {track.number} ({track.codec or 'unknown'}) cannot be soft")
    mkv = track.reader == MKVEXTRACT
    require(runner, "mkvextract" if mkv else "ffmpeg", f"subtitle {track.number} ({track.codec})")
    res = runner.run(webvtt_args(src, track, out), timeout=EXTRACT_TIMEOUT)
    # mkvextract exits 1 when it only warned, and the file is then complete.
    ok = res.returncode == 0 or (mkv and res.returncode == 1)
    if not ok or not out.is_file():
        raise SubtitleError(f"subtitle {track.number}: {tail(res, default='no file written')}")
