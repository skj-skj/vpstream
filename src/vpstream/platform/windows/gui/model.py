"""What the window shows and sends, as plain data: the words for policies and devices, the
address list, the warnings, the options of a start, every question the window asks, and how
its bars move. No Qt here, so all of it is tested on any system; `app` and `browser` only put
it on the screen."""

from __future__ import annotations

import math
import ntpath
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from vpstream import gate, stream, text
from vpstream.platform.windows.network import Address

POLICIES = {
    gate.APPROVAL: "Ask me before each new device",
    gate.OPEN: "Any device on this network",
    gate.LOCKED: "Only the devices I let in",
}
DIRECT = "direct"
BROWSER = "browser"
MODES = {
    DIRECT: "VLC or another player app",
    BROWSER: "A web browser or TV (prepared first)",
}
AUTOMATIC = "Automatic"


def default_policy(mode: str) -> str:
    """The label of what a stream of `mode` lets in when the person does not choose."""
    return POLICIES[stream.DEFAULT_POLICY[mode]]


def policy_of(label: str) -> str:
    return next((key for key, text in POLICIES.items() if text == label), gate.APPROVAL)


@dataclass(frozen=True)
class Choice:
    label: str
    value: str  # the address kept in the configuration; "" for automatic


def address_choices(addresses: Sequence[Address], automatic: str) -> list[Choice]:
    """ "Automatic" (what Windows reaches the network from, `automatic`), then each private
    address with its adapter."""
    first = f"{AUTOMATIC} ({automatic})" if automatic else f"{AUTOMATIC} (no network now)"
    return [Choice(first, ""), *(Choice(a.label, a.ip) for a in addresses)]


def chosen_label(choices: Sequence[Choice], configured: str) -> str:
    """The label of the configured address; one this PC no longer has is shown as such."""
    for choice in choices:
        if choice.value == configured:
            return choice.label
    return f"{configured} (not on this PC now)"


def address_of(choices: Sequence[Choice], label: str) -> str | None:
    return next((c.value for c in choices if c.label == label), None)


def network_of(addresses: Sequence[Address], ip: str) -> Address | None:
    return next((a for a in addresses if a.ip == ip), None)


def start_options(path: str, kind: str, alias: str, mode: str, policy_label: str) -> dict[str, Any]:
    """What `Control.start` is given. Raises ValueError with what is missing."""
    if not path:
        raise ValueError("Pick a video file or a folder of videos first.")
    options: dict[str, Any] = {
        kind: ntpath.normpath(path),  # the picker writes C:/Films; the cache knows C:\\Films
        "mode": mode,
        "policy": policy_of(policy_label),
    }
    if alias.strip():
        options["alias"] = alias.strip()
    return options


# --- a running stream


def size(count: int) -> str:
    for unit, step in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if count >= step:
            return f"{count / step:.1f} {unit}"
    return f"{count} B"


def ago(seconds: float) -> str:
    if seconds < 5:
        return "now"
    if seconds < 90:
        return f"{seconds:.0f} s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min ago"
    return f"{seconds / 3600:.0f} h ago"


def device_state(device: dict[str, Any], policy: str) -> str:
    allowed, passes = device.get("allowed"), device.get("passes")
    if allowed is True:
        return "Let in"
    if allowed is False:
        return "Kept out"
    if passes:
        return "Watching (anyone may)"
    if policy == gate.APPROVAL:
        return "Waiting for you"
    return "Kept out (not let in)"


@dataclass(frozen=True)
class DeviceRow:
    id: str
    name: str
    address: str
    what: str
    state: str
    sent: str
    seen: str
    code: str  # what its waiting page shows, to tell devices apart
    playing: str = ""  # a browser's: the video, where in it

    @property
    def cells(self) -> tuple[str, ...]:
        what = f"{self.what} {self.code}" if self.code else self.what
        return (self.name, self.address, what, self.state, self.playing, self.sent, self.seen)


def what_it_is(device: dict[str, Any]) -> str:
    agent = str(device.get("user_agent") or "")
    if device.get("kind") == gate.BROWSER:
        return "Browser"
    for name in ("VLC", "MX Player", "Kodi", "mpv", "Infuse", "nPlayer"):
        if name.lower() in agent.lower():
            return name
    return "Player"


