"""What the dashboard knows and asks of each browser that plays a stream. No I/O happens here.

A client is a browser known by its approval cookie (see `gate`). Its player reports where it
is about every two seconds and gets back the commands that wait for it. The record holds what
the player last reported: the owner's commands wait in it until the player says it has seen
them, and then show up in what it reports. So the dashboard and the TV's own buttons change
the same record, and it always says what the TV does.

The newer of a button press and a command wins. A report says how long ago the viewer last
pressed each kind of button, so a command made before that press is dropped here; one that
crosses a press on its way back is dropped by the player. The record follows from the next
report.

One browser may have several pages open (tabs). Each says which it is, and a command waits
until every page that reported lately has seen it.
"""

from __future__ import annotations

import contextlib
import math
import re
from dataclasses import dataclass, field
from typing import Any

from vpstream.checks import is_digits, is_finite, is_whole

OFF = "off"
SOFT, BURN = "soft", "burn"  # subtitles the player draws, or a version with them burned in
AUDIO, SUB, SEEK = "audio", "sub", "seek"  # the commands, one of each kind at most
COMMANDS = (AUDIO, SUB, SEEK)
MAX_SECONDS = 7 * 24 * 3600.0  # longer than any film; keeps a forged report in bounds
MAX_NAME = 40
RESUME_MIN = 10.0  # a position this early starts from the beginning
RESUME_END = 30.0  # nor does a video watched to this close to its end resume
ACTIVE_SECONDS = 10.0  # a player reports every 2 s; one quiet this long has gone
PAGE_SECONDS = ACTIVE_SECONDS  # a page that has not reported this long no longer holds commands
MAX_PAGES = 4
PRESSED = {AUDIO: "pa", SUB: "ps", SEEK: "pk"}  # report keys: seconds since that press
_PAGE = re.compile(r"[a-z0-9]{1,16}")
_SUB = re.compile(rf"([1-9][0-9]{{0,3}})-({SOFT}|{BURN})")
_CLOCK = re.compile(r"(?:(\d{1,3}):)?([0-5]?\d):([0-5]\d)(?:\.\d+)?", re.ASCII)


class ClientError(ValueError):
    """A change or a report that does not read; the message says why."""


# --- the values a command or a report carries


def audio_value(value: Any) -> int:
    """An audio choice: a track number, or 0 for Off (the player mutes)."""
    text = str(value).strip().lower() if isinstance(value, str | int) else ""
    if isinstance(value, bool):
        text = ""
    if text == OFF:
        return 0
    if is_digits(text) and int(text) < 10000:
        return int(text)
    raise ClientError(f"not an audio track: {value!r}. Give a track number, or off")


def sub_value(value: Any) -> str:
    """A subtitle choice: `N-soft`, `N-burn` (track N, drawn or burned in) or `off`."""
    text = value.strip().lower() if isinstance(value, str) else ""
    if text == OFF or _SUB.fullmatch(text):
        return text
    raise ClientError(
        f"not a subtitle choice: {value!r}. Give off, or a track number with how it is "
        "shown, such as 2-soft or 3-burn"
    )


def seconds_value(value: Any) -> float:
    """A position: seconds, or a clock time such as 10:10 or 1:02:03."""
    if isinstance(value, bool):
        raise ClientError(f"not a time: {value!r}")
    if isinstance(value, int | float):
        seconds = float(value)
    elif isinstance(value, str) and (m := _CLOCK.fullmatch(value.strip())):
        seconds = int(m[1] or 0) * 3600 + int(m[2]) * 60 + int(m[3])
    else:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            raise ClientError(
                f"not a time: {value!r}. Give seconds, or a time such as 10:10"
            ) from None
    if not math.isfinite(seconds) or not 0 <= seconds <= MAX_SECONDS:
        raise ClientError(f"not a time in a video: {value!r}")
    return seconds


def name_value(value: Any) -> str:
    """A device name as the dashboard shows it: one line, at most MAX_NAME characters."""
    if not isinstance(value, str):
        raise ClientError(f"not a name: {value!r}")
    words = "".join(c if c.isprintable() else " " for c in value).split()
    return " ".join(words)[:MAX_NAME].rstrip()


# --- one client


