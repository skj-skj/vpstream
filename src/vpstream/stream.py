"""One running stream: what it serves, under which alias, and what each URL under it gives.

Nothing here talks to Caddy or the firewall; `control` does that.
"""

from __future__ import annotations

import dataclasses
import os
import re
import secrets
import string
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vpstream import clients, gate, media, platform, player, prepared, together, tracks
from vpstream.certificate import LONGEST_STREAM_SECONDS
from vpstream.checks import is_number
from vpstream.together import Room

DIRECT = "direct"  # the files as they are, for players such as VLC
BROWSER = "browser"  # prepared HLS versions and a player page, for TV browsers
MODES = (DIRECT, BROWSER)
DEFAULT_POLICY = {DIRECT: gate.OPEN, BROWSER: gate.APPROVAL}
DEFAULT_TTL_MINUTES = 180.0
MAX_TTL_MINUTES = 7 * 24 * 60.0
# An HTTPS stream ends before its certificate does, and nothing changes while it runs: it is
# never longer than a new certificate lasts (see `certificate`).
MAX_HTTPS_TTL_MINUTES = LONGEST_STREAM_SECONDS / 60
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

    def __init__(self, path: Path | str, prepared: bool = False) -> None:
        self.path = str(path)
        self.prepared = prepared
        hint = (
            "It is prepared for browser mode, which plays what was prepared and needs no "
            "access: start it in browser mode. Or: "
            if prepared
            else ""
        )
        if platform.is_windows():
            self.user = os.environ.get("USERNAME", "")
            super().__init__(
                f"vpstream cannot read {path}. {hint}A drive letter of a network drive is not "
                "seen by the vpstream window (it runs as administrator): use its \\\\server\\share "
                "path instead"
            )
            return
        # Linux only: who the daemon runs as (pwd), and how to grant it access.
        from vpstream.platform.linux import access

        self.user = access.service_user()
        if platform.is_termux():
            super().__init__(
                f"Termux cannot read {path}. {hint}Allow Termux to read the phone's storage "
                "with: termux-setup-storage"
            )
            return
        super().__init__(
            f"the vpstream service (user {self.user}) cannot read {path}. {hint}`vpstream "
            "start`, run by the owner of the files, gives it access by itself, or the owner "
            f"can run: {access.setfacl_command(self.user, 'rX', path)} (anyone else: with sudo)"
        )

    @property
    def details(self) -> dict[str, Any]:
        return {"code": self.CODE, "path": self.path, "user": self.user, "prepared": self.prepared}


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


def check_ui(value: Any) -> str:
    """The player's look: new or old (see player.UIS)."""
    if value in player.UIS:
        return str(value)
    raise StreamError(f"not a player look: {value!r}. Use {' or '.join(player.UIS)}")


def _viewer_name(value: Any) -> str:
    """The name a viewer gave themselves on the player (n= in a report), or "" for none."""
    return clients.name_value(value) if isinstance(value, str) else ""


def _call_id(value: Any) -> str:
    """The call a page meant to join (`call` of its start), or "" for any."""
    return value if isinstance(value, str) and len(value) <= 20 else ""


def _always(video: str) -> bool:
    return True


def _never(video: str) -> bool:
    return False


def videos_of(kind: str, path: Path, needs_read: Callable[[str], bool] = _always) -> list[str]:
    """The videos a file or folder stream would serve, as full paths with links followed.
    Raises StreamError (Unreadable when the daemon's user cannot read one of them that
    `needs_read`)."""
    if not path.is_absolute():
        start = ", starting with /" if os.sep == "/" else ""
        raise StreamError(f"give the full path{start}: {path}")
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
        if needs_read(str(video)) and not os.access(video, os.R_OK):
            raise Unreadable(video)
    return [str(video) for video in found]


def _full_paths(values: list[Any]) -> bool:
    return all(isinstance(v, str) and os.path.isabs(v) for v in values)


def _ttl_seconds(value: Any, https: bool = False) -> float | None:
    if value is None:
        value = DEFAULT_TTL_MINUTES
    if not is_number(value) or not value >= 0:
        raise StreamError(f"the time limit must be a number of minutes, 0 or more: {value!r}")
    if value > MAX_TTL_MINUTES:
        raise StreamError(f"the time limit can be at most {MAX_TTL_MINUTES:g} minutes (7 days)")
    if https and not 0 < value <= MAX_HTTPS_TTL_MINUTES:
        hours = MAX_HTTPS_TTL_MINUTES / 60
        raise StreamError(
            f"an HTTPS stream needs a time limit of at most {MAX_HTTPS_TTL_MINUTES:g} minutes "
            f"({hours:g} hours, the life of its certificate); give a shorter --ttl, or start "
            "it on plain HTTP with --http"
        )
    return value * 60 or None