def device_rows(running: dict[str, Any], now: float | None = None) -> list[DeviceRow]:
    now = time.time() if now is None else now
    policy = running.get("policy", gate.APPROVAL)
    return [
        DeviceRow(
            id=str(d["id"]),
            name=str(d.get("name") or "(no name)"),
            address=str(d.get("ip") or ""),
            what=what_it_is(d),
            state=device_state(d, policy),
            sent=size(int(d.get("bytes") or 0)),
            seen=ago(now - float(d.get("last_seen") or now)),
            code=str(d.get("code") or ""),
            playing=where_it_is(d) if d.get("kind") == gate.BROWSER else "",
        )
        for d in running.get("devices", [])
    ]


def stream_lines(status: dict[str, Any]) -> list[str]:
    """What the window says about the running stream, under its address."""
    running = status["stream"]
    videos = running["videos"]
    what = videos[0]["name"] if running["kind"] == "file" else f"{len(videos)} videos"
    lines = [f"Streaming {what} from {running['source']}"]
    left = running.get("seconds_left")
    if left is not None:
        lines.append(f"It stops by itself in {text.duration(left)}.")
    if running["kind"] == "dir":
        lines.append(f"Each video: {running['url']}/1, /2, ... (the address alone lists them)")
    lines += [f"Video {v['number']}: {v['note']}" for v in videos if v.get("note")]
    if any(v.get("preparing") for v in videos):
        lines.append("A copy that VLC can seek in quickly is being made; it plays in a moment.")
    if running["policy"] == gate.APPROVAL and any(
        d.get("allowed") is None and not d.get("passes") for d in running["devices"]
    ):
        lines.append("A device is waiting: select it below and press Let in.")
    return lines


# --- warnings

MAKE_PRIVATE = "Make it private"
UNBLOCK = "Remove them"


@dataclass(frozen=True)
class Notice:
    text: str
    button: str = ""  # MAKE_PRIVATE, UNBLOCK or none


def warnings(
    address: Address | None, blocking: Sequence[str], other_admin: str = ""
) -> list[Notice]:
    found = []
    if address is not None and address.public:
        net = f' "{address.network}"' if address.network else ""
        found.append(
            Notice(
                f"Windows treats the network{net} on {address.adapter} as Public, so phones "
                "and TVs on it cannot reach this PC. If it is your home network, make it private.",
                MAKE_PRIVATE,
            )
        )
    if blocking:
        found.append(
            Notice(
                "Windows Firewall has rules that block Caddy, the program that serves the "
                "stream (Windows adds them when its question is answered Cancel). Remove them "
                "to stream.",
                UNBLOCK,
            )
        )
    if other_admin:
        found.append(
            Notice(
                f"vpstream runs as {other_admin}, the administrator who allowed it, not as you: "
                "its settings and prepared videos are that account's."
            )
        )
    return found


# --- the programs vpstream needs

SETUP_INTRO = (
    "vpstream needs these programs. It downloads them from their own sites and checks each "
    "one before it is used."
)


INSTALLED = "installed"


def setup_line(name: str, what: str = "not installed") -> str:
    return f"{name}: {what}"


def failed_line(exc: Exception) -> str:
    return f"failed: {exc}"


def setup_target(what: str, done: int, total: int) -> float | None:
    """Where an install's bar heads: None while the size is not known (a busy bar)."""
    if what == INSTALLED:
        return 1.0
    if what.startswith("failed"):
        return 0.0
    return min(done / total, 1.0) if total else None


# --- browser mode: what to prepare, and whether it is ready


@dataclass(frozen=True)
class Track:
    number: int
    label: str
    how: str = ""  # subtitles: probe's treatment (soft, burned in, unsupported)

    @property
    def text(self) -> str:
        how = {
            "soft": "shown by the player",
            "burned in": "drawn into the picture: a slower prepare",
            "unsupported": "cannot be shown",
        }.get(self.how, "")
        return f"{self.number}: {self.label}" + (f" ({how})" if how else "")


def plan_tracks(plans: Sequence[dict[str, Any]]) -> tuple[list[Track], list[Track]]:
    """The audio and subtitle tracks to offer: the first video's that has any (episodes of
    a folder usually have the same; a number is chosen for all of them)."""
    for plan in plans:
        rows = plan.get("tracks") or []
        if rows:
            audio = [Track(r["number"], r["label"]) for r in rows if r["kind"] == "audio"]
            subs = [
                Track(r["number"], r["label"], r.get("treatment", ""))
                for r in rows
                if r["kind"] == "subtitle"
            ]
            return audio, subs
    return [], []


def sub_spec(numbers: Sequence[int]) -> str:
    """The subtitles a prepare makes, as `prepare --sub` takes them."""
    return ",".join(str(n) for n in sorted(set(numbers))) if numbers else "none"


