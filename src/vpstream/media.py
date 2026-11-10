"""Which files are videos, in what order a folder lists them, and what type they are sent as."""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path

# Extension -> the Content-Type viewers get. Kept here so it does not depend on the host's
# own table, which differs between systems.
VIDEO_TYPES = {
    ".mkv": "video/x-matroska",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".ts": "video/mp2t",
    ".m2ts": "video/mp2t",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
    ".ogv": "video/ogg",
    ".3gp": "video/3gpp",
}


def content_type(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    return (
        VIDEO_TYPES.get(suffix) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    )


def natural_key(name: str) -> list[int | str]:
    """A sort key that puts "episode 2" before "episode 10"."""
    # re.split puts the digit runs at the odd places, so a place always holds the same type.
    parts = re.split(r"(\d+)", name)
    return [int(part) if i % 2 else part.casefold() for i, part in enumerate(parts)]


def scan(folder: Path) -> list[Path]:
    """The videos at the top level of a folder, in natural order. Subfolders, hidden files
    and files that are not videos are left out."""
    found = [
        entry
        for entry in folder.iterdir()
        if entry.suffix.lower() in VIDEO_TYPES
        and not entry.name.startswith(".")
        and entry.is_file()
    ]
    return sorted(found, key=lambda p: (natural_key(p.name), p.name))
