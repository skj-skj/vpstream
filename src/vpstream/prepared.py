"""The prepared versions of a video, kept in the cache until they are removed.

Each source file has one folder, named by `files.source_key` (its path, size and change
time), so a file that is changed gets a new folder and the old one shows as stale:

    <cache>/prepared/<key>/
        meta.json         what the source holds (tracks and their names), for the player
        base/             master.m3u8, video.m3u8 and a1.m3u8, a2.m3u8 ... (see ffargs.MASTER)
        subs/<n>.vtt      subtitle track n as WebVTT, drawn by the player
        burn/<n>/         the video with subtitle track n burned in; its master.m3u8 takes
                          the audio from ../../base

A version is written under a `.part` name and renamed when it is complete, so a folder
without that suffix is always whole.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import secrets
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from vpstream import ffargs, media, tracks
from vpstream.files import dir_size, source_key, write_atomic
from vpstream.probe import MediaInfo

FOLDER = "prepared"  # under the cache folder
META = "meta.json"
BASE = "base"
SUBS = "subs"
BURN = "burn"
PART = ".part"
WORK = ".work"  # a folder of links the subtitles filter reads; never served
COPY = "copy"
TRANSCODE = "transcode"
# The files a viewer may ask for, relative to a video's folder. Nothing else is served.
_SERVED = re.compile(
    r"(?:base|burn/[1-9][0-9]{0,3})/[A-Za-z0-9_]{1,40}\.(?:m3u8|m4s|mp4)|subs/[1-9][0-9]{0,3}\.vtt"
)
_KEY = re.compile(r"[0-9a-f]{32}")


def root(cache_dir: Path) -> Path:
    return cache_dir / FOLDER


def entry_dir(cache_dir: Path, source: str) -> Path:
    """The folder of `source`'s versions. Raises OSError when the source cannot be seen."""
    return root(cache_dir) / source_key(source)


def soft_file(entry: Path, number: int) -> Path:
    return entry / SUBS / f"{number}.vtt"


def burn_dir(entry: Path, number: int) -> Path:
    return entry / BURN / str(number)


def part_of(path: Path) -> Path:
    return path.with_name(path.name + PART)


def master(folder: Path) -> Path:
    return folder / ffargs.MASTER


def base_ready(entry: Path) -> bool:
    return master(entry / BASE).is_file()


# --- what a source holds


def meta_of(info: MediaInfo, base: str) -> dict[str, Any]:
    """What the player needs to know about a source: the names of its tracks."""
    st = os.stat(info.path)
    return {
        "source": info.path,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "duration": info.duration,
        "base": base,
        "audio": [
            {
                "number": a.number,
                "language": a.language,
                "label": tracks.label(a),
                "default": a.default,
            }
            for a in info.audio
        ],
        "subtitles": [
            {
                "number": s.number,
                "language": s.language,
                "label": tracks.label(s),
                "treatment": s.treatment,
            }
            for s in info.subtitles
        ],
    }


def write_meta(entry: Path, meta: dict[str, Any]) -> None:
    entry.mkdir(mode=0o750, parents=True, exist_ok=True)
    write_atomic(entry / META, json.dumps(meta, indent=1) + "\n")