def what_is_left(plans: Sequence[dict[str, Any]]) -> list[str]:
    """What still has to be made before a browser can play: one line per video, [] when
    everything is ready."""
    lines = []
    for plan in plans:
        tasks = plan.get("tasks") or []
        if plan.get("tracks") == [] and plan.get("notes"):
            lines.append(f"{plan['name']}: {'; '.join(plan['notes'])}")
        elif tasks:
            seconds = sum(t.get("seconds") or 0 for t in tasks)
            names = ", ".join(t["task"] for t in tasks)
            lines.append(f"{plan['name']}: {names} (about {text.duration(seconds)})")
    return lines


def start_state(left: Sequence[str]) -> str:
    """What the Stream tab says about a browser stream's video: ready, or what is left."""
    if not left:
        return "Prepared: it can start."
    more = "\n..." if len(left) > 5 else ""
    return "Not prepared yet:\n" + "\n".join(left[:5]) + more


def left_text(left: Sequence[str]) -> str:
    """What the Prepare tab says is still to make."""
    return "All of it is prepared." if not left else "To prepare:\n" + "\n".join(left)


def browser_start(options: dict[str, Any], audio: int | None, sub: int | None) -> dict[str, Any]:
    """A browser stream's start: the audio and subtitles each browser begins with."""
    found = {**options, "mode": BROWSER}
    if audio is not None:
        found["audio"] = str(audio)
    found["sub"] = str(sub) if sub is not None else "off"
    return found


# --- browser mode: the players


def seconds_of(text_: str) -> float:
    """`90`, `12:30` or `1:02:03` as seconds. Raises ValueError."""
    parts = text_.strip().split(":")
    if not text_.strip() or len(parts) > 3 or not all(p.strip().isdigit() for p in parts):
        raise ValueError(f"not a time: {text_!r}. Write it as 12:30 or 1:02:03")
    total = 0
    for part in parts:
        total = total * 60 + int(part)
    return float(total)


