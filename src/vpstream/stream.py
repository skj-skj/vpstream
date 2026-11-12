"""One running stream: what it serves, under which alias, and what each URL under it gives.

Nothing here talks to Caddy or the firewall; `control` does that.
"""

from __future__ import annotations

import dataclasses
import os
import re
import secrets
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vpstream import access, clients, gate, media, player, prepared, tracks
from vpstream.checks import is_number

DIRECT = "direct"  # the files as they are, for players such as VLC
BROWSER = "browser"  # prepared HLS versions and a player page, for TV browsers
MODES = (DIRECT, BROWSER)
DEFAULT_POLICY = {DIRECT: gate.OPEN, BROWSER: gate.APPROVAL}
DEFAULT_TTL_MINUTES = 180.0
MAX_TTL_MINUTES = 7 * 24 * 60.0
PLAYLIST = "playlist.m3u"
HLS = "hls"  # /ALIAS/N/hls/... are the files of a video's prepared versions
ASSETS = "assets"  # /ALIAS/assets/NAME are the player's own files
# Names a stream cannot take: they are, or will be, pages of vpstream itself.
RESERVED = frozenset({"caps", "api", "assets", "static", "player", "hls", "wait", "index"})
_ALIAS = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


class StreamError(ValueError):
    """A stream cannot be started or changed as asked; the message says why."""

    @property
    def details(self) -> dict[str, Any]:
        """What else the answer carries, for a caller that can act on it."""
        return {}