def read_meta(entry: Path) -> dict[str, Any] | None:
    """The recorded meta.json, or None when there is none or it is not usable."""
    try:
        data = json.loads((entry / META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("source"), str):
        return None
    for key in ("audio", "subtitles"):
        listed = data.get(key)
        data[key] = [t for t in listed if isinstance(t, dict)] if isinstance(listed, list) else []
    return data


@dataclass(frozen=True)
class Track:
    """An audio or subtitle track as meta.json names it."""

    number: int
    language: str
    label: str
    default: bool = False


def meta_tracks(entry: Path, kind: str) -> list[Track]:
    """The "audio" or "subtitles" tracks meta.json lists, in track order. Entries that do
    not fit are left out."""
    found = []
    for raw in (read_meta(entry) or {}).get(kind) or []:
        number = raw.get("number")
        if isinstance(number, int) and not isinstance(number, bool) and number > 0:
            language = str(raw.get("language") or "und")
            label = str(raw.get("label") or "") or f"Track {number}"
            found.append(Track(number, language, label, raw.get("default") is True))
    return sorted(found, key=lambda t: t.number)


@dataclass
class Versions:
    """Which versions of a source are complete."""

    base: bool = False
    soft: list[int] = field(default_factory=list)
    burn: list[int] = field(default_factory=list)


def _numbers(folder: Path, pattern: str) -> list[int]:
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    found = (re.fullmatch(pattern, name) for name in names)
    return sorted(int(m[1]) for m in found if m)


def versions(entry: Path) -> Versions:
    return Versions(
        base=base_ready(entry),
        soft=_numbers(entry / SUBS, r"([1-9][0-9]{0,3})\.vtt"),
        burn=[
            n
            for n in _numbers(entry / BURN, r"([1-9][0-9]{0,3})")
            if master(burn_dir(entry, n)).is_file()
        ],
    )


def playlist_uris(text: str) -> list[str]:
    """Every file an HLS playlist names: its media lines and its URI="..." attributes."""
    found = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            found.append(line)
        elif line.startswith("#"):
            found += re.findall(r'URI="([^"]*)"', line)
    return found


def playlist_problems(entry: Path, folder: Path, final: str) -> list[str]:
    """What the playlists of a new version name that a viewer could not fetch, before it is
    put in place: `folder` is still its .part folder, which will be `final` ("base",
    "burn/7") in `entry`. Each named file must be there and be one vpstream serves."""
    problems = []
    for playlist in sorted(folder.glob("*.m3u8")):
        for uri in playlist_uris(playlist.read_text(encoding="utf-8", errors="replace")):
            rel = posixpath.normpath(posixpath.join(final, uri))
            if not _SERVED.fullmatch(rel):
                problems.append(f"{playlist.name} names {uri!r}, which cannot be served")
                continue
            inside = rel.startswith(final + "/")
            path = folder / rel[len(final) + 1 :] if inside else entry / rel
            if not path.is_file():
                problems.append(f"{playlist.name} names {uri!r}, which is not there")
    return problems


def served_file(entry: Path, rel: str) -> Path | None:
    """The file a viewer asked for, `rel` inside a video's folder, or None when that is not
    a file vpstream serves. A link that leads out of the folder is never followed."""
    if not _SERVED.fullmatch(rel):
        return None
    real_entry = os.path.realpath(entry)
    path = os.path.realpath(entry / rel)
    if os.path.commonpath([real_entry, path]) != real_entry or not os.path.isfile(path):
        return None
    return Path(path)


# --- the master playlist of a burned-in version

_ATTRIBUTE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def attributes(line: str) -> dict[str, str]:
    """The attributes of an HLS tag line, values as written (quoted ones with their quotes)."""
    _, _, rest = line.partition(":")
    return {m[1]: m[2] for m in _ATTRIBUTE.finditer(rest)}


def _tag(name: str, attrs: dict[str, str]) -> str:
    return f"#{name}:" + ",".join(f"{k}={v}" for k, v in attrs.items())


def burn_master(base_text: str, burn_text: str, base_prefix: str = "../../base/") -> str:
    """A master playlist for a burned-in version: its own video (from ffmpeg's master of the
    burn, `burn_text`) with the audio alternates of the base version (`base_text`), whose
    playlists are found under `base_prefix`. Pure, so it is unit tested."""
    base_lines = base_text.splitlines()
    burn_lines = burn_text.splitlines()
    base_inf = next((attributes(x) for x in base_lines if x.startswith("#EXT-X-STREAM-INF:")), {})
    try:
        at = next(i for i, x in enumerate(burn_lines) if x.startswith("#EXT-X-STREAM-INF:"))
        video_uri = burn_lines[at + 1].strip()
    except (StopIteration, IndexError):
        raise ValueError("the burned-in version's playlist names no video") from None
    inf = attributes(burn_lines[at])
    out = [line for line in burn_lines[:at] if line.startswith("#EXT")]
    for line in base_lines:
        if line.startswith("#EXT-X-MEDIA:"):
            attrs = attributes(line)
            uri = attrs.get("URI", '""').strip('"')
            attrs["URI"] = f'"{base_prefix}{uri}"'
            out.append(_tag("EXT-X-MEDIA", attrs))
    if "AUDIO" in base_inf:
        inf["AUDIO"] = base_inf["AUDIO"]
        audio_codecs = base_inf.get("CODECS", '""').strip('"').split(",")[1:]
        video_codecs = inf.get("CODECS", '""').strip('"').split(",")[:1]
        if audio_codecs and video_codecs != [""]:
            inf["CODECS"] = '"' + ",".join(video_codecs + audio_codecs) + '"'
    out += [_tag("EXT-X-STREAM-INF", inf), video_uri]
    return "\n".join(out) + "\n"


# --- the whole cache, for `vpstream prepared`


def stale(entry: Path, source: str) -> bool:
    """Whether `entry` no longer belongs to `source`: it is gone or was changed."""
    try:
        return source_key(source) != entry.name
    except OSError:
        return True


@dataclass
class Entry:
    key: str
    source: str
    stale: bool
    base: str  # COPY, TRANSCODE or "" when the base version is not there
    soft: list[int]
    burn: list[int]
    bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "source": media.shown(self.source)}


def entries(cache_dir: Path) -> list[Entry]:
    """Every video in the cache, by source path."""
    found = []
    try:
        folders = sorted(root(cache_dir).iterdir())
    except OSError:
        return []
    for folder in folders:
        if not _KEY.fullmatch(folder.name) or not folder.is_dir():
            continue
        meta = read_meta(folder) or {}
        source = str(meta.get("source") or "")
        have = versions(folder)
        found.append(
            Entry(
                key=folder.name,
                source=source,
                stale=not source or stale(folder, source),
                base=str(meta.get("base") or "") if have.base else "",
                soft=have.soft,
                burn=have.burn,
                bytes=dir_size(folder),
            )
        )
    return sorted(found, key=lambda e: (e.source, e.key))


TRASH = ".trash-"  # a folder moved aside to be removed


def take(entry: Path) -> Path:
    """Move a video's folder aside in one step, so nothing sees half of it while it is
    removed, and return where it went (`entry` itself when it is gone already)."""
    trash = entry.with_name(f"{TRASH}{entry.name}-{secrets.token_hex(4)}")
    try:
        os.rename(entry, trash)
    except FileNotFoundError:
        return entry
    return trash


def remove(folder: Path) -> None:
    shutil.rmtree(folder, ignore_errors=True)


def sweep(cache_dir: Path) -> int:
    """Remove what a prepare that was cut off (a crash, a restart) left half done. Returns
    how many folders went."""
    removed = 0
    for leftover in [
        *root(cache_dir).glob(f"*/*{PART}"),
        *root(cache_dir).glob(f"*/*/*{PART}"),
        *root(cache_dir).glob(f"*/{SUBS}/*{PART}.vtt"),  # see subtitles.extract_webvtt
        *root(cache_dir).glob(f"*/{WORK}*"),
        *root(cache_dir).glob(f"{TRASH}*"),  # a remove that was cut off
    ]:
        if leftover.is_dir() and not leftover.is_symlink():
            shutil.rmtree(leftover, ignore_errors=True)
        else:
            leftover.unlink(missing_ok=True)
        removed += 1
    return removed
