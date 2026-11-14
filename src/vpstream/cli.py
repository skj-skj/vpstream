"""Command line entry point."""

from __future__ import annotations

import argparse
import os
import sys
import tarfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from vpstream import __version__, gate, stream, text
from vpstream.net import url_host


def _progress(msg: str) -> None:
    print(f"... {msg}", file=sys.stderr, flush=True)


def _error(msg: str, code: int = 1) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return code


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        port = -1
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"not a port number from 1 to 65535: {text!r}")
    return port


def _writable_file(path: Path) -> str | None:
    """Why `path` cannot be written as an output file, or None when it can."""
    parent = path.parent if str(path.parent) else Path(".")
    if path.is_dir():
        return f"{path} is a folder"
    if not parent.is_dir():
        return f"the folder {parent} does not exist"
    if not os.access(parent, os.W_OK):
        return f"the folder {parent} is not writable"
    return None


def cmd_selftest(args: argparse.Namespace) -> int:
    from vpstream import selftest

    return selftest.run(integration=args.integration, extra=args.pytest_args)


def cmd_bench(args: argparse.Namespace) -> int:
    from vpstream import bench
    from vpstream.tools import SystemRunner

    path = Path(args.file)
    if not path.is_file():
        print(f"not a file: {path}", file=sys.stderr)
        return 2
    presets = tuple(p.strip() for p in args.presets.split(",") if p.strip())
    if not presets:
        print("--presets needs at least one x264 preset", file=sys.stderr)
        return 2
    if args.tmp and not Path(args.tmp).is_dir():
        return _error(f"--tmp folder does not exist: {args.tmp}", 2)
    # Checked first, so a long benchmark is not lost to a bad path at the end.
    problem = _writable_file(Path(args.json)) if args.json else None
    if problem:
        return _error(f"cannot write --json: {problem}", 2)
    try:
        report = bench.benchmark(
            SystemRunner(),
            path,
            sample=args.sample,
            presets=presets,
            sub=args.sub,
            tmp_dir=args.tmp,
            nice=not args.no_nice,
            progress=_progress,
        )
    except (ValueError, RuntimeError, OSError) as exc:  # ToolMissing is a RuntimeError
        return _error(str(exc))
    print(bench.format_report(report))
    if args.json:
        try:
            bench.write_json(report, Path(args.json))
        except OSError as exc:
            return _error(f"cannot write {args.json}: {exc}")
        print(f"\nSaved {args.json}")
    return 0 if all(s.ok or s.skipped for s in report.steps) else 1


def cmd_caps(args: argparse.Namespace) -> int:
    from vpstream.caps import clips, server
    from vpstream.tools import SystemRunner

    out = Path(args.out or time.strftime("caps-results-%Y%m%d-%H%M%S.json"))
    problem = _writable_file(out)
    if problem:
        return _error(f"cannot write the results to {out}: {problem}", 2)
    clip_dir = Path(args.clips) if args.clips else clips.default_cache_dir()
    try:
        manifest = clips.generate(SystemRunner(), clip_dir, regen=args.regen, progress=_progress)
    except (ValueError, RuntimeError, OSError) as exc:  # ClipDirError, ToolMissing, disk
        return _error(str(exc))
    clip_list = manifest.get("clips")
    for c in clip_list if isinstance(clip_list, list) else []:
        if isinstance(c, dict) and c.get("error"):
            print(f"note: {c.get('label')} not available: {c['error']}", file=sys.stderr)
    state = server.CapsState(
        clip_dir=clip_dir, results_path=out, log=lambda m: print(m, flush=True)
    )
    ttl = args.ttl * 60 if args.ttl > 0 else None
    shown = url_host(args.host)
    print(f"Serving the capability page on {args.host}:{args.port}")
    print(f"Open on the TV:  http://{shown}:{args.port}/caps")
    print(f"Results go to {out.resolve()}")
    print(f"Stops after {args.ttl:g} minutes" if ttl else "No time limit; press Ctrl+C to stop")
    print("Remember to close the port afterwards if you opened it in the firewall.", flush=True)
    try:
        server.serve(state, args.host, args.port, ttl)
    except KeyboardInterrupt:
        print("\nstopped")
    except OSError as exc:
        return _error(f"cannot listen on {args.host}:{args.port}: {exc}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from vpstream import doctor
    from vpstream.tools import SystemRunner

    checks = doctor.run_checks(SystemRunner(), status=_status_quietly())
    print(doctor.format_report(checks))
    return doctor.exit_code(checks)


def _dashboard_line(dash: dict[str, Any]) -> str:
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
    return str(device.get("name") or device.get("user_agent") or "-")


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


ACTIVE = ("queued", "running")  # prepare.ACTIVE; not imported, to keep the CLI start quick
KEPT_ACCESS = (
    "The vpstream service keeps its access to your files while they are prepared; take it "
    "back when they are done with: vpstream revoke"
)


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
        "Dashboard:    " + _dashboard_line(dash),
    ]
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


