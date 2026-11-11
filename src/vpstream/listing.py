"""`vpstream ls [FOLDER]`: the videos of a folder, their tracks and what is prepared of them.

The files are read with ffprobe by whoever runs the command; what is prepared comes from the
daemon's list of the cache, matched by `files.source_key`, as the daemon names folders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vpstream import media, text
from vpstream.files import source_key
from vpstream.probe import ProbeError, probe
from vpstream.tools import Runner

Prepared = dict[str, dict[str, Any]]  # key -> a row of the daemon's `prepared` list


@dataclass
class Row:
    """One video, or one subfolder (`videos` > 0 or a problem)."""

    name: str
    folder: bool = False
    duration: float = 0.0
    audio: list[str] = field(default_factory=list)
    subs: list[str] = field(default_factory=list)
    prepared: str = ""
    videos: int = 0  # a folder: how many videos are at its top level
    ready: int = 0  # and how many of them have a base version
    problem: str = ""


def describe(row: dict[str, Any] | None) -> str:
    """What the cache holds of one video, in a few words."""
    if row is None or not row.get("base"):
        return "not prepared"
    parts = [f"base ({row['base']})"]
    if row.get("soft"):
        parts.append("soft " + ",".join(map(str, row["soft"])))
    if row.get("burn"):
        parts.append("burned in " + ",".join(map(str, row["burn"])))
    return ", ".join(parts)


def _key(video: Path) -> str | None:
    try:
        return source_key(str(video.resolve()))
    except OSError:
        return None


def _video(runner: Runner, video: Path, cache: Prepared | None) -> Row:
    row = Row(media.shown(video.name))
    key = _key(video)
    if cache is not None:
        row.prepared = describe(cache.get(key or ""))
    try:
        info = probe(runner, video.resolve())
    except (ProbeError, OSError) as exc:
        row.problem = str(exc)
        return row
    row.duration = info.duration
    row.audio = [a.language for a in info.audio]
    row.subs = [f"{s.number} {s.language} {s.treatment}" for s in info.subtitles]
    return row


def _folder(folder: Path, cache: Prepared | None) -> Row:
    row = Row(media.shown(folder.name) + "/", folder=True)
    try:
        videos = media.scan(folder)
    except OSError as exc:
        row.problem = f"cannot be read: {exc.strerror or exc}"
        return row
    row.videos = len(videos)
    if cache is not None:
        row.ready = sum(1 for v in videos if (cache.get(_key(v) or "") or {}).get("base"))
    return row


def listing(runner: Runner, folder: Path, cache: Prepared | None) -> list[Row]:
    """Every video at the top level of `folder`, in natural order, then every subfolder
    that holds videos. `cache` is None when the daemon could not be asked. Raises OSError
    when the folder cannot be read. Hidden names are left out, as a stream leaves them."""
    rows = [_video(runner, video, cache) for video in media.scan(folder)]
    subfolders = sorted(
        (e for e in folder.iterdir() if e.is_dir() and not e.name.startswith(".")),
        key=lambda p: (media.natural_key(p.name), p.name),
    )
    for sub in subfolders:
        row = _folder(sub, cache)
        if row.videos or row.problem:
            rows.append(row)
    return rows


def format_listing(rows: list[Row], asked: bool) -> str:
    """The rows as text; `asked` says whether the daemon told what is prepared."""
    if not rows:
        return "No videos here."
    lines = []
    for row in rows:
        if row.folder:
            what = row.problem or f"{row.videos} videos" + (
                f", {row.ready} prepared" if asked else ""
            )
            lines.append(f"{row.name}  {what}")
            continue
        lines.append(f"{row.name}  {text.duration(row.duration)}" if not row.problem else row.name)
        if row.problem:
            lines.append(f"  cannot be read: {row.problem}")
        else:
            lines.append(f"  audio: {', '.join(row.audio) or 'none'}")
            lines.append(f"  subtitles: {'; '.join(row.subs) or 'none'}")
        if asked:
            lines.append(f"  browser mode: {row.prepared}")
    if not asked:
        lines.append("(the daemon does not answer, so what is prepared is not shown)")
    return "\n".join(lines)