def clock(seconds: float) -> str:
    whole = int(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def where_it_is(device: dict[str, Any]) -> str:
    """What a browser's player says it does: the video, where in it, playing or paused."""
    client = device.get("client")
    if not client or not client.get("video"):
        return "not playing yet"
    if not client.get("active"):
        return f"video {client['video']}, gone quiet"
    at = f"{clock(client.get('position') or 0)} of {clock(client.get('duration') or 0)}"
    return f"video {client['video']}, {at}, {'paused' if client.get('paused') else 'playing'}"


def menu_for(status: dict[str, Any], device: dict[str, Any] | None) -> dict[str, list[Any]]:
    """The audio and subtitle choices for what `device` plays (video 1 when it plays
    nothing yet), as the player names them."""
    videos = status["stream"]["videos"]
    number = ((device or {}).get("client") or {}).get("video") or 1
    menu = videos[number - 1].get("menu") if 0 < number <= len(videos) else None
    menu = menu or {"audio": [], "subs": []}
    return {"audio": menu["audio"], "subs": [{"value": "off", "label": "Off"}, *menu["subs"]]}


def send_options(
    devices: Sequence[str] | None, audio: str | None, sub: str | None, seek: str
) -> dict[str, Any]:
    """What `Control.set_clients` is sent from the player panel: to `devices` (None for
    all), the menu values chosen (None for no change), a time to go to. Raises ValueError
    with what to do first."""
    if devices is not None and not devices:
        raise ValueError(PICK_BROWSER)
    options: dict[str, Any] = {"all": True} if devices is None else {"devices": list(devices)}
    if audio is not None:
        options["audio"] = audio
    if sub is not None:
        options["sub"] = sub
    if seek.strip():
        options["seek"] = seconds_of(seek)
    if len(options) == 1:
        raise ValueError("Choose audio, subtitles or a time to go to first.")
    return options


# --- the prepared videos (the library)


@dataclass(frozen=True)
class LibraryRow:
    key: str
    source: str
    name: str
    subtitles: str
    size: str
    state: str
    ready: bool  # a browser can play it now
    bytes: int = 0

    @property
    def cells(self) -> tuple[str, ...]:
        return (self.name, self.subtitles, self.size, self.state)


def library_rows(rows: Sequence[dict[str, Any]]) -> list[LibraryRow]:
    """What `Control.prepared` lists, as the window shows it: ready ones first, by name."""
    found = []
    for row in rows:
        burn = set(row.get("burn") or [])
        subs = sorted(set(row.get("soft") or []) | burn)
        said = ", ".join(f"{n} (drawn in)" if n in burn else str(n) for n in subs)
        if row.get("stale"):
            state = "the file changed or is gone"
        elif row.get("busy"):
            state = "being prepared"
        elif not row.get("base"):
            state = "not ready (no base version)"
        elif row.get("in_use"):
            state = "streaming now"
        elif row.get("unchecked"):
            state = "ready (the file cannot be looked at now)"
        else:
            state = "ready"
        found.append(
            LibraryRow(
                key=str(row["key"]),
                source=str(row["source"]),
                name=ntpath.basename(str(row["source"])),
                subtitles=said or "none",
                size=size(int(row.get("bytes") or 0)),
                state=state,
                ready=bool(row.get("base")) and not row.get("stale") and not row.get("busy"),
                bytes=int(row.get("bytes") or 0),
            )
        )
    return sorted(found, key=lambda r: (not r.ready, r.name.lower()))


def prepared_subs(rows: Sequence[dict[str, Any]], source: str) -> list[int]:
    """The subtitle tracks prepared for `source` (drawn by the player or drawn in)."""
    for row in rows:
        if ntpath.normcase(str(row.get("source"))) == ntpath.normcase(source):
            return sorted(set(row.get("soft") or []) | set(row.get("burn") or []))
    return []


def streaming_what(running: dict[str, Any]) -> str:
    """The running stream in a few words: its file's name, or its folder."""
    return str(running["videos"][0]["name"] if running["kind"] == "file" else running["source"])


def switch_options(running: dict[str, Any] | None, source: str) -> dict[str, Any]:
    """A browser stream of a prepared file; in place of a running stream, at its address
    and with its policy, so a TV only reloads the page to get the next video."""
    options: dict[str, Any] = {"file": source, "mode": BROWSER, "sub": "off"}
    if running:
        options["alias"] = running["alias"]
        options["policy"] = running["policy"]
    return options


# --- the queue: one row for each video


@dataclass(frozen=True)
class VideoJob:
    """The parts of one video queued by one Prepare (its jobs, by consecutive ids), as the
    queue shows them: one line, one bar."""

    key: int  # the first job's id
    source: str
    name: str
    state: str  # running, queued, done, failed or cancelled
    progress: float  # 0..1, the parts weighted by how long each is expected to take
    line: str

    @property
    def active(self) -> bool:
        """It has parts still to make: it can be cancelled."""
        return self.state in ("queued", "running")


def video_jobs(prepare: Sequence[dict[str, Any]]) -> list[VideoJob]:
    """What runs and waits, then the last 5 videos that failed or were cancelled, to say
    why, unless the video was queued again since. A video prepared leaves the queue: it is
    in the list of prepared videos."""
    by_source: dict[str, list[list[dict[str, Any]]]] = {}
    for job in sorted(prepare, key=lambda j: int(j["id"])):
        runs = by_source.setdefault(str(job["source"]), [])
        if runs and int(job["id"]) == int(runs[-1][-1]["id"]) + 1:
            runs[-1].append(job)
        else:
            runs.append([job])
    found = [_video_job(run) for runs in by_source.values() for run in runs]
    active = [v for v in found if v.active]
    latest = {v.source: v.key for v in sorted(found, key=lambda v: v.key)}
    ended = [v for v in found if v.state in ("failed", "cancelled") and latest[v.source] == v.key]
    return sorted(active, key=lambda v: v.key) + sorted(ended, key=lambda v: -v.key)[:5]


def _video_job(run: list[dict[str, Any]]) -> VideoJob:
    states = [str(j.get("state")) for j in run]
    weights = [max(float(j.get("estimate") or 0), 1.0) for j in run]
    done = [
        1.0 if s == "done" else float(j.get("progress") or 0)
        for j, s in zip(run, states, strict=True)
    ]
    progress = sum(w * d for w, d in zip(weights, done, strict=True)) / sum(weights)
    name = str(run[0]["name"])
    count = parts(len(run))
    if "running" in states or "queued" in states:
        state = "running" if "running" in states else "queued"
        current = next((j for j in run if j.get("state") == "running"), None)
        if current is None:
            line = f"{name}: waits for the one before ({count})"
        else:
            number = run.index(current) + 1
            left = sum(
                float(j.get("seconds_left") or 0)
                for j in run
                if j.get("state") in ("queued", "running")
            )
            what = f"part {number} of {len(run)}: {current['task']}"
            line = (
                f"{name}: {what}, starting..."
                if not current.get("progress")
                else f"{name}: {what}, {progress * 100:.0f}%, about {text.duration(left)} left"
            )
    elif "failed" in states:
        state = "failed"
        bad = next(j for j in run if j.get("state") == "failed")
        line = f"{name}: {bad['task']} failed: {bad.get('error') or 'see the log'}"
    elif "cancelled" in states:
        state, line = "cancelled", f"{name}: cancelled"
    else:
        state, line, progress = "done", "", 1.0  # not shown: it left the queue
    return VideoJob(int(run[0]["id"]), str(run[0]["source"]), name, state, progress, line)


def parts(count: int) -> str:
    return f"{count} part" + ("s" if count != 1 else "")


def queued_said(count: int) -> str:
    return f"Queued: {parts(count)}. Choose the next one any time."


def prepared_said(active: int) -> str:
    if not active:
        return "Prepared."
    return f"{active} video{'s' if active != 1 else ''} still to prepare."


def cancelled_said(count: int) -> str:
    return f"Cancelled {parts(count)}."


# --- the questions and notes the window shows

ONE_AT_A_TIME = "A stream is starting; try again in a moment."


def close_question(starting: bool, streaming: bool) -> str | None:
    """What closing asks first, None when it just closes."""
    if streaming:
        return "A stream is running. Stop it and close vpstream?"
    if starting:
        return "A stream is starting. Stop it and close vpstream?"
    return None


PICK_DEVICE = "Select a device in the list first."
PICK_BROWSER = "Select a browser in the list first."
PICK_PREPARED = "Select a video in the list first."
RENAME = "A name for this device:"
PICK_ONE = "Select one video to stream."
NOTHING_QUEUED = "Nothing is being prepared."
SELECT_BROWSER = "Select a browser in the list."
SWITCH_HINT = "Stream it while another streams: the next one plays at the same address."


def ask_switch(now: str, source: str) -> str:
    return f"Stop streaming {now} and stream {ntpath.basename(source)} at the same address?"


def ask_remove(rows: Sequence[LibraryRow]) -> str:
    """The question before removing what was prepared of the selected videos."""
    if len(rows) == 1:
        return (
            f"Remove what was prepared of {rows[0].name} ({rows[0].size})? The video itself stays."
        )
    total = size(sum(r.bytes for r in rows))
    return f"Remove what was prepared of {len(rows)} videos ({total})? The videos themselves stay."


def kept_said(kept: Sequence[str]) -> str:
    """After a remove: the videos kept because they stream or are being prepared."""
    kept = list(dict.fromkeys(kept))  # a video can have two prepared versions
    if not kept:
        return ""
    if len(kept) == 1:
        return f"{ntpath.basename(kept[0])} is streaming or being prepared, so it was kept."
    return f"{len(kept)} videos are streaming or being prepared, so they were kept."


def ask_long(reason: str) -> str:
    """A prepare that takes longer than the film (`NeedsConfirm`)."""
    return f"{reason}.\n\nPrepare it all the same?"


def not_ready(row: LibraryRow) -> str:
    return f"{row.name} is {row.state}."


# --- the bars: they glide to each value the poll brings once a second, and a job that has
# not said how far it is yet (ffmpeg's first seconds) shows a calm busy bar instead

FRAME_SECONDS = 0.04  # 25 frames a second
GLIDE = 0.2  # each frame covers this part of what is left
BUSY_SECONDS = 2.4  # one sweep of the busy bar across
BUSY_WIDTH = 0.25  # the busy bar's moving part, of the whole


def bar_target(state: str, progress: float) -> float | None:
    """Where a job's bar heads: None while it runs at 0% (a busy bar), 1 once done."""
    if state == "running" and progress <= 0.0:
        return None
    if state == "done":
        return 1.0
    if not math.isfinite(progress):
        return 0.0  # a bar heading for NaN would never arrive
    return min(max(progress, 0.0), 1.0)


def glide(value: float, target: float) -> float:
    """The bar's next frame: a part of the way to `target`, there once close."""
    gap = target - value
    return target if abs(gap) < 0.002 else value + gap * GLIDE


def busy_span(seconds: float) -> tuple[float, float]:
    """The part of a busy bar that is filled, `seconds` after it began, as (start, end) in
    0..1: a piece that slides in from the left and out at the right, again and again."""
    at = (seconds % BUSY_SECONDS) / BUSY_SECONDS * (1 + BUSY_WIDTH) - BUSY_WIDTH
    return max(at, 0.0), min(at + BUSY_WIDTH, 1.0)


# --- what the window says as it goes (the line at the bottom), and its pickers

STARTING = "Starting..."
READY = "Ready."
ALL_INSTALLED = "Everything vpstream needs is installed."
STARTING_STREAM = "Starting the stream..."
STREAM_ON = "The stream is on."
STOPPING = "Stopping..."
STOPPED = "Stopped."
COPIED = "The address is copied."
CLOSING = "Closing..."
LOOKING = "looking..."
LOOKING_VIDEO = "Looking at the video..."
LOOKING_PREPARED = "Looking at what is prepared..."
NO_SUBS = "(none in this video)"
OFF = "Off"
PICK_VIDEO = "A video to stream"
PICK_FOLDER = "A folder of videos"
PICK_TO_PREPARE = "A video to prepare"
VIDEO_FILTERS = ("Videos (*.mkv *.mp4 *.m4v *.mov *.avi *.webm *.ts)", "All files (*)")


def stopped_said(why: str) -> str:
    return f"The stream stopped: {why}."


def bug_said(error: BaseException) -> str:
    return f"Something went wrong: {error}\n\nThe log has more."


# --- the Tools tab: which ffmpeg, Caddy and MKVToolNix are used, and newer ones

TOOLS_INTRO = (
    "vpstream uses its own copy of a program when it has one, else the one installed for "
    "the whole system. Its copies come from each program's own release page, at the latest "
    "release, checked against the SHA-256 published with it."
)
INSTALL_OWN = "Install vpstream's copy"
UPDATE = "Update"
NOT_CHECKED = "not looked up yet"
CHECKING = "looking..."


def version_key(version: str) -> tuple[int, ...]:
    """`9.0.2` as (9, 0, 2), to compare; what is not a number counts as 0."""
    return tuple(int(part) if part.isdigit() else 0 for part in version.split("."))


def newer(found: str, have: str) -> bool:
    """Whether release `found` is newer than `have` ("" is older than any)."""
    if not have:
        return True
    one, other = version_key(found), version_key(have)
    width = max(len(one), len(other))
    return one + (0,) * (width - len(one)) > other + (0,) * (width - len(other))


@dataclass(frozen=True)
class ToolRow:
    name: str
    title: str
    where: str  # the program used, or that there is none
    version: str
    latest: str
    action: str  # INSTALL_OWN, UPDATE or "" (none)
    can_remove: bool  # vpstream has a copy of its own


def tool_row(
    name: str,
    title: str,
    path: str | None,
    own: bool,
    version: str,
    own_version: str | None,
    latest: str | None,
    problem: str = "",
    checking: bool = False,
) -> ToolRow:
    """One program in the Tools tab. Update is offered only for vpstream's own copy, when a
    newer release is out; any program without a copy of vpstream's may get one."""
    if path is None:
        where = "not found"
    else:
        where = f"{path} ({"vpstream's copy" if own else 'installed for the whole system'})"
    have = (own_version or version) if own else version
    if checking:
        said = CHECKING
    elif problem:
        said = f"cannot look it up: {problem}"
    elif latest is None:
        said = NOT_CHECKED
    elif own and not newer(latest, have):
        said = f"{latest} (up to date)"
    else:
        said = f"{latest} is out" if own else latest
    if not own:
        action = INSTALL_OWN
    elif latest is not None and newer(latest, have):
        action = UPDATE
    else:
        action = ""
    # Only what vpstream marked as its own is removed (a folder copied there by hand is not).
    return ToolRow(name, title, where, have or "unknown", said, action, own and bool(own_version))


def ask_install(title: str, version: str, size: int, where: str | None, own: bool) -> str:
    """The question before vpstream downloads a copy of a program, or a newer one."""
    big = f" ({size / 1e6:.0f} MB)" if size else ""
    if own:
        return f"Download {title} {version}{big} and use it in place of vpstream's copy?"
    after = f"vpstream uses it in place of {where}." if where else "vpstream uses it."
    return f"Download {title} {version}{big} into vpstream's folder? {after}"


def ask_remove_tool(title: str) -> str:
    return (
        f"Remove vpstream's copy of {title}? The one installed for the whole system, if "
        "there is one, is used after."
    )


def tool_removed(title: str, removed: bool) -> str:
    if not removed:
        return f"vpstream has no copy of {title} to remove."
    return f"vpstream's copy of {title} is removed."
