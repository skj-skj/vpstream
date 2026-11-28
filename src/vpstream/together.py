"""Watch together: browsers that play one video at one place, as a group. No I/O happens
here; the clock is passed in.

The daemon holds the group's state, the room, and every player follows it. Any viewer's
press (play, pause, a seek, audio, subtitles, another video) is a proposal. When no window
is open, it applies to the group at once and opens a window of WINDOW_SECONDS. What is
pressed inside the window is collected by kind, and when the window closes the last of each
kind wins: it applies if it changes anything. Kinds do not conflict, so a pause and a seek
in one window both apply. A step of -10 s builds on the presser's own seek that waits in
the window, so three quick steps move 30 s.

Each open page of the player is known by its id (as in `clients`). A page that joins, late
or again, starts where the group is, and its presses count only COOLDOWN_SECONDS after it
said it is loaded and at that place: someone who just came in cannot pause everyone by
accident. A page that follows the group to another video does not wait again, nor does
one that loads again with the player's other look (`reload`).

Each change says what it was and whose press it was, by page and by the name the viewer
gave themselves on the player, so the other players can say who paused (`changes`).

The position is the room's at `anchor` (a time of the clock); while it plays, it moves on
with the clock. Every change bumps `rev`, which the players wait on (see `control`).

The viewers can also call each other, with video and sound, browser to browser (`Call`,
and call.js on the page). The daemon only keeps who is in the call and passes the few
messages that set up each pair's connection; the video and sound never come here.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

from vpstream import clients
from vpstream.checks import is_finite, is_whole

PLAY, SEEK, AUDIO, SUB, VIDEO = "play", "seek", "audio", "sub", "video"
KINDS = (VIDEO, PLAY, SEEK, AUDIO, SUB)  # the order a window's presses apply in
SKIP = "skip"  # a step of seconds, from the presser's own seek that waits, else the group's
PAUSE = "pause"  # a press of play is PLAY True, of pause PLAY False
# What can be asked of the group, by a viewer's page, the dashboard or the command line.
ACTIONS = (PLAY, PAUSE, SEEK, SKIP, AUDIO, SUB, VIDEO)
WINDOW_SECONDS = 2.0
COOLDOWN_SECONDS = 10.0
SYNC_SECONDS = 2.0  # a page further off the group than this is out of sync
# A page whose browser was on the group's last video this shortly before it changed is
# following it, and does not wait again.
FOLLOW_SECONDS = 20.0
PAGE_SECONDS = 60.0  # a page quiet this long has gone; when it comes back, it joins again
MAX_PAGES = 64
ACTION_SECONDS = 60.0  # how long an action's id is remembered, so a retry is not done twice
MAX_ACTIONS = 256
SAME_SECONDS = 0.5  # a seek this close to where the group is changes nothing
END_SECONDS = 0.25  # a group this close to the end of its video has reached it
MAX_STEP = 3600.0
MAX_DEVICE = 40
APPLIED, QUEUED, COOLDOWN, REFUSED = "applied", "queued", "cooldown", "refused"
OWNER = "owner"  # who changed it, for a change made on the dashboard or the command line

# The call (see Call).
CALL_SIZE = 20  # people in one call at most
CALL_QUIET_SECONDS = 30.0  # a page in the call not heard from this long has left it
MAIL_SIZE = 64  # messages that wait for one page; past it the oldest go
MAIL_SECONDS = 30.0  # a message not taken by then is dropped
MAX_MAIL_DATA = 12000  # characters of one message (an offer with all its addresses)
OFFER, ANSWER, BYE = "offer", "answer", "bye"
MAIL_KINDS = (OFFER, ANSWER, BYE)
POSTS, POST_SECONDS = 40, 10.0  # what a page may send to the call in that many seconds
STARTED, JOINED, FULL, ENDED, LEFT, NOT_IN, SENT = (
    "started", "joined", "full", "ended", "left", "not in the call", "sent"
)  # fmt: skip


class TogetherError(ValueError):
    """An action that does not read; the message says why."""


def as_press(action: Any, value: Any = None) -> tuple[str, Any]:
    """An action (see ACTIONS) as the kind of press it is, and its value: play and pause are
    PLAY, True and False. The value is read by `read_value`."""
    if action in (PLAY, PAUSE):
        return PLAY, action == PLAY
    if action not in ACTIONS:
        raise TogetherError(f"not an action: {action!r}. Use {', '.join(ACTIONS)}")
    return action, value


def read_offset(value: Any) -> float | None:
    """A page's own distance from the group, in seconds (`o` in a report), or None for one
    that does not read."""
    if not isinstance(value, str) or len(value) > 20:
        return None
    try:
        offset = float(value)
    except ValueError:
        return None
    return offset if is_finite(offset) and abs(offset) <= clients.MAX_SECONDS else None


def read_value(kind: str, value: Any, videos: int) -> Any:
    """An action's value in its one form: True to play and False to pause, seconds, an audio
    track (0 for Off), a subtitle choice or a video's number."""
    try:
        if kind == PLAY:
            if isinstance(value, bool):
                return value
            raise TogetherError(f"not play or pause: {value!r}")
        if kind == SEEK:
            return clients.seconds_value(value)
        if kind == SKIP:
            step = float(value) if isinstance(value, int | float | str) else None
            if isinstance(value, bool) or step is None or not is_finite(step):
                raise TogetherError(f"not a step in seconds: {value!r}")
            return max(-MAX_STEP, min(MAX_STEP, step))
        if kind == AUDIO:
            return clients.audio_value(value)
        if kind == SUB:
            return clients.sub_value(value)
        if kind == VIDEO:
            number = int(value) if isinstance(value, str) and value.isdecimal() else value
            if is_whole(number) and 0 < number <= videos:
                return number
            raise TogetherError(f"not a video of this stream: {value!r}")
    except ValueError as exc:  # ClientError, and a step that is not a number
        raise TogetherError(str(exc)) from None
    raise TogetherError(f"not an action: {kind!r}")