def _daemon(route: str, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Ask the daemon (a change when there is a payload). On failure the reason is printed
    and None is returned."""
    from vpstream import client, config

    try:
        info: dict[str, Any] = client.call(
            config.default_paths().socket, "GET" if payload is None else "POST", route, payload
        )
    except (client.DaemonUnavailable, client.DaemonRefused) as exc:
        _error(str(exc))
        return None
    return info


def cmd_status(args: argparse.Namespace) -> int:
    import json

    info = _daemon("status")
    if info is None:
        return 1
    print(json.dumps(info, indent=2) if args.json else format_status(info, __version__))
    return 0


def _show(info: dict[str, Any] | None, devices: bool = False, clients: bool = False) -> int:
    """Print what a change left behind: the stream, its devices or its browsers."""
    if info is None:
        return 1
    stream = info.get("stream")
    if not stream:
        print("No stream is running.")
    elif clients:
        print(format_clients(stream, float(info.get("now") or 0)))
    elif devices:
        print(f"Policy: {stream.get('policy')}")
        if stream.get("guessers"):
            print(
                "Shut out for trying addresses: " + ", ".join(stream["guessers"]) + "\n"
                "  (let one back in with: vpstream devices enable ADDRESS)"
            )
        print(format_devices(stream, float(info.get("now") or 0)))
    else:
        print(format_stream(stream))
    if info.get("firewall_problem"):
        print(f"WARNING: {info['firewall_problem']}", file=sys.stderr)
    return 0


def _idle(info: dict[str, Any] | None) -> bool:
    """Whether the daemon uses none of the caller's files: no stream and no prepare."""
    if info is None:
        return True
    return not info.get("stream") and not any(
        j.get("state") in ACTIVE for j in info.get("prepare") or []
    )


def _quietly(route: str) -> dict[str, Any] | None:
    """Ask the daemon; None when it does not answer, with nothing printed."""
    from vpstream import client, config

    try:
        info: dict[str, Any] = client.call(config.default_paths().socket, "GET", route)
    except (client.DaemonUnavailable, client.DaemonRefused):
        return None
    return info


def _status_quietly() -> dict[str, Any] | None:
    return _quietly("status")


def _confirm(question: str, yes: bool) -> bool:
    """Ask on the terminal; --yes answers for the caller, and without a terminal the answer
    is no."""
    if yes:
        return True
    if sys.stdin.isatty():
        return _ask(question)
    print(question, file=sys.stderr)
    print("No terminal to answer on; pass --yes to proceed.", file=sys.stderr)
    return False


def _request(
    route: str, kind: str, options: dict[str, Any], yes: bool = False
) -> dict[str, Any] | None:
    """Ask the daemon to start a stream or a prepare of a file or folder. When the service
    cannot read the videos, give it access (an ACL entry for its user alone) and ask once
    more; when it would take long, say what and ask whether to go ahead."""
    from vpstream import access, api, client, config
    from vpstream.tools import SystemRunner, ToolMissing

    socket = config.default_paths().socket
    granted = confirmed = False
    try:
        if _idle(client.call(socket, "GET", "status")):
            _revoke()  # what a stream that ended by itself left behind
        while True:
            try:
                info: dict[str, Any] = client.call(socket, "POST", route, options)
                return info
            except client.DaemonRefused as exc:
                code = exc.data.get("code")
                if code == api.UNREADABLE and not granted:
                    granted = True
                    user = str(exc.data.get("user"))
                    try:
                        changed = access.grant(SystemRunner(), user, kind, Path(options[kind]))
                    except (access.AccessError, ToolMissing) as problem:
                        _error(f"{exc}\n{problem}")
                        return None
                    if not changed:
                        raise
                    print(
                        f"Gave the vpstream service (user {user}) read access to {changed} of "
                        "your files and folders.",
                        file=sys.stderr,
                    )
                elif code == api.CONFIRM and not confirmed:
                    confirmed = True
                    print(f"{exc.data.get('error')}\n{format_plans(exc.data.get('plans') or [])}")
                    if not _confirm("Go ahead?", yes):
                        print("Cancelled; nothing was changed.")
                        return None
                    options = {**options, "confirmed": True}
                else:
                    raise
    except (client.DaemonUnavailable, client.DaemonRefused) as exc:
        _error(str(exc))
        return None


def _source_options(args: argparse.Namespace) -> tuple[str, dict[str, Any]] | None:
    """("file" or "dir", options naming its whole path), or None after saying what is wrong."""
    kind, path = ("file", args.file) if args.file is not None else ("dir", args.dir)
    if not path:
        _error(f"--{kind} needs a path")
        return None
    # The daemon runs elsewhere, so it is told the whole path.
    return kind, {kind: os.path.abspath(path)}


def _follow(ids: set[int], run: Any) -> int:
    """Show how the prepare jobs with these ids, of the daemon run `run`, get on until none
    is queued or running. Fails when one failed, or when jobs were lost: the daemon was
    restarted, or a job left its list unseen. Ctrl+C stops the showing, not the preparing."""
    ended: dict[int, str] = {}  # id -> how it ended, as each is seen to end
    live = sys.stdout.isatty()
    line = ""
    lost = ""
    try:
        while True:
            info = _daemon("status")
            if info is None:
                return 1
            if info.get("prepare_run") != run:
                lost = "the daemon was restarted, and its prepares with it"
                break
            jobs = {j["id"]: j for j in info.get("prepare") or [] if j.get("id") in ids}
            for job in jobs.values():
                if job.get("state") not in ACTIVE and job["id"] not in ended:
                    ended[job["id"]] = str(job.get("state"))
                    print(("\r\x1b[K" if line else "") + format_job(job))
                    line = ""
            if not any(j.get("state") in ACTIVE for j in jobs.values()):
                unseen = ids - set(ended)
                if unseen:
                    lost = f"{len(unseen)} prepares ended unseen; see: vpstream prepared"
                break
            running = [j for j in jobs.values() if j.get("state") == "running"]
            if live and running:
                line = format_job(running[0])
                print(f"\r\x1b[K... {line}", end="", flush=True)
            time.sleep(2)
    except KeyboardInterrupt:
        print(f"\nStill preparing in the background; see: vpstream prepared\n{KEPT_ACCESS}")
        return 0
    if lost:
        _error(lost)
    if _idle(_status_quietly()):
        _revoke()
    return 1 if lost or any(state != "done" for state in ended.values()) else 0


def cmd_start(args: argparse.Namespace) -> int:
    found = _source_options(args)
    if found is None:
        return 2
    kind, options = found
    options.update(mode=args.mode, ttl=args.ttl)
    for name in ("alias", "policy", "audio", "sub"):
        if getattr(args, name):
            options[name] = getattr(args, name)
    info = _request("stream/start", kind, options, args.yes)
    if info is None or not info.get("stream"):
        return _show(info) or 1
    stream = info["stream"]
    print(format_stream(stream))
    print("Dashboard:    " + _dashboard_line(info.get("dashboard") or {}))
    print("Stop it with: vpstream stop")
    ids = set(info.get("jobs") or [])
    if ids:
        print("\nPreparing the videos for browsers; Ctrl+C stops showing it, not preparing:")
        return _follow(ids, info.get("prepare_run"))
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    found = _source_options(args)
    if found is None:
        return 2
    kind, options = found
    if args.sub is not None:
        options["sub"] = args.sub
    info = _request("prepare", kind, options, args.yes)
    if info is None:
        return 1
    plans = info.get("plans") or []
    print(format_plans(plans))
    ids = set(info.get("jobs") or [])
    if not ids:
        print("Nothing to make: what was asked for is prepared already or being prepared.")
        return 0
    if args.no_wait:
        print(f"Queued; see: vpstream prepared\n{KEPT_ACCESS}")
        return 0
    return _follow(ids, info.get("prepare_run"))


def cmd_prepared(args: argparse.Namespace) -> int:
    action = args.action or "list"
    path = os.path.realpath(args.path) if args.path else None
    if action == "list":
        listed = _daemon("prepared")
        info = _daemon("status")
        if listed is None or info is None:
            return 1
        print(format_prepared(listed.get("prepared") or []))
        jobs = [j for j in info.get("prepare") or [] if j.get("state") in (*ACTIVE, "failed")]
        if jobs:
            print("\nPrepares:")
            print("\n".join(f"  {format_job(j)}" for j in jobs))
        return 0
    if action == "cancel":
        done = _daemon("prepare/cancel", {"path": path})
        if done is None:
            return 1
        print(f"Cancelled {done.get('cancelled', 0)} prepares.")
        return 0
    payload: dict[str, Any] = {"path": path} if path else {"stale": args.stale, "all": args.all}
    if not path and args.stale == args.all:
        return _error("give a file or folder, or --stale, or --all", 2)
    if args.all and not _confirm("Remove every prepared video?", args.yes):
        print("Cancelled; nothing was changed.")
        return 1
    done = _daemon("prepared/rm", payload)
    if done is None:
        return 1
    print(f"Removed the prepared versions of {done.get('removed', 0)} videos.")
    kept = done.get("kept") or []
    if kept:
        print("Kept, in use by the stream or a prepare: " + ", ".join(map(str, kept)))
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    from vpstream import config, listing
    from vpstream.tools import SystemRunner, ToolMissing

    if args.folder:
        folder = Path(args.folder)
    else:
        try:
            folder = Path(config.load(config.default_paths().config_file).media_folder)
        except config.ConfigError as exc:
            return _error(str(exc))
    if not folder.is_dir():
        return _error(f"not a folder: {folder}", 2)
    answer = _quietly("prepared")
    cache = None
    if answer is not None:
        rows = answer.get("prepared") or []
        cache = {str(row.get("key")): row for row in rows if isinstance(row, dict)}
    try:
        found = listing.listing(SystemRunner(), folder, cache)
    except (OSError, ToolMissing) as exc:
        return _error(f"cannot list {folder}: {exc}")
    print(listing.format_listing(found, asked=cache is not None))
    return 0


def cmd_track_info(args: argparse.Namespace) -> int:
    from vpstream import probe, tracks
    from vpstream.tools import SystemRunner, ToolMissing

    try:
        info = probe.probe(SystemRunner(), Path(args.file).resolve())
    except (probe.ProbeError, ToolMissing, OSError) as exc:
        return _error(str(exc))
    print(tracks.format_track_info(info))
    return 0


def _revoke() -> int:
    """Take back the file access `start` gave the service. Says so when it did."""
    from vpstream import access
    from vpstream.tools import SystemRunner, ToolMissing

    try:
        count = access.revoke(SystemRunner())
    except (access.AccessError, ToolMissing) as exc:
        return _error(str(exc))
    if count:
        print(f"Took back the vpstream service's access to {count} of your files and folders.")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    info = _daemon("status")
    if info is None:
        return 1
    if info.get("stream"):
        return _error("a stream is running; stop it first: vpstream stop")
    if not _idle(info):
        return _error("videos are being prepared; wait, or cancel: vpstream prepared cancel")
    return _revoke()


def cmd_stop(args: argparse.Namespace) -> int:
    info = _daemon("stream/stop", {})
    # Also after "no stream is running": one that ended by itself left its access behind.
    # Not while prepares still read the files: `revoke`, a later stop or start take it back.
    if _idle(info if info is not None else _status_quietly()):
        _revoke()
    else:
        print(KEPT_ACCESS)
    if info is not None:
        print(f"Stopped. Stream port {info.get('stream_port')}: {info.get('stream_port_state')}")
    return _show(info) if info is None or info.get("firewall_problem") else 0


def cmd_alias(args: argparse.Namespace) -> int:
    return _show(_daemon("stream/alias", {"alias": args.name}))


def cmd_devices(args: argparse.Namespace) -> int:
    action = args.action or "list"
    if action in ("enable", "disable"):
        if not args.ip:
            return _error(f"give the device to {action}: vpstream devices {action} DEVICE", 2)
        info = _daemon("devices", {"device": args.ip, "allowed": action == "enable"})
    elif action == "lock":
        info = _daemon("stream/policy", {"policy": "locked"})
    elif action == "unlock":
        info = _daemon("stream/unlock", {})
    else:
        info = _daemon("status")
    return _show(info, devices=True)


CLIENT_CHANGES = ("audio", "sub", "seek", "start_at", "name")


def cmd_clients(args: argparse.Namespace) -> int:
    if (args.action or "list") == "list":
        if args.devices or args.all:
            return _error("give devices to set: vpstream clients set DEVICE ... --audio 2", 2)
        return _show(_daemon("status"), clients=True)
    if bool(args.devices) == args.all:
        return _error("name the devices to change (see vpstream clients), or give --all", 2)
    payload: dict[str, Any] = {k: getattr(args, k) for k in CLIENT_CHANGES}
    payload = {k: v for k, v in payload.items() if v is not None}
    if not payload:
        return _error("say what to change: --audio, --sub, --seek, --start-at or --name", 2)
    if args.video is not None:
        payload["video"] = args.video
    payload.update({"all": True} if args.all else {"devices": args.devices})
    return _show(_daemon("clients", payload), clients=True)


def cmd_daemon(args: argparse.Namespace) -> int:
    from vpstream import config, daemon, firewall
    from vpstream.tools import SystemRunner

    paths = config.default_paths()
    runner = SystemRunner()
    # The helper always works on the real host, so a daemon tried out under $VPSTREAM_ROOT
    # gets none: it must not close the real stream port.
    trial = bool(os.environ.get(config.ROOT_ENV))
    helper = firewall.helper_path()
    fw = None if trial else firewall.SudoFirewall(runner, helper)
    dash_fw = None if trial else firewall.SudoFirewall(runner, helper, firewall.DASHBOARD)
    try:
        return daemon.run(config.load(paths.config_file), paths, runner, fw, dash_fw)
    except (config.ConfigError, daemon.DaemonError) as exc:
        return _error(str(exc))
    except OSError as exc:  # the run folder cannot be made, for example
        return _error(f"the daemon cannot start: {exc}")


def _service(args: argparse.Namespace) -> Any:
    from vpstream import service
    from vpstream.tools import SystemRunner

    return service.Service(SystemRunner(), sudo_user=os.environ.get("SUDO_USER"))


def _password(args: argparse.Namespace, ask: bool) -> str | None:
    """The dashboard password from --dash-pass, --dash-pass-file (- for standard input), or
    asked twice on the terminal when `ask`. None when there is none to take. Raises
    ValueError with what is wrong."""
    if args.dash_pass is not None:
        print(
            "note: a password given on the command line stays in your shell history; "
            "--dash-pass-file or the prompt keep it out",
            file=sys.stderr,
        )
        return str(args.dash_pass)
    if args.dash_pass_file is not None:
        try:
            if args.dash_pass_file == "-":
                first = sys.stdin.readline()
            else:
                with open(args.dash_pass_file, encoding="utf-8") as fh:
                    first = fh.readline()
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"cannot read the password file: {exc}") from exc
        password = first.rstrip("\r\n")
        if not password:
            raise ValueError("the password file's first line is empty")
        return password
    if not ask or not sys.stdin.isatty():
        return None
    import getpass

    password = getpass.getpass("New dashboard password: ")
    if getpass.getpass("The same again: ") != password:
        raise ValueError("the two passwords are not the same")
    return password