@dataclass
class Answer:
    """What one request to the stream port gets."""

    status: int
    # "video" or "file" (Caddy sends it), "index", "playlist", "player", "asset", "redirect",
    # "client" (the answer to a player's report), "preparing", "notready", "waiting",
    # "busy" (a page sent too much), "denied" or "missing"
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
_ACTION = re.compile(r"[a-z0-9-]{1,32}")  # the id a player gives a press


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
    https: bool = False  # served over HTTPS, with the host's certificate
    # Watch together (browser mode): the group every player follows; None when each plays
    # on its own.
    together: Room | None = None
    ui: str = player.NEW_UI  # browser mode: the player's look (see player.UIS)

    def __post_init__(self) -> None:
        if len(self.served) != len(self.videos):
            self.served = list(self.videos)

    @classmethod
    def create(
        cls,
        options: dict[str, Any],
        now: float,
        has_base: Callable[[str], bool] = _never,
        https: bool = False,
    ) -> Stream:
        """A new stream from what `start` was given. Raises StreamError for anything wrong.
        `has_base` tells whether a video's base version is prepared: a browser stream plays
        only what was prepared, so the service need not read such a video itself. `https`
        says the host serves streams over HTTPS; the option `http` starts this one on plain
        HTTP all the same."""
        known = {
            "file",
            "dir",
            "mode",
            "alias",
            "ttl",
            "policy",
            "audio",
            "sub",
            "confirmed",
            "http",
            "together",
            "ui",
        }
        if extra := sorted(set(options) - known):
            raise StreamError(f"unknown option: {', '.join(extra)}")
        kind, source = source_of(options)
        mode = options.get("mode") or DIRECT
        if mode not in MODES:
            raise StreamError(f"not a mode: {mode!r}. Use one of: {', '.join(MODES)}")
        policy = check_policy(options.get("policy") or DEFAULT_POLICY[mode])
        alias = check_alias(options["alias"]) if options.get("alias") else random_alias()
        plain = options.get("http", False)
        if not isinstance(plain, bool):
            raise StreamError(f"http must be true or false, not {plain!r}")
        https = https and not plain
        group = options.get("together", False)
        if not isinstance(group, bool):
            raise StreamError(f"together must be true or false, not {group!r}")
        if group and mode != BROWSER:
            raise StreamError("watch together is for browser mode (--mode browser)")
        if group and policy != gate.APPROVAL:
            raise StreamError(
                "watch together lets in only the devices you approve: leave out the policy, "
                "or give approval"
            )
        ui = check_ui(options.get("ui") or player.NEW_UI)
        if options.get("ui") and mode != BROWSER:
            raise StreamError("the player's look is for browser mode (--mode browser)")
        ttl = _ttl_seconds(options.get("ttl"), https)
        audio = check_choice("audio", options.get("audio"))
        sub = check_choice("sub", options.get("sub"))
        if mode == DIRECT and (audio or sub):
            raise StreamError("audio and subtitle choices are for browser mode (--mode browser)")
        try:
            videos = videos_of(
                kind, Path(source), (lambda v: not has_base(v)) if mode == BROWSER else _always
            )
        except Unreadable as exc:
            if mode == DIRECT and not exc.prepared and has_base(exc.path):
                raise Unreadable(exc.path, prepared=True) from None
            raise
        stream = cls(
            kind=kind,
            source=source,
            videos=videos,
            alias=alias,
            started=now,
            expires=now + ttl if ttl else None,
            mode=mode,
            audio=audio,
            sub=sub,
            https=https,
            together=Room(videos=len(videos)) if group else None,
            ui=ui,
        )
        stream.set_policy(policy)
        return stream

    # --- changes

    def set_ui(self, ui: Any, now: float) -> None:
        """Switch the player's look. Open pages see it in the answer to their next report,
        and load again with it; watching together, they come back with no cooldown."""
        if self.mode != BROWSER:
            raise StreamError("the player's look is for browser mode")
        new = check_ui(ui)
        if new != self.ui and self.together is not None:
            self.together.reload(now)
        self.ui = new

    def set_policy(self, policy: str) -> None:
        """Switch the policy. A lock keeps the devices that get in right now."""
        policy = check_policy(policy)
        if self.together is not None and policy == gate.OPEN:
            raise StreamError(
                "watch together lets in only the devices you approve; it cannot be open"
            )
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
        body: dict[str, Any] | None = None,
    ) -> Answer:
        """Answer one request for `path` (decoded, as the viewer asked for it), with the
        approval cookie the browser sent and the query of a player's report (None for a
        request that is not one; see player.REPORT_HEADER). `body` is what a page posted to
        its call (see player.CALL), None for any other request.

        Only a request under the alias counts as a device; anything else on the port, such
        as a scanner trying `/`, is simply not found.
        """
        if self.devices.guesser(ip):
            return MISSING
        answer = self._route(path, ip, user_agent, wants_html, now, cookie, query, body)
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
        body: dict[str, Any] | None = None,
    ) -> Answer:
        parts = [part for part in path.split("/") if part]
        if not parts or parts[0].lower() != self.alias:
            return MISSING
        browser = self.mode == BROWSER
        # Only browser mode has pages a browser stays on, so only there is a cookie given.
        seen = self.devices.admit(self.policy, ip, user_agent, now, cookie, browser and wants_html)
        if seen.ok:
            answer = self._admitted(parts, seen.device, wants_html, query, now, body)
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

    def _number(self, text: str) -> int:
        """The video that `text` names, from 1; 0 for none of this stream's."""
        ok = NUMBER.fullmatch(text) and 1 <= int(text) <= len(self.videos)
        return int(text) if ok else 0

    def _admitted(
        self,
        parts: list[str],
        device: gate.Device | None,
        wants_html: bool,
        query: dict[str, str] | None,
        now: float,
        body: dict[str, Any] | None = None,
    ) -> Answer:
        browser = self.mode == BROWSER
        if body is not None:  # only a page's call is posted to
            good = len(parts) == 3 and parts[2] == player.CALL and self._number(parts[1])
            return self._call(device, body, now) if browser and good else MISSING
        if len(parts) == 1:
            if self.kind == "file":
                return self._play(1, wants_html, device, now) if browser else self._video(1)
            if self.together is not None and wants_html:  # the group's video, not a choice
                return self._play(self.together.video, wants_html, device, now)
            return self._listing("index" if wants_html else "playlist")
        head = parts[1].lower()
        if len(parts) == 2 and head == PLAYLIST and self.kind == "dir":
            return self._listing("playlist")
        if browser and len(parts) == 3 and head == ASSETS:
            return Answer(200, "asset", asset=parts[2]) if parts[2] in player.ASSETS else MISSING
        number = self._number(parts[1])
        if not number:
            return MISSING
        if len(parts) == 2:
            if not browser:
                return self._video(number)
            return self._play(number, wants_html, device, now)
        if browser and len(parts) > 3 and parts[2] == HLS:
            return self._prepared_file(number, "/".join(parts[3:]))
        if browser and len(parts) == 3 and parts[2] == player.REPORT:
            return self._report(number, device, query, now)
        if browser and len(parts) == 3 and parts[2] == player.SYNC:
            return self._sync(number, device, query, now)
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

    def _play(
        self, number: int, wants_html: bool, device: gate.Device | None, now: float
    ) -> Answer:
        """A browser gets the player page, any other player the HLS master playlist. The
        page starts where this browser left off, or where the owner said it should, with
        the tracks it chose last. Watching together, it starts where the group is, with
        the group's tracks, and a page asked for another video gets the group's."""
        room = self.together
        if room is not None and wants_html:
            room.tick(now)
            if number != room.video:
                location = f"/{self.alias}/{room.video}"
                return Answer(302, "redirect", number=room.video, location=location)
        if not self.ready(number):
            return Answer(503, "notready", number=number)
        if not wants_html:
            return Answer(302, "redirect", number=number, location=self.master_url(number))
        own = device.client if device is not None and device.kind == gate.BROWSER else None
        # Watching together, the page follows the group, not what this browser chose; its
        # subtitle size stays its own.
        client = own if room is None else None
        settings = player.settings(
            Path(self.entries[number - 1]),
            alias=self.alias,
            number=number,
            names=self.names(),
            folder=self.kind == "dir",
            audio=self.audio,
            sub=self.sub,
            client=client,
            group=None if room is None else room.state(now),
            own=own,
            ui=self.ui,
            call=self.https,
        )
        used = client is not None and client.start_at is not None and client.start_at[0] == number
        if client is not None:
            client.start_position(number)  # a start position set by the owner is used once
        saves = SAVE_NOW if used else ""
        return Answer(200, "player", number=number, alias=self.alias, data=settings, saves=saves)

    def _report(
        self, number: int, device: gate.Device | None, query: dict[str, str] | None, now: float
    ) -> Answer:
        """A player says where it is; the answer is what waits for it. Only a browser
        known by its cookie has a record, so one browser never sees another's commands.
        Watching together, the answer is the group's state, and a report may carry a
        press of the viewer's (`act`) for the group."""
        if device is None or device.kind != gate.BROWSER or query is None:
            return MISSING
        if device.client is None:
            device.client = clients.Client()
        commands = device.client.report(number, query, now)
        if self.together is None:
            data: dict[str, Any] = {"commands": commands, "ui": self.ui}
            return Answer(200, "client", number=number, data=data, saves=SAVE_LATER)
        # The subtitle size is this browser's own: the only command a page of the group gets.
        mine = [c for c in commands if clients.SUB_SIZE in c]
        room, page = self.together, clients.page_key(query.get("pg"))
        if not page:
            return MISSING
        room.report(
            page,
            device.id,
            now,
            video=number,
            position=clients.read_float(query.get("t")),
            duration=clients.read_float(query.get("d")),
            ready=query.get("in") == "1",
            offset=together.read_offset(query.get("o")),
            name=_viewer_name(query.get("n")),
            rev=clients.read_int(query.get("rev")),
        )
        if room.owns(page, device.id):
            room.call.touch(page, now)
        outcome = self._act(page, device.id, query, now) if query.get("act") else None
        data = {
            **self.group_state(page, device.id, now, clients.read_int(query.get("cm"))),
            "outcome": outcome,
            "commands": mine,
            "ui": self.ui,
        }
        return Answer(200, "client", number=number, data=data, saves=SAVE_LATER)

    def _act(self, page: str, device: str, query: dict[str, str], now: float) -> str:
        """A press of a page for the group: act= one of together.ACTIONS, with its value in
        v and its id in id."""
        action = query.get("id", "")
        action = action if _ACTION.fullmatch(action) else ""
        try:
            return self.press(query.get("act"), query.get("v"), page, device, action, now)
        except together.TogetherError:
            return together.REFUSED

    def press(
        self, action: Any, value: Any, page: str, device: str, action_id: str, now: float
    ) -> str:
        """A press for the group watching together (see together.Room.propose), of a page
        or of the owner (`page` ""): another video only once it is prepared for browsers.
        Raises TogetherError for one that does not read or cannot be done."""
        room = self.together
        if room is None:
            raise together.TogetherError("this stream is not watched together")
        kind, value = together.as_press(action, value)
        value = together.read_value(kind, value, len(self.videos))
        if kind == together.VIDEO and not self.ready(value):
            raise together.TogetherError(f"video {value} is not prepared for browsers yet")
        return room.propose(kind, value, page, device, action_id, now)

    def _sync(
        self, number: int, device: gate.Device | None, query: dict[str, str] | None, now: float
    ) -> Answer:
        """A page waits for the group to change: the answer comes at once when the group's
        rev is not the one the page has (`rev`), or the call has news for it (its version is
        not `cv`, or a message newer than `cm` waits), else `control` holds it ("hold")."""
        room = self.together
        if room is None or device is None or device.kind != gate.BROWSER or query is None:
            return MISSING
        page = clients.page_key(query.get("pg"))
        if not page:
            return MISSING
        room.tick(now)
        if room.owns(page, device.id):
            room.call.touch(page, now)
        rev = clients.read_int(query.get("rev"))
        version, after = clients.read_int(query.get("cv")), clients.read_int(query.get("cm"))
        if (
            rev is None
            or rev != room.rev
            or number != room.video
            or self.call_news(page, device.id, version, after)
        ):
            data = self.group_state(page, device.id, now, after)
            return Answer(200, "client", number=number, data=data)
        data = {"page": page, "rev": rev, "device": device.id, "cv": version, "cm": after}
        return Answer(200, "hold", number=number, data=data)

    def call_news(self, page: str, device: str, version: int | None, after: int | None) -> bool:
        """Whether the call has news for a page that has seen it at `version`, and its
        messages up to `after` (see together.Call.news); messages only for a page of
        `device`'s own. Never on plain HTTP: no calls."""
        room = self.together
        if room is None or not self.https:
            return False
        return room.call.news(page, version, after, mail=room.owns(page, device))

    def _call(self, device: gate.Device | None, body: dict[str, Any], now: float) -> Answer:
        """What a page of the group did in the call (`do`): "start" (Call, or Join, with
        its `mic` and `cam`; `was` its page before it loaded again; `call` the call it meant
        to join, "" for any), "end", "state" (its
        `mic`, `cam`, and the tiles it `sees`), "away" (its tab closed or loads again: see
        Call.away), or "send" a message (`kind`, `data`) to another page in it (`to`).
        Only a page the group knows as this browser's, of an approved browser, on a stream
        over HTTPS: the camera works nowhere else. The answer is the call as this page sees
        it, with its messages newer than `cm`."""
        room = self.together
        if room is None or not self.https or device is None or device.kind != gate.BROWSER:
            return MISSING
        # The page must be this browser's own: the others in the call see its key.
        page = clients.page_key(body.get("pg"))
        if not page or not room.owns(page, device.id):
            return MISSING
        call = room.call
        if not call.allow(page, now):
            return Answer(429, "busy")
        room.tick(now)
        call.touch(page, now)
        what = body.get("do")
        if what == "start":
            was = clients.page_key(body.get("was"))
            outcome = room.start_call(page, now, was, _call_id(body.get("call")))
            if outcome in (together.STARTED, together.JOINED):
                call.set_state(page, body.get("mic"), body.get("cam"), body.get("sees"))
        elif what == "end":
            outcome = call.leave(page)
        elif what == "state":
            ok = call.set_state(page, body.get("mic"), body.get("cam"), body.get("sees"))
            outcome = together.APPLIED if ok else together.NOT_IN
        elif what == "away":
            outcome = together.APPLIED if call.away(page, now) else together.NOT_IN
        elif what == "send":
            ok = call.send(page, body.get("to"), body.get("kind"), body.get("data"), now)
            outcome = together.SENT if ok else together.REFUSED
        else:
            outcome = together.REFUSED
        data = {
            "call": room.call_state(page, now, clients.read_int(body.get("cm"))),
            "outcome": outcome,
        }
        return Answer(200, "client", data=data)

    def group_state(
        self, page: str, device: str, now: float, after: int | None = None
    ) -> dict[str, Any]:
        """What a page of the group is told: the group's state, its own cooldown, and
        whether it watches alone (the group then goes where it is). Over HTTPS, the call
        too, with the page's messages newer than `after`: only to a page of `device`'s own
        (another browser that knows its key gets none, and takes none away)."""
        room = self.together
        assert room is not None
        state = {
            "together": room.state(now),
            "cooldown": round(room.cooldown(page, now), 2),
            "alone": room.alone(page, now),
        }
        if self.https:
            state["call"] = room.call_state(page, now, after, mail=room.owns(page, device))
        return state

    def _prepared_file(self, number: int, rel: str) -> Answer:
        found = prepared.served_file(Path(self.entries[number - 1]), rel)
        if found is None:
            return MISSING
        return Answer(200, "file", str(found), number, rel=f"{number}/{rel}")

    # --- as plain data

    def names(self) -> list[str]:
        return [media.shown(Path(video).name) for video in self.videos]

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        """Everything, for the state file. Watching together, the group is recorded where
        it is at `now`."""
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
            "https": self.https,
            "together": None if self.together is None else self.together.to_dict(now),
            "ui": self.ui,
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
                or not _full_paths(videos)
                or not (expires is None or isinstance(expires, int | float))
                or data["unlocked_policy"] == gate.LOCKED
            ):
                raise StreamError("the recorded stream is not usable")
            count = len(videos)
            # Every key is required: a record from before 0.14 is not taken up again.
            served, indexing, notes = data["served"], data["indexing"], data["notes"]
            entries, https = data["entries"], data["https"]
            if not isinstance(https, bool) or (
                https
                and (expires is None or expires - float(data["started"]) > LONGEST_STREAM_SECONDS)
            ):
                raise StreamError("the recorded stream is not usable")
            if (
                not isinstance(served, list)
                or len(served) != count
                or not _full_paths(served)
                or not all(isinstance(n, int) and 0 < n <= count for n in indexing)
                or not isinstance(notes, dict)
                or not isinstance(entries, list)
                or not _full_paths(entries)
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
                https=https,
                together=(
                    Room.from_dict(data["together"], count)
                    if data.get("together") is not None and data["mode"] == BROWSER
                    else None
                ),
                # A record from before 0.21 has none: the stream keeps the look it had.
                ui=data["ui"] if data.get("ui") in player.UIS else player.OLD_UI,
            )
        except (KeyError, TypeError, ValueError) as exc:  # StreamError is a ValueError
            raise StreamError(f"the recorded stream is not usable: {exc}") from exc

    def describe(self, base_url: str, now: float) -> dict[str, Any]:
        """What `status` and the dashboard show. `base_url` is `http(s)://host:port`."""
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
            "https": self.https,
            "together": None if self.together is None else self.together.describe(now),
            "ui": self.ui if self.mode == BROWSER else None,
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