@dataclass
class Proposal:
    value: Any
    page: str  # "" for the owner
    device: str
    video: int  # a seek is for the video the group was on when it was pressed
    name: str = ""  # the presser's own name, as their page last said it


@dataclass
class Page:
    """One open page of the player in the group."""

    device: str  # its browser, by device id
    seen: float
    video: int = 0  # the video it is on, as it reported
    joined: float | None = None  # when it said it was loaded and at the group's place
    follows: bool = False  # it came with the group to another video, or loaded again: no cooldown
    drift: float | None = None  # how far it is off the group, at its last report
    name: str = ""  # the name the viewer gave themselves on the player; "" for none


@dataclass
class Member:
    """One page in the call, and what its viewer has turned on."""

    joined: float
    seen: float
    mic: bool = False  # a page joins with its mic off and its camera on
    cam: bool = True
    # The pages whose tiles it shows now: only they get its video (see call.js).
    sees: list[str] = field(default_factory=list)


@dataclass
class Mail:
    """One message that sets up the connection of two pages; what it holds is the
    browsers' own, and is never read here."""

    seq: int
    sender: str
    kind: str
    data: str
    at: float


@dataclass
class Call:
    """The call of the group's viewers. Someone presses Call: a call starts, and every
    other page of the group is rung. Anyone joins and leaves when they like, also one who
    left before. With 2 in the call, End ends it for both; with more, only for the one who
    pressed it. A page not heard from for CALL_QUIET_SECONDS leaves the same way.

    Each pair of pages in the call connects browser to browser; the messages that set that
    up go through here, into the other page's mailbox, and come out with the page's next
    answer. A page says which messages it has (`after`), so one lost on the way comes
    again. A mailbox keeps MAIL_SIZE messages, each for MAIL_SECONDS at most, and a page
    sends POSTS in POST_SECONDS at most.

    Every change that the pages show (who is in, their mic and camera) bumps `version`;
    a new message does not."""

    id: str = ""  # the call that runs, "" for none; every call has an id of its own
    calls: int = 0  # how many calls there were: the next one's id
    version: int = 1
    starter: str = ""  # the page that started it
    members: dict[str, Member] = field(default_factory=dict)  # in the order they joined
    left: set[str] = field(default_factory=set)  # pages that left this call: not rung again
    mail: dict[str, list[Mail]] = field(default_factory=dict)  # page -> its mailbox
    seq: int = 0  # the newest message's number
    posts: dict[str, list[float]] = field(default_factory=dict)  # page -> when it sent

    @property
    def running(self) -> bool:
        return bool(self.id)

    def _changed(self) -> None:
        self.version += 1

    def start(self, page: str, now: float) -> str:
        """Call pressed on `page`: a new call, which rings everyone else, or the one that
        runs, joined. STARTED, JOINED or FULL."""
        if not self.running:
            self.calls += 1
            self.id, self.starter = str(self.calls), page
            self.members, self.left, self.mail = {page: Member(now, now)}, set(), {}
            self._changed()
            return STARTED
        if page not in self.members:
            if len(self.members) >= CALL_SIZE:
                return FULL
            self.members[page] = Member(now, now)
            self.left.discard(page)
            self._changed()
        return JOINED

    def leave(self, page: str) -> str:
        """End pressed on `page`. With 2 in the call (or 1) it ends for both (ENDED);
        with more, this page leaves it (LEFT)."""
        if page not in self.members:
            return NOT_IN
        if len(self.members) <= 2:
            self.end()
            return ENDED
        del self.members[page]
        self.mail.pop(page, None)
        self.left.add(page)
        self._changed()
        return LEFT

    def end(self) -> None:
        if self.running:
            self.id, self.starter = "", ""
            self.members, self.left, self.mail = {}, set(), {}
            self._changed()

    def touch(self, page: str, now: float) -> None:
        """The page was heard from."""
        member = self.members.get(page)
        if member is not None:
            member.seen = max(member.seen, now)

    def set_state(self, page: str, mic: Any, cam: Any, sees: Any) -> bool:
        """What a page in the call has on: its mic, its camera, and the tiles it shows.
        A value that does not read is left as it was. False for a page not in the call."""
        member = self.members.get(page)
        if member is None:
            return False
        before = (member.mic, member.cam, member.sees)
        if isinstance(mic, bool):
            member.mic = mic
        if isinstance(cam, bool):
            member.cam = cam
        if isinstance(sees, list):
            shown = [p for p in sees if isinstance(p, str) and p in self.members and p != page]
            member.sees = list(dict.fromkeys(shown))[:CALL_SIZE]
        if (member.mic, member.cam, member.sees) != before:
            self._changed()
        return True

    def allow(self, page: str, now: float) -> bool:
        """Whether `page` may send once more now (POSTS in POST_SECONDS); counts it."""
        sent = [at for at in self.posts.get(page, []) if now - at < POST_SECONDS]
        allowed = len(sent) < POSTS
        if allowed:
            sent.append(now)
        self.posts[page] = sent
        if len(self.posts) > MAX_PAGES:  # pages that went
            for key in [k for k, v in self.posts.items() if not v or now - v[-1] >= POST_SECONDS]:
                del self.posts[key]
        return allowed

    def send(self, page: str, to: str, kind: Any, data: Any, now: float) -> bool:
        """A message from `page` to `to`, both in the call: SENT, or False for one that
        does not read."""
        ok = page in self.members and to in self.members and to != page
        if not ok or kind not in MAIL_KINDS:
            return False
        if not isinstance(data, str) or len(data) > MAX_MAIL_DATA:
            return False
        self.seq += 1
        box = self.mail.setdefault(to, [])
        box.append(Mail(self.seq, page, kind, data, now))
        del box[:-MAIL_SIZE]
        return True

    def take(self, page: str, after: int | None, now: float) -> list[dict[str, Any]]:
        """The messages for `page` newer than `after`, the newest it has; those up to it
        are gone from its mailbox."""
        box = [m for m in self.mail.get(page, []) if m.seq > (after or 0)]
        if page in self.mail:
            self.mail[page] = box
        return [
            {"seq": m.seq, "from": m.sender, "kind": m.kind, "data": m.data}
            for m in box
            if now - m.at <= MAIL_SECONDS
        ]

    def news(self, page: str, version: int | None, after: int | None) -> bool:
        """Whether the page has not seen the call as it is, or has a message waiting."""
        if version != self.version:
            return True
        return any(m.seq > (after or 0) for m in self.mail.get(page, []))

    def tick(self, now: float) -> bool:
        """Pages not heard from leave, and old messages go. True when the call changed."""
        version = self.version
        for page, member in list(self.members.items()):
            if now - member.seen > CALL_QUIET_SECONDS and page in self.members:
                self.leave(page)
        for page, box in list(self.mail.items()):
            self.mail[page] = [m for m in box if now - m.at <= MAIL_SECONDS]
            if not self.mail[page]:
                del self.mail[page]
        return self.version != version

    def rings(self, page: str) -> bool:
        """Whether `page` is asked to join: every page that is not in the call and has not
        left it, also one that came after it started."""
        return self.running and page not in self.members and page not in self.left


