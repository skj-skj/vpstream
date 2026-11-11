"""One running stream: what it serves, under which alias, and what each URL under it gives.

Nothing here talks to Caddy or the firewall; `control` does that.
"""

from __future__ import annotations

import os
import re
import secrets
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vpstream import access, gate, media

DIRECT = "direct"
MODES = (DIRECT,)  # browser mode comes with the prepare command
DEFAULT_POLICY = {DIRECT: gate.OPEN}
DEFAULT_TTL_MINUTES = 180.0
MAX_TTL_MINUTES = 7 * 24 * 60.0
PLAYLIST = "playlist.m3u"
# Names a stream cannot take: they are, or will be, pages of vpstream itself.
RESERVED = frozenset({"caps", "api", "assets", "static", "player", "hls", "wait", "index"})
_ALIAS = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


class StreamError(ValueError):
    """A stream cannot be started or changed as asked; the message says why."""


def check_alias(text: Any) -> str:
    """The alias in its one spelling (lower case), or a StreamError."""
    alias = text.strip().lower() if isinstance(text, str) else ""
    if not _ALIAS.fullmatch(alias):
        raise StreamError(
            f"not a usable alias: {text!r}. Use 1 to 32 letters, digits, - or _, "
            "starting with a letter or digit"
        )
    if alias in RESERVED:
        raise StreamError(f"the alias {alias!r} is reserved; pick another")
    return alias


def random_alias() -> str:
    while True:
        alias = "".join(secrets.choice(string.ascii_lowercase) for _ in range(3))
        if alias not in RESERVED:
            return alias


def check_policy(text: Any) -> str:
    if text not in gate.POLICIES:
        raise StreamError(f"not a policy: {text!r}. Use one of: {', '.join(gate.POLICIES)}")
    return str(text)


class Unreadable(StreamError):
    """The daemon's user cannot read what it was asked to stream. The command line can
    often put that right itself (see `access`), so the answer says what and for whom."""

    def __init__(self, path: Path) -> None:
        self.path = str(path)
        self.user = access.service_user()
        super().__init__(
            f"the vpstream service (user {self.user}) cannot read {path}. `vpstream start`, run "
            "by the owner of the files, gives it access by itself; anyone else can with: "
            f"sudo setfacl -m u:{self.user}:rX {path}"
        )


def _is(path: Path, check: str) -> bool:
    """`path.is_file()` or `path.is_dir()`, but a folder that cannot be entered is told
    apart from a path that is not there."""
    try:
        path.stat()
    except PermissionError:
        raise Unreadable(path) from None
    except OSError:
        return False
    return bool(getattr(path, check)())


def _videos(kind: str, path: Path) -> list[str]:
    if not path.is_absolute():
        raise StreamError(f"give the full path, starting with /: {path}")
    if kind == "file":
        if not _is(path, "is_file"):
            raise StreamError(f"not a file: {path}")
        if not media.is_video(path):
            raise StreamError(
                f"not a video file: {path}. vpstream streams: "
                + " ".join(sorted(media.VIDEO_TYPES))
            )
        found = [path]
    else:
        if not _is(path, "is_dir"):
            raise StreamError(f"not a folder: {path}")
        if not os.access(path, os.R_OK | os.X_OK):
            raise Unreadable(path)
        found = media.scan(path)
        if not found:
            raise StreamError(f"no videos at the top level of {path}")
    # A link is followed now, once: what is streamed is the file it names at this moment,
    # and that must be a video too.
    found = [real for real in (video.resolve() for video in found) if media.is_video(real)]
    if not found:
        raise StreamError(
            f"no videos at the top level of {path}"
            if kind == "dir"
            else f"not a video file: {path}"
        )
    for video in found:
        if not os.access(video, os.R_OK):
            raise Unreadable(video)
    return [str(video) for video in found]


def _ttl_seconds(value: Any) -> float | None:
    if value is None:
        value = DEFAULT_TTL_MINUTES
    if isinstance(value, bool) or not isinstance(value, int | float) or not value >= 0:
        raise StreamError(f"the time limit must be a number of minutes, 0 or more: {value!r}")
    if value > MAX_TTL_MINUTES:
        raise StreamError(f"the time limit can be at most {MAX_TTL_MINUTES:g} minutes (7 days)")
    return value * 60 or None


