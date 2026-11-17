"""What the command line prints: the daemon's answers as text. Pure, so it is unit tested.

Only `text` and `certificate` are imported, so the command line still starts quickly.
"""

from __future__ import annotations

import time
from typing import Any

from vpstream import certificate, text


def dashboard_line(dash: dict[str, Any]) -> str:
    if dash.get("mode") == "public":
        notes = [f"public, port {dash.get('port_state') or 'unknown'}"]
        if not dash.get("password"):
            notes.append("NO PASSWORD: sudo vpstream dash-pass")
        line = f"{dash.get('url') or dash.get('problem')} ({'; '.join(notes)})"
        if dash.get("certificate"):
            line += f"\n              certificate authority SHA-256 {dash['certificate']}"
        return line
    if dash.get("url"):
        return f"{dash['url']} (Tailscale only)"
    if dash.get("address"):
        return f"http://{dash['address']}/ on this host only; {dash.get('problem')}"
    return str(dash.get("problem"))


def _https_line(https: dict[str, Any]) -> str | None:
    """The stream's HTTPS and its certificate, or None while streams are plain HTTP."""
    if not https.get("enabled"):
        return None
    cert = https.get("certificate")
    if not cert:
        return "on; no certificate yet (the next stream asks for one)"
    when = certificate.when(float(cert["expires"]))
    left = float(cert["expires"]) - time.time()
    state = f"{text.duration(left)} left" if left > 0 else "expired; the next stream asks again"
    return f"on; certificate for {cert.get('host')} until {when} ({state})"


def device_state(device: dict[str, Any]) -> str:
    """One word for where a device stands; WL marks one that hit the waiting list."""
    allowed = device.get("allowed")
    if allowed is None:
        state = "allowed" if device.get("passes") else "waiting"
    else:
        state = "enabled" if allowed else "disabled"
    return f"{state} WL" if device.get("wl") else state


def format_devices(stream: dict[str, Any], now: float) -> str:
    devices = stream.get("devices") or []
    if not devices:
        return "No device has asked for the stream yet."
    rows = [
        ["DEVICE", "ADDRESS", "STATE", "CODE", "REQUESTS", "BLOCKED", "SENT", "LAST SEEN", "PLAYER"]
    ]
    for d in devices:
        rows.append(
            [
                str(d.get("id") or d.get("ip")),
                str(d.get("ip")),
                device_state(d),
                str(d.get("code") or "-"),
                str(d.get("requests", 0)),
                str(d.get("blocked", 0)),
                text.size(float(d.get("bytes") or 0)),
                _ago(d.get("last_seen"), now),
                _device_name(d)[:60],
            ]
        )
    return "\n".join(text.table(rows))


def _ago(when: Any, now: float) -> str:
    return f"{text.duration(max(0.0, now - when))} ago" if when else "never"


def _device_name(device: dict[str, Any]) -> str:
    """The name the owner gave a device, else its user agent."""
    return text.printable(str(device.get("name") or device.get("user_agent") or "-"))


def _clock(seconds: float) -> str:
    """1:02:03, or 2:03 under an hour: how a player shows a position."""
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def _track(menu: list[dict[str, Any]], value: Any) -> str:
    """A client's audio or subtitle choice by its label in the video's menu."""
    if value is None:
        return "-"
    if value in (0, "off"):
        return "off"
    return next((m["label"] for m in menu if m["value"] == str(value)), str(value))


def format_clients(stream: dict[str, Any], now: float) -> str:
    """The browsers of a browser stream: where each is, what it plays with, what waits."""
    browsers = [d for d in stream.get("devices") or [] if d.get("kind") == "browser"]
    if not browsers:
        return "No browser has opened the stream yet."
    videos = stream.get("videos") or []
    rows = [["DEVICE", "STATE", "VIDEO", "AT", "AUDIO", "SUBTITLES", "WAITING", "NAME"]]
    for d in browsers:
        c = d.get("client") or {}
        number = int(c.get("video") or 0)
        menu = videos[number - 1].get("menu") or {} if 0 < number <= len(videos) else {}
        if not d.get("passes"):
            state = device_state(d)
        elif not c.get("reported"):
            state = "not playing yet"
        elif not c.get("active"):
            state = f"gone {_ago(c['reported'], now)}"
        else:
            state = "paused" if c.get("paused") else "playing"
        at = _clock(float(c.get("position") or 0))
        if c.get("duration"):
            at += f" / {_clock(float(c['duration']))}"
        waiting = [f"{k} {v}" for k, v in sorted((c.get("pending") or {}).items())]
        if c.get("start_at"):
            video, seconds = c["start_at"]
            waiting.append(f"start {video} at {_clock(float(seconds))}")
        rows.append(
            [
                str(d.get("id")),
                state,
                str(number or "-"),
                at if number else "-",
                _track(menu.get("audio", []), c.get("audio")),
                _track(menu.get("subs", []), c.get("sub")),
                ", ".join(waiting) or "-",
                _device_name(d)[:40],
            ]
        )
    return "\n".join(text.table(rows))