@dataclass
class Room:
    videos: int = 1  # how many the stream has, for a change of video
    video: int = 1
    position: float = 0.0  # at `anchor`
    anchor: float = 0.0
    paused: bool = True
    audio: int | None = None  # None until someone chooses: each page starts with the default
    sub: str | None = None
    rev: int = 1
    changed_by: str = ""  # device id, OWNER, or "" for the group reaching the end
    changed_at: float = 0.0
    # What the last change did, each kind with whose press it was (see `state`).
    changes: list[dict[str, Any]] = field(default_factory=list)
    video_changed: float = 0.0
    reloaded: float = 0.0  # when every page was told to load again (see `reload`)
    window_until: float | None = None  # while a window is open: when it closes
    pending: dict[str, Proposal] = field(default_factory=dict)
    pages: dict[str, Page] = field(default_factory=dict)
    durations: dict[int, float] = field(default_factory=dict)  # as the players report them
    actions: dict[str, tuple[float, str]] = field(default_factory=dict)  # id -> (when, outcome)
    call: Call = field(default_factory=Call)

    # --- where the group is

    @property
    def duration(self) -> float:
        return self.durations.get(self.video, 0.0)

    def live(self, now: float) -> float:
        """The group's position at `now`."""
        at = self.position if self.paused else self.position + max(0.0, now - self.anchor)
        return min(at, self.duration) if self.duration else at

    def _set(self, now: float, position: float | None = None, paused: bool | None = None) -> None:
        """Move the anchor to `now`, then change the position or the paused state."""
        self.position, self.anchor = self.live(now), now
        if position is not None:
            end = self.duration
            self.position = max(0.0, min(position, end)) if end else max(0.0, position)
        if paused is not None:
            self.paused = paused

    def _changed(
        self, device: str, now: float, changes: list[dict[str, Any]] | None = None
    ) -> None:
        self.rev += 1
        self.changed_by, self.changed_at = device, now
        self.changes = changes or []

    # --- what pages say and do

    def join(self, page: str, device: str, now: float) -> Page:
        """The page `page` of `device`, known from now on. A page new to the room joins it;
        one whose browser had a page on the group's last video, or before the pages were told
        to load again, follows the group. A page
        that was quiet PAGE_SECONDS joins again, with a cooldown."""
        known = self.pages.get(page)
        if known is not None:
            if now - known.seen > PAGE_SECONDS:
                known.joined, known.follows = None, False
            known.seen = now
            return known
        since = max(self.video_changed, self.reloaded)
        follows = bool(since) and now - since <= FOLLOW_SECONDS
        follows = follows and any(
            p.device == device and p.seen >= since - clients.ACTIVE_SECONDS
            for p in self.pages.values()
        )
        kept = [(k, p) for k, p in self.pages.items() if now - p.seen <= PAGE_SECONDS]
        kept = sorted(kept, key=lambda item: item[1].seen)[-(MAX_PAGES - 1) :]
        self.pages = dict(kept)
        self.pages[page] = joined = Page(device[:MAX_DEVICE], now, follows=follows)
        return joined

    def reload(self, now: float) -> None:
        """Every page loads again (the stream switched the player's look): the pages that
        come back from the browsers that had one are in at once, with no cooldown."""
        self.reloaded = now

    def report(
        self,
        page: str,
        device: str,
        now: float,
        video: int = 0,
        position: float | None = None,
        duration: float | None = None,
        ready: bool = False,
        offset: float | None = None,
        name: str = "",
        rev: int | None = None,
    ) -> Page:
        """Take one report of a page: the video it is on, where it is, how long the video is,
        and whether it is loaded and at the group's place (`ready`). Its cooldown starts
        with the first report that says so.

        A page that watches alone, in the group's place, takes the group with it: one whose
        video waits for data is not out of sync with nobody. Its `offset` must be from the
        group as it is now (`rev`), with no change on its way.

        Its drift is the `offset` it measured itself, where the report has one: `position`
        is where it was when the report left, and over the internet the group has moved on
        by the time it comes (a page in step would show as behind by the trip there)."""
        self.tick(now)
        known = self.join(page, device, now)
        known.video = video
        known.name = name[: clients.MAX_NAME]
        if video == self.video and duration and duration > 0:
            # The longest any page says: one version of a video (subtitles burned in) may
            # end a little before another, and the group must not stop before the last.
            self.durations[video] = max(duration, self.durations.get(video, 0.0))
        if video == self.video and offset is not None:
            known.drift = offset
        elif video == self.video and position is not None:
            known.drift = position - self.live(now)
        else:
            known.drift = None
        if (
            known.drift is not None
            and offset is not None
            and abs(offset) > SAME_SECONDS
            and known.joined is not None
            and rev == self.rev
            and not self.paused
            and self.window_until is None
            and self.alone(page, now)
        ):
            self._set(now, self.live(now) + offset)
            known.drift = 0.0
        if ready and known.joined is None and video == self.video:
            known.joined = now - (COOLDOWN_SECONDS if known.follows else 0.0)
        self.tick(now)  # a duration learned now may be the end
        return known

    def alone(self, page: str, now: float) -> bool:
        """Whether `page` is the only page that watches now (see clients.ACTIVE_SECONDS)."""
        return not any(
            key != page and now - p.seen <= clients.ACTIVE_SECONDS for key, p in self.pages.items()
        )

    def cooldown(self, page: str, now: float) -> float:
        """The seconds before this page's presses count. A page that has not said it is
        loaded waits the whole cooldown, and more."""
        known = self.pages.get(page)
        if known is None or known.joined is None:
            return COOLDOWN_SECONDS
        return max(0.0, COOLDOWN_SECONDS - (now - known.joined))

    def propose(
        self,
        kind: str,
        value: Any,
        page: str,
        device: str,
        action: str,
        now: float,
    ) -> str:
        """A press of `page` (of `device`), or of the owner (`page` ""). The answer is
        APPLIED (at once), QUEUED (it waits for the window to close), COOLDOWN (the page
        has just joined: nothing is done) or REFUSED. An `action` id that was seen already
        answers as it did then and does nothing more. Raises TogetherError for a value that
        does not read."""
        self.tick(now)
        self._forget_actions(now)
        if action and action in self.actions:
            return self.actions[action][1]
        value = read_value(kind, value, self.videos)
        if page and self.cooldown(page, now) > 0:
            outcome = COOLDOWN
        elif page and (page not in self.pages or self.pages[page].video != self.video):
            outcome = REFUSED  # a page on another video than the group's
        else:
            if kind == SKIP:
                waits = self.pending.get(SEEK)
                own = waits is not None and waits.page == page and waits.video == self.video
                base = waits.value if own and waits is not None else self.live(now)
                kind, value = SEEK, max(0.0, base + value)
            if self.duration and kind == SEEK:
                value = min(value, self.duration)
            known = self.pages.get(page)
            name = known.name if known is not None else ""
            proposal = Proposal(value, page, device[:MAX_DEVICE], self.video, name)
            if self.window_until is None:
                # A press that changes nothing opens no window: it holds up no one.
                if self._apply({kind: proposal}, now):
                    self.window_until = now + WINDOW_SECONDS
                outcome = APPLIED
            else:
                self.pending[kind] = proposal
                outcome = QUEUED
        if action:
            self.actions[action] = (now, outcome)
        return outcome

    def tick(self, now: float) -> bool:
        """Close a window that is due, and pause a group that reached the end of its video.
        True when that changed the room. Pages gone quiet leave the call (see Call.tick)."""
        self.call.tick(now)
        rev = self.rev
        if self.window_until is not None and now >= self.window_until:
            pending, self.pending, self.window_until = self.pending, {}, None
            self._apply(pending, now)
        if not self.paused and self.duration and self.live(now) >= self.duration - END_SECONDS:
            self._set(now, self.duration, paused=True)
            self._changed("", now)
        return self.rev != rev

    def _apply(self, proposals: dict[str, Proposal], now: float) -> bool:
        """Apply the proposals, in KINDS order, that change something; one rev for all.
        True when one did."""
        changed = ""
        changes: list[dict[str, Any]] = []
        for kind in KINDS:
            p = proposals.get(kind)
            if p is None:
                continue
            if kind == VIDEO and p.value != self.video:
                self.video, self.position, self.anchor, self.paused = p.value, 0.0, now, True
                self.video_changed = now
            elif kind == PLAY and p.video == self.video and p.value == self.paused:
                at_end = bool(self.duration) and self.live(now) >= self.duration - 1.0
                self._set(now, 0.0 if p.value and at_end else None, paused=not p.value)
            elif kind == SEEK and p.video == self.video:
                if abs(p.value - self.live(now)) < SAME_SECONDS:
                    continue
                self._set(now, p.value)
            elif kind == AUDIO and p.value != self.audio:
                self.audio = p.value
            elif kind == SUB and p.value != self.sub:
                self.sub = p.value
            else:
                continue
            changed = p.device or OWNER
            what = kind if kind != PLAY else (PLAY if p.value else PAUSE)
            changes.append({"what": what, "by": changed, "page": p.page, "name": p.name})
        if changed:
            self._changed(changed, now, changes)
        return bool(changed)

    def _forget_actions(self, now: float) -> None:
        old = [k for k, (at, _) in self.actions.items() if now - at > ACTION_SECONDS]
        for key in old:
            del self.actions[key]
        while len(self.actions) >= MAX_ACTIONS:
            del self.actions[next(iter(self.actions))]

    # --- the call

    def start_call(self, page: str, now: float) -> str:
        """Call pressed on a page of the group (see Call.start); REFUSED for a page the
        room does not know."""
        self.tick(now)
        if page not in self.pages:
            return REFUSED
        return self.call.start(page, now)

    def _name(self, page: str) -> str:
        known = self.pages.get(page)
        return known.name if known is not None else ""

    def call_state(self, page: str, now: float, after: int | None = None) -> dict[str, Any]:
        """What `page` is told of the call: who is in it, in the order they joined, with
        their names and what they have on; whether it is rung; and its messages newer than
        `after`. `id` is "" while no call runs."""
        call = self.call
        members = [
            {"page": key, "name": self._name(key), "mic": m.mic, "cam": m.cam, "sees": m.sees}
            for key, m in call.members.items()
        ]
        return {
            "id": call.id,
            "v": call.version,
            "by": self._name(call.starter),
            "members": members,
            "ring": call.rings(page),
            "mail": call.take(page, after, now),
        }

    # --- as plain data

    def state(self, now: float) -> dict[str, Any]:
        """What every player follows, whole, so a missed update is never lost."""
        return {
            "rev": self.rev,
            "video": self.video,
            "position": round(self.live(now), 3),
            "paused": self.paused,
            "audio": self.audio,
            "sub": self.sub,
            "duration": self.duration,
            "window": round(max(0.0, (self.window_until or now) - now), 3),
            "changed_by": self.changed_by,
            "changed_ago": round(max(0.0, now - self.changed_at), 1) if self.changed_at else None,
            "changes": [dict(c) for c in self.changes],
            "now": now,
        }

    def describe(self, now: float) -> dict[str, Any]:
        """What the dashboard shows: the state, and the pages of the group by device."""
        self.tick(now)
        pages = [
            {
                "page": key,
                "device": p.device,
                "video": p.video,
                "drift": None if p.drift is None else round(p.drift, 2),
                "out_of_sync": p.drift is not None and abs(p.drift) > SYNC_SECONDS,
                "cooldown": round(self.cooldown(key, now), 1),
                "seen": p.seen,
                "name": p.name,
            }
            for key, p in sorted(self.pages.items(), key=lambda item: item[1].seen)
            if now - p.seen <= clients.ACTIVE_SECONDS
        ]
        members = [
            {
                "page": key,
                "device": self.pages[key].device if key in self.pages else "",
                "name": self._name(key),
                "mic": m.mic,
                "cam": m.cam,
            }
            for key, m in self.call.members.items()
        ]
        call = {"id": self.call.id, "members": members}
        return {**self.state(now), "pending": sorted(self.pending), "pages": pages, "call": call}

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        """For the state file. A room read back is paused where it was at `now` (by default,
        at its anchor)."""
        at = self.live(now) if now is not None else self.position
        return {
            "video": self.video,
            "position": at,
            "audio": self.audio,
            "sub": self.sub,
            "rev": self.rev,
            "changed_by": self.changed_by,
            "changed_at": self.changed_at,
            "video_changed": self.video_changed,
            "durations": {str(n): d for n, d in self.durations.items()},
            "pages": {
                key: {"device": p.device, "seen": p.seen, "video": p.video, "joined": p.joined}
                for key, p in self.pages.items()
            },
            # Not who is in the call: their connections go with the daemon. Its count goes
            # on, so the pages see the change.
            "call": {"version": self.call.version, "calls": self.call.calls},
        }

    @classmethod
    def from_dict(cls, data: Any, videos: int) -> Room:
        """Read back what `to_dict` gave, for a stream of `videos` videos. The group comes
        back paused; values that do not fit are dropped."""
        room = cls(videos=videos)
        if not isinstance(data, dict):
            return room
        video = data.get("video")
        if is_whole(video) and 0 < video <= videos:
            room.video = video
        for name in ("position", "changed_at", "video_changed"):
            value = data.get(name)
            if is_finite(value) and value >= 0:
                setattr(room, name, float(value))
        room.position = min(room.position, clients.MAX_SECONDS)
        for name, read in ((AUDIO, clients.audio_value), (SUB, clients.sub_value)):
            if data.get(name) is not None:
                with contextlib.suppress(clients.ClientError):
                    setattr(room, name, read(data[name]))
        rev = data.get("rev")
        # Pages still hold the old rev: the next is new to all of them.
        room.rev = rev + 1 if is_whole(rev) and 0 < rev < 10**12 else 1
        if isinstance(data.get("changed_by"), str):
            room.changed_by = data["changed_by"][:MAX_DEVICE]
        durations = data.get("durations")
        for key, value in durations.items() if isinstance(durations, dict) else []:
            good = isinstance(key, str) and key.isdecimal() and is_finite(value) and value > 0
            if good and 0 < int(key) <= videos:
                room.durations[int(key)] = float(value)
        call = data.get("call")
        call = call if isinstance(call, dict) else {}
        for name in ("version", "calls"):
            value = call.get(name)
            if is_whole(value) and 0 <= value < 10**12:
                setattr(room.call, name, value + (name == "version"))
        pages = data.get("pages")
        for key, raw in list(pages.items())[:MAX_PAGES] if isinstance(pages, dict) else []:
            page = _page(raw)
            if clients.page_key(key) and page is not None:
                room.pages[key] = page
        return room


def _page(raw: Any) -> Page | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("device"), str):
        return None
    seen, video, joined = raw.get("seen"), raw.get("video"), raw.get("joined")
    if not is_finite(seen) or not is_whole(video):
        return None
    page = Page(raw["device"][:MAX_DEVICE], float(seen), video)
    page.joined = float(joined) if is_finite(joined) else None
    return page