def cmd_init(args: argparse.Namespace) -> int:
    from vpstream import service

    svc = _service(args)
    try:
        public = svc.dashboard_public(args.public)
        given = args.dash_pass is not None or args.dash_pass_file is not None
        if given and not public:
            return _error("a password is for the public dashboard only (--no-tailscale)", 2)
        # Not asked for a password that a refusal for want of root would throw away.
        ask = os.geteuid() == 0 and svc.wants_password(public)
        svc.init(public, _password(args, ask))
    except ValueError as exc:
        return _error(str(exc))
    except (service.ServiceError, OSError) as exc:
        return _error(str(exc))
    return 0


def cmd_dash_pass(args: argparse.Namespace) -> int:
    from vpstream import service

    if os.geteuid() != 0:
        return _error("this needs root: sudo vpstream dash-pass")
    try:
        password = _password(args, ask=True)
        if password is None:
            return _error("no terminal to ask on; give the password with --dash-pass-file FILE", 2)
        _service(args).set_password(password)
    except ValueError as exc:
        return _error(str(exc))
    except (service.ServiceError, OSError) as exc:
        return _error(str(exc))
    return 0


def cmd_dash_unblock(args: argparse.Namespace) -> int:
    if _daemon("dashboard/unblock", {}) is None:
        return 1
    print("Failed logins are forgotten: the dashboard takes logins from everyone again.")
    return 0


