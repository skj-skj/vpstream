"""Letting the service read the videos of the person who starts a stream.

The daemon and Caddy run as the service user, not as whoever types `vpstream start`. When the
service cannot read what it is asked to stream, the command line gives it access itself: an
ACL entry for the service user alone, read on the videos and enter on the folders, set with
`setfacl`. That needs no root, only that the caller owns the files, and it opens them to no
other user. Where the daemon runs as the caller (a trial run, Termux) nothing is ever needed.

What was given is written down in a file of the caller's own, and taken back by `vpstream
stop`, by the next `vpstream start` and by `vpstream revoke`. An entry that was there before
is never written down, so it is never removed.
"""

from __future__ import annotations

import json
import os
import pwd
import re
from dataclasses import dataclass, field
from pathlib import Path

from vpstream import media
from vpstream.files import write_atomic
from vpstream.tools import Runner, require, tail

TOOL = "setfacl"
READ_TOOL = "getfacl"  # from the same package
_BITS = {"r": 4, "w": 2, "x": 1}
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


def _acl(runner: Runner, path: Path | str) -> list[str] | None:
    """A file's ACL as `getfacl` prints it, with numbers for users; None when unknown."""
    if not runner.which(READ_TOOL):
        return None
    res = runner.run([READ_TOOL, "-c", "-n", "-p", "--", str(path)], timeout=30)
    return res.stdout.splitlines() if res.returncode == 0 else None


# What every file has, ACL or not. The mask comes with the first extra entry and stays when
# the last one is removed, so a file that has only these and a mask carries nothing else.
_BASE = ("user::", "group::", "other::")


def entry(runner: Runner, path: Path | str, uid: int) -> int | None:
    """The permission bits of the ACL entry for the user `uid` on a file, or None when it
    has none. Asked from `getfacl`, with numbers for users, so no name needs reading."""
    for line in _acl(runner, path) or []:
        kind, _, rest = line.partition(":")
        owner, _, perms = rest.partition(":")
        if kind == "user" and owner == str(uid):
            return sum(bit for letter, bit in _BITS.items() if letter in perms[:3])
    return None


def record_file() -> Path:
    """The caller's own list of what `start` gave the service."""
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(state) / "vpstream" / "grants.json"


def recorded(path: Path) -> dict[str, list[str]]:
    """Service user -> paths, as written down. An unusable file counts as empty."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        user: [p for p in paths if isinstance(p, str)]
        for user, paths in data.items()
        if isinstance(user, str) and _USER.fullmatch(user) and isinstance(paths, list)
    }


def _record(path: Path, grants: dict[str, list[str]]) -> None:
    grants = {user: paths for user, paths in grants.items() if paths}
    if not grants:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_atomic(path, json.dumps(grants, indent=1) + "\n")


@dataclass
class Plan:
    """What to change so `user` can read a stream's source."""

    grants: dict[str, list[Path]] = field(default_factory=dict)  # "r", "rx" or "x" -> paths
    not_mine: list[Path] = field(default_factory=list)  # lacking, and not the caller's to change

    service_uid: int | None = None  # whose entries to look for; None when it is not known
    runner: Runner | None = None  # how to look for them

    def add(self, path: Path, perms: str, uid: int) -> None:
        need = sum(_BITS[letter] for letter in perms)
        try:
            st = path.stat()
        except OSError:
            return  # not there, or not the caller's to look at: the daemon says so
        if st.st_mode & need == need:
            return  # open to every user already
        has = None
        if self.service_uid is not None and self.runner is not None:
            has = entry(self.runner, path, self.service_uid)
        if has is not None and has & need == need:
            return  # given before, by hand or by an earlier start
        if st.st_uid == uid:
            self.grants.setdefault(perms, []).append(path)
        else:
            self.not_mine.append(path)


def plan(
    kind: str,
    source: Path,
    uid: int,
    service_uid: int | None = None,
    runner: Runner | None = None,
) -> Plan:
    """Read on every video, read and enter on a streamed folder, enter on the folders above."""
    found = Plan(service_uid=service_uid, runner=runner)
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


def _uid(user: str) -> int | None:
    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError:
        return None


def grant(
    runner: Runner,
    user: str,
    kind: str,
    source: Path,
    uid: int | None = None,
    record: Path | None = None,
) -> int:
    """Give `user` access to a stream's source. Returns how many files and folders were
    changed; raises AccessError when it cannot be done from here."""
    if not _USER.fullmatch(user):
        raise AccessError(f"not a user name: {user!r}")
    record = record or record_file()
    todo = plan(kind, source, os.geteuid() if uid is None else uid, _uid(user), runner)
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
    # Written down before the change, never after: a start that is cut off must not leave
    # an entry nobody knows about. Taking back one that was never made is harmless.
    known = recorded(record)
    new = [str(path) for paths in todo.grants.values() for path in paths]
    known[user] = list(dict.fromkeys([*known.get(user, []), *new]))
    _record(record, known)
    for call in commands(user, todo.grants):
        res = runner.run(call, timeout=60)
        if res.returncode != 0:
            raise AccessError(f"setfacl failed: {tail(res, stdout=True)}")
    return sum(len(paths) for paths in todo.grants.values())


def _mine(runner: Runner, path: str, service_uid: int, me: int) -> bool:
    try:
        return os.stat(path).st_uid == me and entry(runner, path, service_uid) is not None
    except OSError:
        return False


def _drop_bare_masks(runner: Runner, paths: list[str]) -> None:
    """Remove the mask setfacl leaves behind when the last extra entry goes, so the file is
    as it was (`ls -l` shows no `+`). Only where nothing else is left: `setfacl -x` has just
    made the mask equal to the group's own bits, so removing it changes no access."""
    bare = []
    for path in paths:
        lines = _acl(runner, path)
        if (
            lines
            and any(line.startswith("mask::") for line in lines)
            and all(line.startswith((*_BASE, "mask::")) for line in lines if line.strip())
        ):
            bare.append(path)
    if bare:
        runner.run([TOOL, "-b", "--", *bare], timeout=60)  # a failure leaves only the mask


def revoke(runner: Runner, record: Path | None = None, uid: int | None = None) -> int:
    """Take back every entry that was written down. Returns how many were removed; raises
    AccessError when some could not be, and keeps those written down."""
    record = record or record_file()
    known = recorded(record)
    if not known:
        return 0
    me = os.geteuid() if uid is None else uid
    removed = 0
    left: dict[str, list[str]] = {}
    problem = ""
    for user, paths in known.items():
        service_uid = _uid(user)
        # Only what is still there, still the caller's, and still carries the entry.
        there = [p for p in paths if service_uid is not None and _mine(runner, p, service_uid, me)]
        if there:
            require(runner, TOOL, "taking back the vpstream service's access to your videos")
        for start in range(0, len(there), BATCH):
            batch = there[start : start + BATCH]
            res = runner.run([TOOL, "-x", f"u:{user}", "--", *batch], timeout=60)
            if res.returncode == 0:
                removed += len(batch)
                _drop_bare_masks(runner, batch)
            else:
                left.setdefault(user, []).extend(batch)
                problem = tail(res, stdout=True)
    _record(record, left)
    if problem:
        raise AccessError(f"setfacl could not take back every entry: {problem}")
    return removed
