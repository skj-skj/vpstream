"""What the window shows and sends, as plain data: the words for policies and devices, the
address list, the warnings, the options of a start. No Tk here, so all of it is tested on
any system; `app` only puts it on the screen."""

from __future__ import annotations

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
        kind: ntpath.normpath(path),  # Tk writes C:/Films; the cache knows C:\\Films
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

    @property
    def cells(self) -> tuple[str, ...]:
        return (self.name, self.address, self.what, self.state, self.sent, self.seen)


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


def warnings(address: Address | None, blocking: Sequence[str], other_admin: str = "") -> list[str]:
    found = []
    if address is not None and address.public:
        net = f' "{address.network}"' if address.network else ""
        found.append(
            f"Windows treats the network{net} on {address.adapter} as Public, so phones and TVs "
            "on it cannot reach this PC. If it is your home network, make it private."
        )
    if blocking:
        found.append(
            "Windows Firewall has rules that block Caddy, the program that serves the stream "
            "(Windows adds them when its question is answered Cancel). Remove them to stream."
        )
    if other_admin:
        found.append(
            f"vpstream runs as {other_admin}, the administrator who allowed it, not as you: "
            "its settings and prepared videos are that account's."
        )
    return found