def cmd_dashboard_cert(args: argparse.Namespace) -> int:
    info = _daemon("certificate")
    if info is None:
        return 1
    print(f"SHA-256 {info['fingerprint']}", file=sys.stderr)
    print(info["pem"], end="" if info["pem"].endswith("\n") else "\n")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    from vpstream import service

    what = "the vpstream service, its user, configuration and state"
    what += ", and every prepared video" if args.purge else ""
    if os.geteuid() == 0 and not args.yes:
        if not sys.stdin.isatty():
            return _error("no terminal to ask on; pass --yes to proceed", 2)
        if not _ask(f"Remove {what} from this host?"):
            print("Cancelled; nothing was changed.")
            return 1
    try:
        _service(args).uninstall(purge=args.purge)
    except (service.ServiceError, OSError) as exc:
        return _error(str(exc))
    return 0


def _ask(question: str) -> bool:
    try:
        answer = input(f"{question} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def cmd_install(args: argparse.Namespace) -> int:
    from vpstream import ffmpeg_install
    from vpstream.tools import SystemRunner

    if args.tmp and not Path(args.tmp).is_dir():
        return _error(f"--tmp folder does not exist: {args.tmp}", 2)
    setup = ffmpeg_install.Setup(
        runner=SystemRunner(),
        http=ffmpeg_install.UrlHttp(),
        confirm=lambda question: _confirm(question, args.yes),
        progress=_progress,
        tmp_dir=args.tmp,
    )
    try:
        print(setup.run(update=args.command == "update"))
    except ffmpeg_install.Cancelled:
        print("Cancelled; nothing was changed.")
        return 1
    except (ffmpeg_install.SetupError, OSError, tarfile.TarError) as exc:
        return _error(str(exc))
    return 0


def _password_options(parser: argparse.ArgumentParser) -> None:
    given = parser.add_mutually_exclusive_group()
    given.add_argument(
        "--dash-pass", metavar="PASSWORD", help="the dashboard password (stays in shell history)"
    )
    given.add_argument(
        "--dash-pass-file",
        metavar="FILE",
        help="read the password from a file's first line (- for stdin)",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vpstream", description="On-demand video streaming from a VPS."
    )
    p.add_argument("--version", action="version", version=f"vpstream {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    st = sub.add_parser("selftest", help="run the tests shipped with the package")
    st.add_argument(
        "--integration",
        action="store_true",
        help="also run tests that need ffmpeg and other real tools on this host",
    )
    st.add_argument(
        "pytest_args", nargs=argparse.REMAINDER, help="extra pytest arguments, after --"
    )
    st.set_defaults(func=cmd_selftest)

    d = sub.add_parser("doctor", help="check this host for the programs vpstream needs")
    d.set_defaults(func=cmd_doctor)

    ini = sub.add_parser(
        "init", help="set the service up on this host (as root; safe to run again)"
    )
    where = ini.add_mutually_exclusive_group()
    where.add_argument(
        "--no-tailscale",
        dest="public",
        action="store_const",
        const=True,
        help="put the dashboard on the internet, over HTTPS with a password",
    )
    where.add_argument(
        "--tailscale",
        dest="public",
        action="store_const",
        const=False,
        help="put the dashboard back on the tailnet only (the default)",
    )
    _password_options(ini)
    ini.set_defaults(func=cmd_init, public=None)
    dp = sub.add_parser("dash-pass", help="set a new password for the public dashboard (as root)")
    _password_options(dp)
    dp.set_defaults(func=cmd_dash_pass)
    sub.add_parser(
        "dashboard-cert", help="print the public dashboard's certificate authority, to install"
    ).set_defaults(func=cmd_dashboard_cert)
    sub.add_parser(
        "dash-unblock", help="forget the public dashboard's failed logins, to log in again"
    ).set_defaults(func=cmd_dash_unblock)
    un = sub.add_parser("uninstall", help="remove what init created (as root)")
    un.add_argument("--purge", action="store_true", help="also remove the prepared videos")
    un.add_argument("--yes", action="store_true", help="do not ask first")
    un.set_defaults(func=cmd_uninstall)

    s = sub.add_parser("status", help="show what the running daemon is doing")
    s.add_argument("--json", action="store_true", help="print the daemon's answer as JSON")
    s.set_defaults(func=cmd_status)

    go = sub.add_parser("start", help="start a stream of one video or a folder of videos")
    source = go.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", metavar="PATH", help="the video to stream")
    source.add_argument("--dir", metavar="PATH", help="a folder; its videos get numbers")
    go.add_argument("--mode", choices=stream.MODES, default=stream.DIRECT, help="(default direct)")
    go.add_argument("--alias", help="the short name in the address (default: 3 random letters)")
    go.add_argument(
        "--ttl",
        type=float,
        default=stream.DEFAULT_TTL_MINUTES,
        metavar="MINUTES",
        help="stop by itself after this many minutes (default 180; 0 = no limit)",
    )
    go.add_argument(
        "--policy",
        choices=gate.POLICIES,
        help="who gets in (default: open in direct mode, approval in browser mode)",
    )
    go.add_argument(
        "--audio",
        metavar="TRACK",
        help="browser mode: the audio track to start with (number or language)",
    )
    go.add_argument(
        "--sub",
        metavar="TRACK",
        help="browser mode: the prepared subtitles to start with (number, language or off)",
    )
    go.add_argument("--yes", action="store_true", help="do not ask before a long transcode")
    go.set_defaults(func=cmd_start)

    pr = sub.add_parser("prepare", help="make the versions browser mode plays, ahead of time")
    pick = pr.add_mutually_exclusive_group(required=True)
    pick.add_argument("--file", metavar="PATH", help="the video to prepare")
    pick.add_argument("--dir", metavar="PATH", help="a folder; every video at its top level")
    pr.add_argument(
        "--sub",
        metavar="TRACKS",
        help="subtitle tracks: numbers from track-info or language codes, such as 1,3 or eng, "
        "or none (default: every plain text track, as soft subtitles)",
    )
    pr.add_argument("--yes", action="store_true", help="do not ask when it takes long")
    pr.add_argument("--no-wait", action="store_true", help="queue it and return at once")
    pr.set_defaults(func=cmd_prepare)

    pd = sub.add_parser("prepared", help="list or remove prepared videos, or cancel prepares")
    pd.add_argument("action", nargs="?", choices=["list", "rm", "cancel"], help="(default list)")
    pd.add_argument("path", nargs="?", help="for rm and cancel: a file or a folder")
    pd.add_argument("--stale", action="store_true", help="rm: those of changed or gone files")
    pd.add_argument("--all", action="store_true", help="rm: every prepared video")
    pd.add_argument("--yes", action="store_true", help="rm --all: do not ask first")
    pd.set_defaults(func=cmd_prepared)

    ls = sub.add_parser("ls", help="list a folder's videos, their tracks and what is prepared")
    ls.add_argument("folder", nargs="?", help="(default: [media] folder in the configuration)")
    ls.set_defaults(func=cmd_ls)

    ti = sub.add_parser("track-info", help="list a file's tracks with the numbers --sub takes")
    ti.add_argument("file")
    ti.set_defaults(func=cmd_track_info)
    sub.add_parser("stop", help="stop the stream and close its port").set_defaults(func=cmd_stop)
    sub.add_parser(
        "revoke", help="take back the file access `start` gave the service (stop does it too)"
    ).set_defaults(func=cmd_revoke)
    al = sub.add_parser("alias", help="change the short name of the running stream")
    al.add_argument("name")
    al.set_defaults(func=cmd_alias)
    dv = sub.add_parser("devices", help="list the devices of the stream, or let them in or out")
    dv.add_argument(
        "action",
        nargs="?",
        choices=["list", "enable", "disable", "lock", "unlock"],
        help="(default list)",
    )
    dv.add_argument(
        "ip", nargs="?", metavar="DEVICE", help="for enable and disable: its id or IP address"
    )
    dv.set_defaults(func=cmd_devices)
    cl = sub.add_parser(
        "clients",
        help="list the browsers of a browser stream, or set their audio, subtitles, position "
        "or name",
    )
    cl.add_argument("action", nargs="?", choices=["list", "set"], help="(default list)")
    cl.add_argument("devices", nargs="*", metavar="DEVICE", help="for set: device ids")
    cl.add_argument("--all", action="store_true", help="for set: every browser that gets in")
    cl.add_argument("--audio", help="an audio track number, or off")
    cl.add_argument("--sub", help="subtitles such as 2-soft or 3-burn, or off")
    cl.add_argument("--seek", help="jump there now: seconds, or a time such as 10:10")
    cl.add_argument(
        "--start-at", dest="start_at", help="where the next load of the video starts, once"
    )
    cl.add_argument("--video", type=int, help="for --start-at: which video (default: its own)")
    cl.add_argument("--name", help="what to call the device on the dashboard")
    cl.set_defaults(func=cmd_clients)

    dm = sub.add_parser("daemon", help="run the daemon in the foreground (the service does this)")
    dm.set_defaults(func=cmd_daemon)

    b = sub.add_parser("bench", help="measure how fast this host prepares a file")
    b.add_argument("file")
    b.add_argument(
        "--sample",
        type=float,
        default=120.0,
        help="seconds of the file to encode for each step (default 120; 0 = all)",
    )
    b.add_argument(
        "--presets",
        default="veryfast,medium",
        help="x264 presets to try, comma separated (default veryfast,medium)",
    )
    b.add_argument(
        "--sub",
        type=int,
        default=None,
        help="subtitle track number to burn in (default: first image or ASS track)",
    )
    b.add_argument("--json", metavar="PATH", help="also write the results as JSON")
    b.add_argument("--tmp", metavar="DIR", help="folder for temporary output (default: system)")
    b.add_argument(
        "--no-nice", action="store_true", help="run at normal priority instead of nice and ionice"
    )
    b.set_defaults(func=cmd_bench)

    c = sub.add_parser("caps", help="serve a capability test page to a TV browser")
    c.add_argument("--host", default="0.0.0.0", help="address to listen on (default 0.0.0.0)")
    c.add_argument("--port", type=_port, default=1337, help="port to listen on (default 1337)")
    c.add_argument(
        "--ttl",
        type=float,
        default=60,
        help="stop after this many minutes (default 60; 0 = no limit)",
    )
    c.add_argument("--out", metavar="PATH", help="results file (default caps-results-*.json)")
    c.add_argument("--clips", metavar="DIR", help="folder for the generated test clips")
    c.add_argument("--regen", action="store_true", help="make the test clips again")
    c.set_defaults(func=cmd_caps)

    for name, about in (
        ("install", "install a program vpstream needs (ffmpeg: the newest stable static build)"),
        ("update", "update a program installed by `vpstream install`"),
    ):
        i = sub.add_parser(name, help=about)
        i.add_argument("program", choices=["ffmpeg"], help="what to install (ffmpeg)")
        i.add_argument(
            "--yes", action="store_true", help="do not ask before replacing an existing ffmpeg"
        )
        i.add_argument("--tmp", metavar="DIR", help="folder for the download (default: system)")
        i.set_defaults(func=cmd_install)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    if args.command == "selftest" and args.pytest_args[:1] == ["--"]:
        args.pytest_args = args.pytest_args[1:]
    return int(args.func(args))
