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

import contextlib
import json
import os
import pwd
import re
import shlex
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from vpstream import media
from vpstream.files import read_json_object, write_atomic
from vpstream.tools import Runner, require, tail

if sys.platform == "win32":
    raise ImportError("vpstream.platform.linux is for Linux; Windows has platform.windows")

TOOL = "setfacl"
READ_TOOL = "getfacl"  # from the same package
_BITS = {"r": 4, "w": 2, "x": 1}
BATCH = 200  # paths per setfacl call
_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")  # goes into a command, so nothing else


class AccessError(RuntimeError):
    """Access could not be given; the message says why and what to do."""


def setfacl_command(user: str, perms: str, path: Path | str) -> str:
    """The command that gives `user` access to `path`, to copy: the path quoted for a shell."""
    return f"setfacl -m u:{user}:{perms} {shlex.quote(str(path))}"


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


def _batches[T](items: Sequence[T]) -> Iterator[Sequence[T]]:
    """BATCH items at a time, for one call of setfacl or getfacl each."""
    for start in range(0, len(items), BATCH):
        yield items[start : start + BATCH]


def _acls(runner: Runner, paths: Sequence[Path | str]) -> list[list[str] | None]:
    """The ACLs of many files, in their order, asked from `getfacl` a batch at a time. It
    prints one block per file, each ended by an empty line. When a file cannot be read it is
    left out and getfacl fails, so then that batch is asked again one file at a time."""
    if not runner.which(READ_TOOL):
        return [None] * len(paths)
    found: list[list[str] | None] = []
    for batch in _batches([str(path) for path in paths]):
        res = runner.run([READ_TOOL, "-c", "-n", "-p", "--", *batch], timeout=30)
        printed = res.stdout.strip("\n")
        blocks = [block.splitlines() for block in printed.split("\n\n")] if printed else []
        if res.returncode == 0 and len(blocks) == len(batch):
            found += blocks
        else:
            found += [_acl(runner, path) for path in batch]
    return found


def _bits(lines: list[str] | None, uid: int) -> int | None:
    """The permission bits of the ACL entry for the user `uid` in an ACL as `getfacl -n`
    prints it, or None when it has none."""
    for line in lines or []:
        kind, _, rest = line.partition(":")
        owner, _, perms = rest.partition(":")
        if kind == "user" and owner == str(uid):
            return sum(bit for letter, bit in _BITS.items() if letter in perms[:3])
    return None


def entry(runner: Runner, path: Path | str, uid: int) -> int | None:
    """The permission bits of the ACL entry for the user `uid` on a file, or None when it
    has none. Asked from `getfacl`, with numbers for users, so no name needs reading."""
    return _bits(_acl(runner, path), uid)


def entries(runner: Runner, paths: Sequence[Path | str], uid: int) -> list[int | None]:
    """`entry` for many files, with one getfacl call per batch."""
    return [_bits(lines, uid) for lines in _acls(runner, paths)]


def record_file() -> Path:
    """The caller's own list of what `start` gave the service."""
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(state) / "vpstream" / "grants.json"


def recorded(path: Path) -> dict[str, list[str]]:
    """Service user -> paths, as written down. An unusable file counts as empty."""
    data = read_json_object(path) or {}
    return {
        user: [p for p in paths if isinstance(p, str)]
        for user, paths in data.items()
        if isinstance(user, str) and _USER.fullmatch(user) and isinstance(paths, list)
    }


def _record(path: Path, grants: dict[str, list[str]]) -> None:
    grants = {user: paths for user, paths in grants.items() if paths}
    if not grants:
        path.unlink(missing_ok=True)
        # Its folder too, which only this file lives in: nothing is left behind once
        # everything is taken back. One that holds anything else stays.
        with contextlib.suppress(OSError):
            path.parent.rmdir()
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_atomic(path, json.dumps(grants, indent=1) + "\n")


