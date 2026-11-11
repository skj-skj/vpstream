"""Small helpers for vpstream's own files."""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
from pathlib import Path

SPARE_BYTES = 512 * 1024 * 1024  # left free on a disk after vpstream writes to it


def write_atomic(path: Path, text: str) -> None:
    """Write a file whole: a crash leaves the old content or the new, never half of it."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def source_key(source: str | Path) -> str:
    """A name for one version of a file: its path, size and change time. A file that is
    changed or replaced gets another name, so nothing made from the old one passes for it.
    Raises OSError when the file cannot be looked at."""
    st = os.stat(source)
    key = f"{source}\0{st.st_size}\0{st.st_mtime_ns}".encode("utf-8", "surrogateescape")
    return hashlib.sha256(key).hexdigest()[:32]


def dir_size(path: Path) -> int:
    """The bytes of every file under `path`; links count as themselves, not what they name."""
    total = 0
    for folder, _, names in os.walk(path):
        for name in names:
            with contextlib.suppress(OSError):  # removed while it was counted
                total += os.lstat(os.path.join(folder, name)).st_size
    return total


def has_room(folder: Path, need: int) -> bool:
    """Whether `need` bytes fit in `folder`'s disk with SPARE_BYTES still free after."""
    return shutil.disk_usage(folder).free >= need + SPARE_BYTES


def under(path: str, folder: str) -> bool:
    """Whether `path` is `folder` itself or anything inside it (paths as text, absolute)."""
    top = folder.rstrip("/") or "/"
    return path == top or path.startswith(top if top == "/" else top + "/")
