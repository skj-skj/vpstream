"""Letting the service read the videos of the person who starts a stream.

The daemon and Caddy run as the service user, not as whoever types `vpstream start`. When the
service cannot read what it is asked to stream, the command line gives it access itself: an
ACL entry for the service user alone, read on the videos and enter on the folders, set with
`setfacl`. That needs no root, only that the caller owns the files, and it opens them to no
other user. Where the daemon runs as the caller (a trial run, Termux) nothing is ever needed.
"""

from __future__ import annotations

import os
import pwd
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from vpstream import media
from vpstream.tools import Runner, require, tail

TOOL = "setfacl"
BATCH = 200  # paths per setfacl call
_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")  # goes into a command, so nothing else


class AccessError(RuntimeError):
    """Access could not be given; the message says why and what to do."""


def service_user() -> str:
    """The user this process runs as: for the daemon, the one that must read the videos."""
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return str(os.geteuid())


@dataclass
class Plan:
    """What to change so `user` can read a stream's source."""

    grants: dict[str, list[Path]] = field(default_factory=dict)  # "r", "rx" or "x" -> paths
    not_mine: list[Path] = field(default_factory=list)  # lacking, and not the caller's to change

    def add(self, path: Path, perms: str, uid: int) -> None:
        need = {"r": stat.S_IROTH, "x": stat.S_IXOTH, "rx": stat.S_IROTH | stat.S_IXOTH}[perms]
        try:
            st = path.stat()
        except OSError:
            return  # not there, or not the caller's to look at: the daemon says so
        if st.st_mode & need == need:
            return  # open to every user already
        if st.st_uid == uid:
            self.grants.setdefault(perms, []).append(path)
        else:
            self.not_mine.append(path)


def plan(kind: str, source: Path, uid: int) -> Plan:
    """Read on every video, read and enter on a streamed folder, enter on the folders above."""
    found = Plan()
    if kind == "dir":
        found.add(source, "rx", uid)
        try:
            videos = media.scan(source)
        except OSError:
            videos = []
    else:
        videos = [source]
    # The daemon follows a link once and reads what it names, so that is what is opened.
    files = list(dict.fromkeys(video.resolve() for video in videos))
    for file in files:
        found.add(file, "r", uid)
    above = dict.fromkeys(
        parent for path in (source.absolute(), *files) for parent in reversed(path.parents)
    )
    for folder in above:
        if folder != source:
            found.add(folder, "x", uid)
    return found


def commands(user: str, grants: dict[str, list[Path]]) -> list[list[str]]:
    """The setfacl calls that carry out a plan."""
    calls = []
    for perms, paths in grants.items():
        for start in range(0, len(paths), BATCH):
            batch = [str(p) for p in paths[start : start + BATCH]]
            calls.append([TOOL, "-m", f"u:{user}:{perms}", "--", *batch])
    return calls


def grant(runner: Runner, user: str, kind: str, source: Path, uid: int | None = None) -> int:
    """Give `user` access to a stream's source. Returns how many files and folders were
    changed; raises AccessError when it cannot be done from here."""
    if not _USER.fullmatch(user):
        raise AccessError(f"not a user name: {user!r}")
    todo = plan(kind, source, os.geteuid() if uid is None else uid)
    if todo.not_mine:
        first = todo.not_mine[0]
        perms = "x" if first.is_dir() and first != source else "rX"
        raise AccessError(
            f"the vpstream service (user {user}) cannot read {first}, and it is not your file "
            f"to open up. Its owner or root can: sudo setfacl -m u:{user}:{perms} {first}"
        )
    if not todo.grants:
        return 0
    require(runner, TOOL, "giving the vpstream service access to your videos")
    for call in commands(user, todo.grants):
        res = runner.run(call, timeout=60)
        if res.returncode != 0:
            raise AccessError(f"setfacl failed: {tail(res, stdout=True)}")
    return sum(len(paths) for paths in todo.grants.values())
