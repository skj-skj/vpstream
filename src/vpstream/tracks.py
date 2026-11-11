"""Choosing tracks by number or language, naming them, and the `vpstream track-info` table.

Numbers restart at 1 for each kind of track, in the order ffprobe lists them, so
`--sub 2` is the second subtitle track whatever its stream index is.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from vpstream import ffargs, probe, text
from vpstream.probe import AudioTrack, MediaInfo, SubtitleTrack

OFF = "off"
NONE = "none"

# Two-letter codes, and the other three-letter spelling of a few, mapped to the three-letter
# code Matroska files use, so `--sub en` and `--sub fra` find "eng" and "fre" tracks.
_SAME = {
    "en": "eng", "ja": "jpn", "hi": "hin", "es": "spa", "fr": "fre", "fra": "fre",
    "de": "ger", "deu": "ger", "it": "ita", "pt": "por", "ru": "rus", "zh": "chi",
    "zho": "chi", "ko": "kor", "ar": "ara", "nl": "dut", "nld": "dut", "sv": "swe",
    "pl": "pol", "tr": "tur", "ta": "tam", "te": "tel", "bn": "ben", "mr": "mar",
    "ml": "mal", "kn": "kan", "ur": "urd", "pa": "pan", "gu": "guj", "th": "tha",
    "vi": "vie", "id": "ind", "he": "heb", "el": "gre", "ell": "gre", "cs": "cze",
    "ces": "cze", "da": "dan", "fi": "fin", "no": "nor", "hu": "hun", "uk": "ukr",
}  # fmt: skip
_NAMES = {
    "eng": "English", "jpn": "Japanese", "hin": "Hindi", "spa": "Spanish", "fre": "French",
    "ger": "German", "ita": "Italian", "por": "Portuguese", "rus": "Russian", "chi": "Chinese",
    "kor": "Korean", "ara": "Arabic", "dut": "Dutch", "swe": "Swedish", "pol": "Polish",
    "tur": "Turkish", "tam": "Tamil", "tel": "Telugu", "ben": "Bengali", "mar": "Marathi",
    "mal": "Malayalam", "kan": "Kannada", "urd": "Urdu", "pan": "Punjabi", "guj": "Gujarati",
    "tha": "Thai", "vie": "Vietnamese", "ind": "Indonesian", "heb": "Hebrew", "gre": "Greek",
    "cze": "Czech", "dan": "Danish", "fin": "Finnish", "nor": "Norwegian", "hun": "Hungarian",
    "ukr": "Ukrainian",
}  # fmt: skip
_NUMBER = re.compile(r"[1-9][0-9]{0,3}")
_LANGUAGE = re.compile(r"[a-z]{2,3}")


class SelectionError(ValueError):
    """A track choice that cannot be read; the message says how to write it."""


def language_code(code: str) -> str:
    """One spelling for a language code: lower case, three letters where it is known."""
    low = code.strip().lower()
    return _SAME.get(low, low)


def language_name(code: str) -> str:
    """ "English" for eng or en; the code itself for one not in the table."""
    canon = language_code(code)
    return _NAMES.get(canon, "" if canon in ("", "und") else canon)


def label(track: AudioTrack | SubtitleTrack) -> str:
    """What a player shows for a track: its title, else its language, else its number."""
    return track.title.strip() or language_name(track.language) or f"Track {track.number}"


Choice = int | str  # a track number, or a language code


def parse_choices(spec: str) -> list[Choice]:
    """`1,3,eng` as [1, 3, "eng"]. `none` is the empty list. Raises SelectionError."""
    tokens = [t.strip().lower() for t in spec.split(",")]
    if tokens == [NONE]:
        return []
    choices: list[Choice] = []
    for token in tokens:
        if _NUMBER.fullmatch(token):
            choices.append(int(token))
        elif _LANGUAGE.fullmatch(token) and token != NONE:
            choices.append(language_code(token))
        else:
            raise SelectionError(
                f"not a track number or a language code: {token!r}. Use numbers from "
                "`vpstream track-info` or codes such as eng, separated by commas, or none"
            )
    return choices


def parse_choice(spec: str) -> Choice | None:
    """One track number or language code; None for `off` (or `none`)."""
    if spec.strip().lower() in (OFF, NONE):
        return None
    choices = parse_choices(spec)
    if len(choices) != 1:
        raise SelectionError(f"give one track number or language code, or off: {spec!r}")
    return choices[0]


class Numbered(Protocol):
    """A track as far as choosing it goes."""

    @property
    def number(self) -> int: ...

    @property
    def language(self) -> str: ...


def matching[Track: Numbered](tracks: Sequence[Track], choice: Choice) -> list[Track]:
    """The tracks a choice names: the one with that number, or all in that language."""
    if isinstance(choice, int):
        return [t for t in tracks if t.number == choice]
    return [t for t in tracks if language_code(t.language) == choice]


def select_subtitles(
    tracks: Sequence[SubtitleTrack], spec: str | None
) -> tuple[list[SubtitleTrack], list[str]]:
    """The subtitle tracks to prepare, in file order, and a note for each choice that names
    nothing in this file or a track that cannot be prepared.

    No `spec` means every plain text track (soft); image, ASS and SSA tracks are then left
    out, because each needs a burned-in version, which is a full encode.
    """
    if spec is None:
        return [t for t in tracks if t.treatment == probe.SOFT], []
    chosen: dict[int, SubtitleTrack] = {}
    notes: list[str] = []
    for choice in parse_choices(spec):
        found = matching(tracks, choice)
        if not found:
            what = f"subtitle {choice}" if isinstance(choice, int) else f"{choice} subtitles"
            notes.append(f"{what}: not in this file")
        for track in found:
            if track.treatment == probe.UNSUPPORTED:
                notes.append(f"subtitle {track.number} ({track.codec or 'unknown'}): unsupported")
            else:
                chosen[track.number] = track
    return [chosen[n] for n in sorted(chosen)], notes


def pick[Track: Numbered](tracks: Sequence[Track], choice: Choice | None) -> Track | None:
    """The first track a choice names, or None."""
    if choice is None:
        return None
    found = matching(tracks, choice)
    return found[0] if found else None


class Defaulted(Protocol):
    @property
    def default(self) -> bool: ...


def default_audio(tracks: Sequence[Defaulted]) -> int:
    """The 0-based audio track a player starts with: the one marked default, else the first."""
    return next((i for i, t in enumerate(tracks) if t.default), 0)


# --- vpstream track-info


def _channels(count: int) -> str:
    return {1: "mono", 2: "2.0", 6: "5.1", 8: "7.1"}.get(count, f"{count} ch")


def browser_video(info: MediaInfo) -> str:
    """How browser mode prepares the video: copied as it is, or transcoded and why."""
    if not info.video:
        return "no video track: cannot be prepared"
    v = info.video[0]
    if ffargs.copy_ok(v.codec, v.pix_fmt, v.bit_depth):
        return "copied as it is (minutes)"
    return f"transcoded to H.264, a full encode ({v.codec} {v.bit_depth}-bit {v.pix_fmt})"


def format_track_info(info: MediaInfo) -> str:
    """Every track of a file with its number, as `--sub` and `--audio` count them."""
    fmt = info.format_name.split(",")[0]
    head = f"{Path(info.path).name} · {text.duration(info.duration)} · {fmt}"
    lines = [f"{head} · {text.size(info.size)}" if info.size else head]
    if info.video:
        lines += ["", "VIDEO"]
        rows = []
        for v in info.video:
            rate = f"{v.bitrate / 1e6:.1f} Mbit/s" if v.bitrate else ""
            hdr = "HDR" if v.hdr else ""
            rows.append([str(v.number), v.codec, v.profile, f"{v.width}x{v.height}",
                         f"{v.fps:g} fps", f"{v.bit_depth}-bit", hdr, rate])  # fmt: skip
        lines += text.table(indent="  ", rows=rows)
    lines.append(f"  browser mode: {browser_video(info)}")
    if info.audio:
        lines += ["", "AUDIO"]
        lines += text.table(
            indent="  ",
            rows=[
                [
                    str(a.number),
                    a.language,
                    label(a),
                    a.codec,
                    _channels(a.channels),
                    "default" if a.default else "",
                ]
                for a in info.audio
            ],
        )
    if info.subtitles:
        lines += ["", "SUBTITLES"]
        lines += text.table(
            indent="  ",
            rows=[
                [str(s.number), s.language, label(s), s.codec or "unknown", s.kind, s.treatment]
                for s in info.subtitles
            ],
        )
    return "\n".join(lines)