def format_stream(stream: dict[str, Any]) -> str:
    """The running stream: what it is and the address of every video."""
    left = stream.get("seconds_left")
    limit = "no time limit" if left is None else f"stops in {text.duration(float(left))}"
    lines = [
        f"Stream:       /{stream.get('alias')} ({stream.get('mode')}, policy "
        f"{stream.get('policy')}, {limit})",
        f"Source:       {stream.get('source')}",
    ]
    videos = stream.get("videos") or []
    if stream.get("kind") == "dir":
        lines.append(
            f"Address:      {stream.get('url')}  (index for browsers, playlist for players)"
        )
        width = len(str(len(videos)))
        lines += [
            f"  {v['number']:>{width}}  {v['url']}  {v['name']}{_video_state(v)}" for v in videos
        ]
    else:
        lines.append(f"Address:      {stream.get('url')}{''.join(map(_video_state, videos[:1]))}")
    preparing = sum(bool(v.get("preparing")) for v in videos)
    if preparing:
        lines.append(
            f"Preparing:    {preparing} MKV {'file gets' if preparing == 1 else 'files get'} a "
            "full seek index (no re-encoding, about a minute per GB); until then its address "
            "says to try again"
        )
    lines += [f"note: video {v['number']}: {v['note']}" for v in videos if v.get("note")]
    return "\n".join(lines)


def _video_state(video: dict[str, Any]) -> str:
    job = video.get("job")
    if job:
        return "  [queued]" if job.get("state") == "queued" else f"  [{_percent(job)}]"
    if video.get("preparing"):
        return "  [preparing]"
    return "  [not ready]" if video.get("ready") is False else ""


def _percent(job: dict[str, Any]) -> str:
    return f"{100 * float(job.get('progress') or 0):.0f}%"


# prepare.ACTIVE; not imported, to keep the start of the command line quick.
ACTIVE = ("queued", "running")


def format_job(job: dict[str, Any]) -> str:
    """One prepare job on one line."""
    state = str(job.get("state"))
    left = job.get("seconds_left")
    if state == "running":
        about = f", about {text.duration(float(left))} left" if left is not None else ""
        state = f"{_percent(job)}{about}"
    elif state == "queued" and left is not None:
        state = f"queued, about {text.duration(float(left))}"
    line = f"{job.get('name')}: {job.get('task')}: {state}"
    return f"{line}: {job['error']}" if job.get("error") else line


def format_plans(plans: list[dict[str, Any]]) -> str:
    """What a prepare will make for each video, and about how long it takes."""
    lines = []
    for plan in plans:
        film = text.duration(float(plan.get("duration") or 0))
        tasks = plan.get("tasks") or []
        if tasks:
            took = text.duration(float(plan.get("seconds") or 0))
            warn = "  (longer than the film)" if plan.get("longer_than_film") else ""
            lines.append(f"{plan.get('name')} ({film}): about {took}{warn}")
            lines += [
                f"  {t.get('task')}: about {text.duration(float(t.get('seconds') or 0))}"
                for t in tasks
            ]
        else:
            lines.append(f"{plan.get('name')} ({film}): nothing to make")
        lines += [f"  note: {note}" for note in plan.get("notes") or []]
    return "\n".join(lines)


def format_prepared(rows: list[dict[str, Any]]) -> str:
    """Every video in the cache: what of it is prepared, and the disk it takes."""
    if not rows:
        return "Nothing is prepared."
    table = [["BASE", "SOFT", "BURNED IN", "SIZE", "SOURCE"]]
    for row in rows:
        flags = {"stale": "stale", "unchecked": "cannot check", "in_use": "in use",
                 "busy": "preparing"}  # fmt: skip
        marks = [mark for key, mark in flags.items() if row.get(key)]
        table.append(
            [
                str(row.get("base") or "-"),
                ",".join(map(str, row.get("soft") or [])) or "-",
                ",".join(map(str, row.get("burn") or [])) or "-",
                text.size(float(row.get("bytes") or 0)),
                str(row.get("source") or row.get("key"))
                + (f"  ({', '.join(marks)})" if marks else ""),
            ]
        )
    total = text.size(sum(float(row.get("bytes") or 0) for row in rows))
    return "\n".join([*text.table(table), f"{len(rows)} videos, {total} in all"])


def format_status(info: dict[str, Any], cli_version: str) -> str:
    """The daemon's status as text for `vpstream status`."""
    dash = info.get("dashboard") or {}
    stream = info.get("stream")
    lines = [
        f"vpstream daemon {info.get('version')}, "
        f"up {text.duration(float(info.get('uptime_seconds') or 0))} (pid {info.get('pid')})",
        format_stream(stream) if stream else "Stream:       none",
        f"Stream port:  {info.get('stream_port')} ({info.get('stream_port_state', 'unknown')})",
        "Dashboard:    " + dashboard_line(dash),
    ]
    if https := _https_line(info.get("https") or {}):
        lines.insert(3, f"HTTPS:        {https}")
    if stream:
        count = len(stream.get("devices") or [])
        lines.append(f"Devices:      {count} (see: vpstream devices)")
    jobs = [j for j in info.get("prepare") or [] if j.get("state") in ACTIVE]
    if jobs:
        lines.append(f"Preparing:    {format_job(jobs[0])}")
        if len(jobs) > 1:
            lines.append(f"              and {len(jobs) - 1} more queued (see: vpstream prepared)")
    if info.get("firewall_problem"):
        lines.append(f"WARNING: {info['firewall_problem']}")
    if info.get("version") != cli_version:
        lines.append(
            f"note: the daemon runs {info.get('version')} but this command is {cli_version}; "
            "restart it with: sudo systemctl restart vpstream"
        )
    return "\n".join(lines)