@dataclass
class Plan:
    """What to change so `user` can read a stream's source."""

    grants: dict[str, list[Path]] = field(default_factory=dict)  # "r", "rx" or "x" -> paths
    not_mine: list[Path] = field(default_factory=list)  # lacking, and not the caller's to change


def _wanted(kind: str, source: Path) -> list[tuple[Path, str]]:
    """Each path the service must use, with what it needs there: read on every video, read
    and enter on a streamed folder, enter on the folders above."""
    wanted: list[tuple[Path, str]] = []
    if kind == "dir":
        wanted.append((source, "rx"))
        try:
            videos = media.scan(source)
        except OSError:
            videos = []
    else:
        videos = [source]
    # The daemon follows a link once and reads what it names, so that is what is opened.
    files = list(dict.fromkeys(video.resolve() for video in videos))
    wanted += [(file, "r") for file in files]
    above = dict.fromkeys(
        parent for path in (source.absolute(), *files) for parent in reversed(path.parents)
    )
    wanted += [(folder, "x") for folder in above if folder != source]
    return wanted


def plan(
    kind: str,
    source: Path,
    uid: int,
    service_uid: int | None = None,
    runner: Runner | None = None,
) -> Plan:
    """What to grant so the service can use a stream's source (see `_wanted`). The service's
    own entries are looked for (with `service_uid` and `runner`) only on what other users
    cannot use already."""
    lacking: list[tuple[Path, str, int, int]] = []  # path, perms, their bits, its owner
    for path, perms in _wanted(kind, source):
        need = sum(_BITS[letter] for letter in perms)
        try:
            st = path.stat()
        except OSError:
            continue  # not there, or not the caller's to look at: the daemon says so
        if st.st_mode & need != need:  # else open to every user already
            lacking.append((path, perms, need, st.st_uid))
    if service_uid is not None and runner is not None:
        given = entries(runner, [path for path, *_ in lacking], service_uid)
    else:
        given = [None] * len(lacking)
    found = Plan()
    for (path, perms, need, owner), has in zip(lacking, given, strict=True):
        if has is not None and has & need == need:
            continue  # given before, by hand or by an earlier start
        if owner == uid:
            found.grants.setdefault(perms, []).append(path)
        else:
            found.not_mine.append(path)
    return found


def commands(user: str, grants: dict[str, list[Path]]) -> list[list[str]]:
    """The setfacl calls that carry out a plan."""
    calls = []
    for perms, paths in grants.items():
        for batch in _batches(paths):
            calls.append([TOOL, "-m", f"u:{user}:{perms}", "--", *map(str, batch)])
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
            f"to open up. Its owner can: {setfacl_command(user, perms, first)} (root: with sudo)"
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


def _owned(path: str, me: int) -> bool:
    try:
        return os.stat(path).st_uid == me
    except OSError:
        return False


def _drop_bare_masks(runner: Runner, paths: list[str]) -> None:
    """Remove the mask setfacl leaves behind when the last extra entry goes, so the file is
    as it was (`ls -l` shows no `+`). Only where nothing else is left: `setfacl -x` has just
    made the mask equal to the group's own bits, so removing it changes no access."""
    bare = []
    for path, lines in zip(paths, _acls(runner, paths), strict=True):
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
        owned = [p for p in paths if service_uid is not None and _owned(p, me)]
        there = []
        if service_uid is not None:
            given = entries(runner, owned, service_uid)
            there = [p for p, has in zip(owned, given, strict=True) if has is not None]
        if there:
            require(runner, TOOL, "taking back the vpstream service's access to your videos")
        for batch in _batches(there):
            res = runner.run([TOOL, "-x", f"u:{user}", "--", *batch], timeout=60)
            if res.returncode == 0:
                removed += len(batch)
                _drop_bare_masks(runner, list(batch))
            else:
                left.setdefault(user, []).extend(batch)
                problem = tail(res, stdout=True)
    _record(record, left)
    if problem:
        raise AccessError(f"setfacl could not take back every entry: {problem}")
    return removed