class NeedsConfirm(StreamError):
    """It would take long (a transcode, or longer than the film). Sent again with
    `confirmed`, it goes ahead."""

    CODE = "confirm"

    def __init__(self, message: str, plans: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.plans = plans

    @property
    def details(self) -> dict[str, Any]:
        return {"code": self.CODE, "plans": self.plans}


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

    CODE = "unreadable"

    def __init__(self, path: Path) -> None:
        self.path = str(path)
        self.user = access.service_user()
        super().__init__(
            f"the vpstream service (user {self.user}) cannot read {path}. `vpstream start`, run "
            "by the owner of the files, gives it access by itself; anyone else can with: "
            f"sudo setfacl -m u:{self.user}:rX {path}"
        )

    @property
    def details(self) -> dict[str, Any]:
        return {"code": self.CODE, "path": self.path, "user": self.user}


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


def source_of(options: dict[str, Any]) -> tuple[str, str]:
    """("file", path) or ("dir", path) from a request that names exactly one of them."""
    file, folder = options.get("file"), options.get("dir")
    if bool(file) == bool(folder):
        raise StreamError("give exactly one of a file or a folder to stream")
    source = file or folder
    if not isinstance(source, str):
        raise StreamError(f"not a path: {source!r}")
    return ("file" if file else "dir"), source


def check_choice(name: str, value: Any, many: bool = False) -> str | None:
    """A track choice as given, after checking it reads: one track for `--audio` and the
    `--sub` of start, several (`many`) for the `--sub` of prepare."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise StreamError(f"{name}: not a track choice: {value!r}")
    try:
        tracks.parse_choices(value) if many else tracks.parse_choice(value)
    except tracks.SelectionError as exc:
        raise StreamError(f"{name}: {exc}") from exc
    return value


def videos_of(kind: str, path: Path) -> list[str]:
    """The videos a file or folder stream would serve, as full paths with links followed.
    Raises StreamError (Unreadable when the daemon's user cannot read them)."""
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
    if not is_number(value) or not value >= 0:
        raise StreamError(f"the time limit must be a number of minutes, 0 or more: {value!r}")
    if value > MAX_TTL_MINUTES:
        raise StreamError(f"the time limit can be at most {MAX_TTL_MINUTES:g} minutes (7 days)")
    return value * 60 or None


@dataclass
class Answer:
    """What one request to the stream port gets."""

    status: int
    # "video" or "file" (Caddy sends it), "index", "playlist", "player", "asset", "redirect",
    # "client" (the answer to a player's report), "preparing", "notready", "waiting",
    # "denied" or "missing"
    kind: str
    file: str = ""  # a video or a prepared file: the full path of the file to send
    number: int = 0  # the video's number
    location: str = ""  # for a redirect: where to
    asset: str = ""  # for an asset: its name in player.ASSETS
    alias: str = ""  # for an index or a playlist: what it lists
    names: list[str] = field(default_factory=list)
    ready: list[bool] = field(default_factory=list)  # for an index: which videos play
    rel: str = ""  # a prepared file, as Caddy finds it under the video's number
    data: dict[str, Any] = field(default_factory=dict)  # the player's settings; the code
    cookie: str = ""  # a new approval cookie for the browser (see `gate`)
    saves: str = ""  # it changed what the state file holds: SAVE_NOW or SAVE_LATER


# How soon what a request changed must be written down. A new cookie, or a start position
# that was used, would be wrong after a crash; where a player is can wait a little.
SAVE_NOW, SAVE_LATER = "now", "later"
MISSING = Answer(404, "missing")
DENIED = Answer(403, "denied")
NUMBER = re.compile(r"[1-9][0-9]{0,5}")


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
    # Browser mode: each video's folder of prepared versions, and what players start with.
    entries: list[str] = field(default_factory=list)
    audio: str | None = None
    sub: str | None = None

    def __post_init__(self) -> None:
        if len(self.served) != len(self.videos):
            self.served = list(self.videos)

    @classmethod
    def create(cls, options: dict[str, Any], now: float) -> Stream:
        """A new stream from what `start` was given. Raises StreamError for anything wrong."""
        known = {"file", "dir", "mode", "alias", "ttl", "policy", "audio", "sub", "confirmed"}
        if extra := sorted(set(options) - known):
            raise StreamError(f"unknown option: {', '.join(extra)}")
        kind, source = source_of(options)
        mode = options.get("mode") or DIRECT
        if mode not in MODES:
            raise StreamError(f"not a mode: {mode!r}. Use one of: {', '.join(MODES)}")
        policy = check_policy(options.get("policy") or DEFAULT_POLICY[mode])
        alias = check_alias(options["alias"]) if options.get("alias") else random_alias()
        ttl = _ttl_seconds(options.get("ttl"))
        audio = check_choice("audio", options.get("audio"))
        sub = check_choice("sub", options.get("sub"))
        if mode == DIRECT and (audio or sub):
            raise StreamError("audio and subtitle choices are for browser mode (--mode browser)")
        stream = cls(
            kind=kind,
            source=source,
            videos=videos_of(kind, Path(source)),
            alias=alias,
            started=now,
            expires=now + ttl if ttl else None,
            mode=mode,
            audio=audio,
            sub=sub,
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

    def visit(
        self,
        path: str,
        ip: str,
        user_agent: str,
        wants_html: bool,
        now: float,
        cookie: str | None = None,
        query: dict[str, str] | None = None,
    ) -> Answer:
        """Answer one request for `path` (decoded, as the viewer asked for it), with the
        approval cookie the browser sent and the query of a player's report (None for a
        request that is not one; see player.REPORT_HEADER).

        Only a request under the alias counts as a device; anything else on the port, such
        as a scanner trying `/`, is simply not found.
        """
        if self.devices.guesser(ip):
            return MISSING
        answer = self._route(path, ip, user_agent, wants_html, now, cookie, query)
        # A device that gets in may ask for what is not there (a browser's favicon); from
        # anyone else it is a guess at the alias.
        if answer.kind == "missing" and not self.devices.passes(self.policy, ip, cookie):
            self.devices.miss(ip)
        return answer

    def _route(
        self,
        path: str,
        ip: str,
        user_agent: str,
        wants_html: bool,
        now: float,
        cookie: str | None,
        query: dict[str, str] | None,
    ) -> Answer:
        parts = [part for part in path.split("/") if part]
        if not parts or parts[0].lower() != self.alias:
            return MISSING
        browser = self.mode == BROWSER
        # Only browser mode has pages a browser stays on, so only there is a cookie given.
        seen = self.devices.admit(self.policy, ip, user_agent, now, cookie, browser and wants_html)
        if seen.ok:
            answer = self._admitted(parts, seen.device, wants_html, query)
        else:
            answer = self._refused(seen.device, wants_html)
        if seen.cookie:
            # A copy: MISSING and DENIED are shared.
            answer = dataclasses.replace(answer, cookie=seen.cookie, saves=SAVE_NOW)
        return answer

    def _refused(self, device: gate.Device | None, wants_html: bool) -> Answer:
        """A browser that waits for the owner sees its code; anything else is refused."""
        waits = self.policy == gate.APPROVAL and device is not None and device.allowed is None
        if wants_html and waits and device is not None and device.code:
            return Answer(403, "waiting", data={"code": device.code})
        return DENIED

    def _admitted(
        self,
        parts: list[str],
        device: gate.Device | None,
        wants_html: bool,
        query: dict[str, str] | None,
    ) -> Answer:
        browser = self.mode == BROWSER
        if len(parts) == 1:
            if self.kind == "file":
                return self._play(1, wants_html, device) if browser else self._video(1)
            return self._listing("index" if wants_html else "playlist")
        head = parts[1].lower()
        if len(parts) == 2 and head == PLAYLIST and self.kind == "dir":
            return self._listing("playlist")
        if browser and len(parts) == 3 and head == ASSETS:
            return Answer(200, "asset", asset=parts[2]) if parts[2] in player.ASSETS else MISSING
        if not NUMBER.fullmatch(parts[1]) or not 1 <= int(parts[1]) <= len(self.videos):
            return MISSING
        number = int(parts[1])
        if len(parts) == 2:
            if not browser:
                return self._video(number)
            return self._play(number, wants_html, device)
        if browser and len(parts) > 3 and parts[2] == HLS:
            return self._prepared_file(number, "/".join(parts[3:]))
        if browser and len(parts) == 3 and parts[2] == player.REPORT:
            return self._report(number, device, query)
        return MISSING

    def ready(self, number: int) -> bool:
        """Whether a video can be played: in browser mode, once its base version is made."""
        if self.mode != BROWSER:
            return number not in self.indexing
        return prepared.base_ready(Path(self.entries[number - 1]))

    def _listing(self, kind: str) -> Answer:
        ready = [self.ready(n) for n in range(1, len(self.videos) + 1)]
        return Answer(200, kind, alias=self.alias, names=self.names(), ready=ready)

    def _video(self, number: int) -> Answer:
        if number in self.indexing:
            return Answer(503, "preparing", number=number)
        return Answer(200, "video", self.served[number - 1], number)

    def master_url(self, number: int) -> str:
        return f"/{self.alias}/{number}/{HLS}/{prepared.BASE}/{player.MASTER}"

    def _play(self, number: int, wants_html: bool, device: gate.Device | None) -> Answer:
        """A browser gets the player page, any other player the HLS master playlist. The
        page starts where this browser left off, or where the owner said it should, with
        the tracks it chose last."""
        if not self.ready(number):
            return Answer(503, "notready", number=number)
        if not wants_html:
            return Answer(302, "redirect", number=number, location=self.master_url(number))
        client = device.client if device is not None and device.kind == gate.BROWSER else None
        settings = player.settings(
            Path(self.entries[number - 1]),
            alias=self.alias,
            number=number,
            names=self.names(),
            folder=self.kind == "dir",
            audio=self.audio,
            sub=self.sub,
            client=client,
        )
        used = client is not None and client.start_at is not None and client.start_at[0] == number
        if client is not None:
            client.start_position(number)  # a start position set by the owner is used once
        saves = SAVE_NOW if used else ""
        return Answer(200, "player", number=number, alias=self.alias, data=settings, saves=saves)

    def _report(
        self, number: int, device: gate.Device | None, query: dict[str, str] | None
    ) -> Answer:
        """A player says where it is; the answer is what waits for it. Only a browser
        known by its cookie has a record, so one browser never sees another's commands."""
        if device is None or device.kind != gate.BROWSER or query is None:
            return MISSING
        if device.client is None:
            device.client = clients.Client()
        commands = device.client.report(number, query, device.last_seen)
        return Answer(200, "client", number=number, data={"commands": commands}, saves=SAVE_LATER)

    def _prepared_file(self, number: int, rel: str) -> Answer:
        found = prepared.served_file(Path(self.entries[number - 1]), rel)
        if found is None:
            return MISSING
        return Answer(200, "file", str(found), number, rel=f"{number}/{rel}")

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
            "devices": self.devices.rows(private=True),
            "served": self.served,
            "indexing": sorted(self.indexing),
            "notes": {str(n): text for n, text in self.notes.items()},
            "entries": self.entries,
            "audio": self.audio,
            "sub": self.sub,
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
            entries = data.get("entries", [])
            if (
                not isinstance(served, list)
                or len(served) != count
                or not all(isinstance(v, str) and v.startswith("/") for v in served)
                or not all(isinstance(n, int) and 0 < n <= count for n in indexing)
                or not isinstance(notes, dict)
                or not isinstance(entries, list)
                or not all(isinstance(e, str) and e.startswith("/") for e in entries)
                or len(entries) != (count if data["mode"] == BROWSER else 0)
            ):
                raise StreamError("the recorded stream is not usable")
            return cls(
                entries=entries,
                audio=check_choice("audio", data.get("audio")),
                sub=check_choice("sub", data.get("sub")),
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
                devices=gate.Devices.from_rows(data.get("devices"), count),
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
            "audio": self.audio,
            "sub": self.sub,
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
                    "ready": self.ready(n),
                    "preparing": n in self.indexing,
                    "note": self.notes.get(n),
                }
                for n, name in enumerate(self.names(), 1)
            ],
            "devices": [
                {**row, "passes": gate.allows(self.policy, row["allowed"])}
                for row in self.devices.rows(now=now)
            ],
        }