@dataclass
class Answer:
    """What one request to the stream port gets."""

    status: int
    kind: str  # "video", "index", "playlist", "denied" or "missing"
    file: str = ""  # for a video: the file to send (the original, or its re-indexed copy)
    number: int = 0  # and its number
    alias: str = ""  # for an index or a playlist: what it lists
    names: list[str] = field(default_factory=list)


MISSING = Answer(404, "missing")
DENIED = Answer(403, "denied")


@dataclass
class Stream:
    kind: str  # "file" or "dir"
    source: str
    videos: list[str]  # full paths; a video's number is its place here, from 1, for good
    alias: str
    started: float
    expires: float | None  # when the stream stops by itself; None for no time limit
    mode: str = DIRECT
    policy: str = gate.OPEN
    unlocked_policy: str = gate.OPEN  # what Unlock goes back to
    devices: gate.Devices = field(default_factory=gate.Devices)
    # What is sent for each number: the video itself, or a copy with a proper seek index.
    served: list[str] = field(default_factory=list)
    indexing: set[int] = field(default_factory=set)  # numbers whose copy is being made
    notes: dict[int, str] = field(default_factory=dict)  # number -> why it stays slow to seek

    def __post_init__(self) -> None:
        if len(self.served) != len(self.videos):
            self.served = list(self.videos)

    @classmethod
    def create(cls, options: dict[str, Any], now: float) -> Stream:
        """A new stream from what `start` was given. Raises StreamError for anything wrong."""
        known = {"file", "dir", "mode", "alias", "ttl", "policy"}
        if extra := sorted(set(options) - known):
            raise StreamError(f"unknown option: {', '.join(extra)}")
        file, folder = options.get("file"), options.get("dir")
        if bool(file) == bool(folder):
            raise StreamError("give exactly one of a file or a folder to stream")
        source = file or folder
        if not isinstance(source, str):
            raise StreamError(f"not a path: {source!r}")
        mode = options.get("mode") or DIRECT
        if mode not in MODES:
            raise StreamError(f"not a mode: {mode!r}. This version has: {', '.join(MODES)}")
        policy = check_policy(options.get("policy") or DEFAULT_POLICY[mode])
        alias = check_alias(options["alias"]) if options.get("alias") else random_alias()
        ttl = _ttl_seconds(options.get("ttl"))
        kind = "file" if file else "dir"
        stream = cls(
            kind=kind,
            source=source,
            videos=_videos(kind, Path(source)),
            alias=alias,
            started=now,
            expires=now + ttl if ttl else None,
            mode=mode,
        )
        stream.set_policy(policy)
        return stream

    # --- changes

    def set_policy(self, policy: str) -> None:
        """Switch the policy. A lock keeps the devices that get in right now."""
        policy = check_policy(policy)
        if policy == gate.LOCKED:
            if self.policy != gate.LOCKED:
                self.devices.keep_current(self.policy)
        else:
            self.unlocked_policy = policy
        self.policy = policy

    def unlock(self) -> None:
        self.set_policy(self.unlocked_policy)

    # --- requests

    def visit(self, path: str, ip: str, user_agent: str, wants_html: bool, now: float) -> Answer:
        """Answer one request for `path` (decoded, as the viewer asked for it).

        Only a request under the alias counts as a device; anything else on the port, such
        as a scanner trying `/`, is simply not found.
        """
        if self.devices.guesser(ip):
            return MISSING
        answer = self._route(path, ip, user_agent, wants_html, now)
        # A device that gets in may ask for what is not there (a browser's favicon); from
        # anyone else it is a guess at the alias.
        if answer is MISSING and not self.devices.passes(self.policy, ip):
            self.devices.miss(ip)
        return answer

    def _route(self, path: str, ip: str, user_agent: str, wants_html: bool, now: float) -> Answer:
        parts = [part for part in path.split("/") if part]
        if not parts or parts[0].lower() != self.alias:
            return MISSING
        if not self.devices.visit(self.policy, ip, user_agent, now):
            return DENIED
        if len(parts) == 1:
            if self.kind == "file":
                return self._video(1)
            return self._listing("index" if wants_html else "playlist")
        if len(parts) == 2:
            if parts[1].lower() == PLAYLIST and self.kind == "dir":
                return self._listing("playlist")
            if re.fullmatch(r"[1-9][0-9]{0,5}", parts[1]):
                return self._video(int(parts[1]))
        return MISSING

    def _listing(self, kind: str) -> Answer:
        return Answer(200, kind, alias=self.alias, names=self.names())

    def _video(self, number: int) -> Answer:
        if not 1 <= number <= len(self.videos):
            return MISSING
        if number in self.indexing:
            return Answer(503, "preparing", number=number)
        return Answer(200, "video", self.served[number - 1], number)

    # --- as plain data

    def names(self) -> list[str]:
        return [media.shown(Path(video).name) for video in self.videos]

    def to_dict(self) -> dict[str, Any]:
        """Everything, for the state file."""
        return {
            "kind": self.kind,
            "source": self.source,
            "videos": self.videos,
            "alias": self.alias,
            "started": self.started,
            "expires": self.expires,
            "mode": self.mode,
            "policy": self.policy,
            "unlocked_policy": self.unlocked_policy,
            "devices": self.devices.rows(),
            "served": self.served,
            "indexing": sorted(self.indexing),
            "notes": {str(n): text for n, text in self.notes.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Stream:
        """Read back what `to_dict` gave. Raises StreamError when it is not a stream."""
        try:
            videos = data["videos"]
            expires = data["expires"]
            if (
                data["kind"] not in ("file", "dir")
                or data["mode"] not in MODES
                or not isinstance(videos, list)
                or not videos
                or not all(isinstance(v, str) and v.startswith("/") for v in videos)
                or not (expires is None or isinstance(expires, int | float))
                or data["unlocked_policy"] == gate.LOCKED
            ):
                raise StreamError("the recorded stream is not usable")
            count = len(videos)
            served = data.get("served", videos)  # a stream recorded before 0.9 has none
            indexing = data.get("indexing", [])
            notes = data.get("notes", {})
            if (
                not isinstance(served, list)
                or len(served) != count
                or not all(isinstance(v, str) and v.startswith("/") for v in served)
                or not all(isinstance(n, int) and 0 < n <= count for n in indexing)
                or not isinstance(notes, dict)
            ):
                raise StreamError("the recorded stream is not usable")
            return cls(
                served=served,
                indexing=set(indexing),
                notes={int(n): str(text) for n, text in notes.items()},
                kind=data["kind"],
                source=str(data["source"]),
                videos=videos,
                alias=check_alias(data["alias"]),
                started=float(data["started"]),
                expires=expires,
                mode=data["mode"],
                policy=check_policy(data["policy"]),
                unlocked_policy=check_policy(data["unlocked_policy"]),
                devices=gate.Devices.from_rows(data.get("devices")),
            )
        except (KeyError, TypeError, ValueError) as exc:  # StreamError is a ValueError
            raise StreamError(f"the recorded stream is not usable: {exc}") from exc

    def describe(self, base_url: str, now: float) -> dict[str, Any]:
        """What `status` and the dashboard show. `base_url` is `http://host:port`."""
        url = f"{base_url}/{self.alias}"
        return {
            "kind": self.kind,
            "source": media.shown(self.source),
            "alias": self.alias,
            "url": url,
            "guessers": self.devices.guessers(),
            "mode": self.mode,
            "policy": self.policy,
            "unlocked_policy": self.unlocked_policy,
            "started": self.started,
            "expires": self.expires,
            "seconds_left": None if self.expires is None else max(0.0, self.expires - now),
            "videos": [
                {
                    "number": n,
                    "name": name,
                    "url": f"{url}/{n}",
                    "preparing": n in self.indexing,
                    "note": self.notes.get(n),
                }
                for n, name in enumerate(self.names(), 1)
            ],
            "devices": [
                {**row, "passes": gate.allows(self.policy, row["allowed"])}
                for row in self.devices.rows()
            ],
        }
