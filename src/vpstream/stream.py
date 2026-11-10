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

from vpstream import gate, media

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


def _unreadable(path: Path) -> StreamError:
    return StreamError(
        f"the vpstream service cannot read {path}. It runs as its own user: give that user "
        "access to the file and to every folder above it (for example chmod o+r on the file "
        "and o+x on the folders)"
    )


def _videos(kind: str, path: Path) -> list[str]:
    if not path.is_absolute():
        raise StreamError(f"give the full path, starting with /: {path}")
    if kind == "file":
        if not path.is_file():
            raise StreamError(f"not a file: {path}")
        found = [path]
    else:
        if not path.is_dir():
            raise StreamError(f"not a folder: {path}")
        if not os.access(path, os.R_OK | os.X_OK):
            raise _unreadable(path)
        found = media.scan(path)
        if not found:
            raise StreamError(f"no videos at the top level of {path}")
    for video in found:
        if not os.access(video, os.R_OK):
            raise _unreadable(video)
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
    file: str = ""  # for a video: the file to send
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
        return Answer(200, "video", self.videos[number - 1])

    # --- as plain data

    def names(self) -> list[str]:
        return [Path(video).name for video in self.videos]

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
            return cls(
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
            "source": self.source,
            "alias": self.alias,
            "url": url,
            "mode": self.mode,
            "policy": self.policy,
            "unlocked_policy": self.unlocked_policy,
            "started": self.started,
            "expires": self.expires,
            "seconds_left": None if self.expires is None else max(0.0, self.expires - now),
            "videos": [
                {"number": n, "name": name, "url": f"{url}/{n}"}
                for n, name in enumerate(self.names(), 1)
            ],
            "devices": [
                {**row, "passes": gate.allows(self.policy, row["allowed"])}
                for row in self.devices.rows()
            ],
        }
