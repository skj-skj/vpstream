"""A copy of an MKV file with a proper seek index (see `mkv`), made with mkvmerge.

Nothing is re-encoded: mkvmerge writes the same tracks again with an index entry for every
video and audio track. The copy goes in the cache under a name made from the original's path,
size and change time, so it is made once and found again by the next stream of that file.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from vpstream import mkv
from vpstream.tools import Runner, low_priority_prefix, require, tail

TOOL = "mkvmerge"
FOLDER = "index"  # under the cache folder
IDENTIFY_TIMEOUT = 120
SPARE_BYTES = 512 * 1024 * 1024  # left free on the disk after the copy
POLL_SECONDS = 0.5


class ReindexError(RuntimeError):
    """The copy could not be made; the message says why."""


class Cancelled(Exception):
    pass


def copy_path(cache_dir: Path, source: str) -> Path:
    """Where the copy of `source` goes. A changed original gets a new name."""
    st = os.stat(source)
    key = f"{source}\0{st.st_size}\0{st.st_mtime_ns}".encode("utf-8", "surrogateescape")
    return cache_dir / FOLDER / f"{hashlib.sha256(key).hexdigest()[:32]}.mkv"


def ready(cache_dir: Path, source: str) -> Path | None:
    """The copy of `source` when it has been made, else None."""
    try:
        path = copy_path(cache_dir, source)
    except OSError:
        return None
    return path if path.is_file() else None


def track_ids(identify: dict[str, Any]) -> list[int]:
    """mkvmerge's ids of the video and audio tracks, from `mkvmerge -J`."""
    return [
        int(t["id"])
        for t in identify.get("tracks") or []
        if t.get("type") in ("video", "audio") and isinstance(t.get("id"), int)
    ]


def mkvmerge_args(source: str, out: Path, ids: list[int]) -> list[str]:
    cues = [arg for tid in ids for arg in ("--cues", f"{tid}:iframes")]
    return [TOOL, "-q", "-o", str(out), *cues, source]


def _identify(runner: Runner, source: str) -> dict[str, Any]:
    res = runner.run([TOOL, "-J", source], timeout=IDENTIFY_TIMEOUT)
    try:
        data = json.loads(res.stdout)
    except ValueError:
        data = None
    if res.returncode != 0 or not isinstance(data, dict):
        raise ReindexError(f"mkvmerge cannot read it: {tail(res, stdout=True)}")
    return data


def make(runner: Runner, source: str, out: Path, cancel: threading.Event) -> None:
    """Write the copy of `source` to `out`, at low priority. Raises Cancelled when `cancel`
    is set first, ReindexError when it fails and ToolMissing without mkvmerge. A copy that
    was not finished is never left behind under `out`."""
    program = require(runner, TOOL, "a proper seek index in MKV files")
    ids = track_ids(_identify(runner, source))
    out.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    need = os.stat(source).st_size + SPARE_BYTES
    if shutil.disk_usage(out.parent).free < need:
        raise ReindexError(f"not enough free space in {out.parent} for a copy")
    part = out.with_name(out.name + ".part")
    args = mkvmerge_args(source, part, ids)
    args[0] = program
    try:
        proc = subprocess.Popen(
            [*low_priority_prefix(runner), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        while proc.poll() is None:
            if cancel.wait(POLL_SECONDS):
                proc.terminate()
                proc.wait()
                raise Cancelled
        said = proc.stdout.read() if proc.stdout else ""
        if proc.returncode not in (0, 1):  # 1: done, with warnings
            raise ReindexError(f"mkvmerge failed: {said.strip()[-300:] or proc.returncode}")
        problem = mkv.index_problem(part)
        if problem:
            raise ReindexError(f"the copy is no better: {problem}")
        os.replace(part, out)
    finally:
        part.unlink(missing_ok=True)