@dataclass
class Command:
    seq: int
    value: Any
    video: int = 0  # a seek is for the video the client was on when it was asked for
    issued: float = 0.0  # when the owner asked

    def due(self, kind: str, page: Page) -> bool:
        """Whether this page still has to be told. A seek means nothing on another video."""
        if self.seq <= page.ack:
            return False
        return not (kind == SEEK and self.video not in (0, page.video))


@dataclass
class Page:
    """One open page of the player in this browser, as its reports show it."""

    ack: int = 0
    video: int = 0
    seen: float = 0.0


@dataclass
class Client:
    video: int = 0  # the video it plays, by number; 0 before its first report
    position: float = 0.0
    duration: float = 0.0
    paused: bool = True
    audio: int | None = None  # what the player last reported (see audio_value)
    sub: str | None = None  # likewise (see sub_value)
    reported: float = 0.0  # when; 0 for never
    positions: dict[int, float] = field(default_factory=dict)  # video -> where to resume
    start_at: tuple[int, float] | None = None  # (video, seconds): the next load starts there
    seq: int = 0  # the number of the last command
    commands: dict[str, Command] = field(default_factory=dict)  # kind -> what waits
    # Its open pages by their id. Not written down: after a restart, pages say so again.
    pages: dict[str, Page] = field(default_factory=dict, compare=False)

    def command(self, kind: str, value: Any, now: float = 0.0) -> None:
        """Ask the player for a change. A newer command of the same kind replaces one that
        still waits."""
        self.seq += 1
        self.commands[kind] = Command(self.seq, value, self.video, now)

    def active(self, now: float) -> bool:
        """Whether a player is on a page and reporting."""
        return bool(self.reported) and now - self.reported <= ACTIVE_SECONDS

    def set_start(self, video: int, seconds: float) -> None:
        self.start_at = (video, seconds)

    def start_position(self, video: int, consume: bool = True) -> float:
        """Where a load of `video` starts: the one-time start position the owner set for
        it, else a seek that still waits, else where this client left off, else the
        beginning."""
        if self.start_at is not None and self.start_at[0] == video:
            at = self.start_at[1]
            if consume:
                self.start_at = None
            return at
        seek = self.commands.get(SEEK)
        if seek is not None and seek.video in (0, video):
            return float(seek.value)
        return self.positions.get(video, 0.0)

    def wanted(self, kind: str) -> Any:
        """The audio or subtitles a page loaded now starts with: what waits for the player,
        else what it reported last. The page counts as having seen what waits (`seq`)."""
        command = self.commands.get(kind)
        return command.value if command is not None else getattr(self, kind)

    def report(self, video: int, values: dict[str, Any], now: float) -> list[dict[str, Any]]:
        """Take one report of the player on `video` and answer with the commands for it.

        `values` are the query of the report: t (position), d (duration), p (1 when
        paused), a and s (audio and subtitles, as audio_value and sub_value read them), ack
        (the last command the page has seen), pg (which page) and pa, ps and pk (seconds
        since the viewer last pressed audio, subtitles or a seek, after the page's last
        report). Anything that does not read is left as it was: a report is never refused
        for one bad value.
        """
        self.video = video
        self.reported = now
        page = self._page(values.get("pg"), video, _int(values.get("ack")), now)
        for kind, key in PRESSED.items():
            ago = _float(values.get(key))
            command = self.commands.get(kind)
            if ago is not None and command is not None and command.issued <= now - ago:
                del self.commands[kind]  # the viewer chose after the owner asked
        live = list(self.pages.values())
        self.commands = {k: c for k, c in self.commands.items() if any(c.due(k, p) for p in live)}
        duration = _float(values.get("d"))
        if duration is not None:
            self.duration = duration
        position = _float(values.get("t"))
        if position is not None:
            self.position = position
            self._remember(video, position)
        self.paused = values.get("p") != "0"
        for key, read, name in (("a", audio_value, AUDIO), ("s", sub_value, SUB)):
            if key in values:
                with contextlib.suppress(ClientError):
                    setattr(self, name, read(values[key]))
        return [
            {"seq": c.seq, kind: c.value}
            for kind, c in sorted(self.commands.items())
            if c.due(kind, page)
        ]

    def _page(self, key: Any, video: int, ack: int | None, now: float) -> Page:
        """The page that reports, brought up to date; pages that went quiet are forgotten."""
        name = key if isinstance(key, str) and _PAGE.fullmatch(key) else ""
        page = self.pages.pop(name, None) or Page()
        page.ack = max(page.ack, ack or 0)
        page.video, page.seen = video, now
        kept = [(n, p) for n, p in self.pages.items() if now - p.seen <= PAGE_SECONDS]
        kept = sorted(kept, key=lambda item: item[1].seen)[-(MAX_PAGES - 1) :]
        self.pages = {**dict(kept), name: page}
        return page

    def _remember(self, video: int, position: float) -> None:
        near_end = self.duration and position > self.duration - RESUME_END
        if position < RESUME_MIN or near_end:
            self.positions.pop(video, None)
        else:
            self.positions[video] = position

    # --- as plain data

    def to_dict(self) -> dict[str, Any]:
        return {
            "video": self.video,
            "position": self.position,
            "duration": self.duration,
            "paused": self.paused,
            "audio": self.audio,
            "sub": self.sub,
            "reported": self.reported,
            "positions": {str(n): at for n, at in self.positions.items()},
            "start_at": list(self.start_at) if self.start_at else None,
            "seq": self.seq,
            "commands": {
                kind: {"seq": c.seq, "value": c.value, "video": c.video, "issued": c.issued}
                for kind, c in self.commands.items()
            },
        }

    def describe(self, now: float) -> dict[str, Any]:
        """What the dashboard shows: the record, what still waits for the player, and
        whether it plays now."""
        data = self.to_dict()
        data["pending"] = {kind: c.value for kind, c in self.commands.items()}
        data["active"] = self.active(now)
        del data["commands"], data["seq"]
        return data

    @classmethod
    def from_dict(cls, data: Any, videos: int) -> Client | None:
        """Read back what `to_dict` gave, for a stream of `videos` videos. Values that do
        not fit are dropped; something that is not a record at all gives None."""
        if not isinstance(data, dict):
            return None
        client = cls()
        video = _int(data.get("video"))
        client.video = video if video is not None and video <= videos else 0
        for name in ("position", "duration", "reported"):
            value = _float(data.get(name), limit=None if name == "reported" else MAX_SECONDS)
            setattr(client, name, value or 0.0)
        client.paused = data.get("paused") is not False
        try:
            client.audio = None if data.get("audio") is None else audio_value(data["audio"])
        except ClientError:
            client.audio = None
        try:
            client.sub = None if data.get("sub") is None else sub_value(data["sub"])
        except ClientError:
            client.sub = None
        positions = data.get("positions")
        for key, at in positions.items() if isinstance(positions, dict) else []:
            n, seconds = _int(key), _float(at)
            if n is not None and 0 < n <= videos and seconds is not None:
                client.positions[n] = seconds
        start = data.get("start_at")
        if isinstance(start, list) and len(start) == 2:
            n, seconds = _int(start[0]), _float(start[1])
            if n is not None and 0 < n <= videos and seconds is not None:
                client.start_at = (n, seconds)
        client.seq = _int(data.get("seq")) or 0
        commands = data.get("commands")
        for kind, raw in commands.items() if isinstance(commands, dict) else []:
            command = _command(kind, raw, client.seq)
            if command is not None:
                client.commands[kind] = command
        return client


_READ = {AUDIO: audio_value, SUB: sub_value, SEEK: seconds_value}


def _command(kind: Any, raw: Any, last: int) -> Command | None:
    if kind not in _READ or not isinstance(raw, dict):
        return None
    seq, video = _int(raw.get("seq")), _int(raw.get("video"))
    if seq is None or not 0 < seq <= last:
        return None
    try:
        value = _READ[kind](raw.get("value"))
    except ClientError:
        return None
    return Command(seq, value, video or 0, _float(raw.get("issued"), limit=None) or 0.0)


def _int(value: Any) -> int | None:
    """A whole number of 0 or more, from a report (text) or a record (a number)."""
    if isinstance(value, str) and is_digits(value) and len(value) <= 12:
        return int(value)
    if is_whole(value) and 0 <= value < 10**12:
        return value
    return None


def _float(value: Any, limit: float | None = MAX_SECONDS) -> float | None:
    """A number of seconds of 0 or more, from a report (text) or a record (a number)."""
    if isinstance(value, str) and len(value) <= 20:
        try:
            value = float(value)
        except ValueError:
            return None
    if not is_finite(value) or value < 0:
        return None
    return None if limit is not None and value > limit else float(value)
