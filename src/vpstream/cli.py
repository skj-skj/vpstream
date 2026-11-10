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

from vpstream import __version__, gate, stream
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

    checks = doctor.run_checks(SystemRunner())
    print(doctor.format_report(checks))
    return doctor.exit_code(checks)


def _duration(seconds: float) -> str:
    """A short human duration: 45s, 3m 12s, 2h 05m, 3d 4h."""
    total = int(seconds)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _dashboard_line(dash: dict[str, Any]) -> str:
    if dash.get("url"):
        return f"{dash['url']} (Tailscale only)"
    if dash.get("address"):
        return f"http://{dash['address']}/ on this host only; {dash.get('problem')}"
    return str(dash.get("problem"))


def _size(count: float) -> str:
    for unit in ("B", "kB", "MB", "GB"):
        if count < 1000 or unit == "GB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1000
    raise AssertionError("unreachable")


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
    rows = [("ADDRESS", "STATE", "REQUESTS", "BLOCKED", "SENT", "LAST SEEN", "PLAYER")]
    for d in devices:
        seen = d.get("last_seen") or 0
        rows.append(
            (
                str(d.get("ip")),
                device_state(d),
                str(d.get("requests", 0)),
                str(d.get("blocked", 0)),
                _size(float(d.get("bytes") or 0)),
                f"{_duration(max(0.0, now - seen))} ago" if seen else "never",
                str(d.get("user_agent") or "-")[:60],
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    return "\n".join(
        "  ".join(
            [*(cell.ljust(w) for cell, w in zip(row, widths, strict=False)), row[-1]]
        ).rstrip()
        for row in rows
    )


def format_stream(stream: dict[str, Any]) -> str:
    """The running stream: what it is and the address of every video."""
    left = stream.get("seconds_left")
    limit = "no time limit" if left is None else f"stops in {_duration(float(left))}"
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
        lines += [f"  {v['number']:>{width}}  {v['url']}  {v['name']}" for v in videos]
    else:
        lines.append(f"Address:      {stream.get('url')}")
    return "\n".join(lines)


def format_status(info: dict[str, Any], cli_version: str) -> str:
    """The daemon's status as text for `vpstream status`."""
    dash = info.get("dashboard") or {}
    stream = info.get("stream")
    lines = [
        f"vpstream daemon {info.get('version')}, "
        f"up {_duration(float(info.get('uptime_seconds') or 0))} (pid {info.get('pid')})",
        format_stream(stream) if stream else "Stream:       none",
        f"Stream port:  {info.get('stream_port')} ({info.get('stream_port_state', 'unknown')})",
        "Dashboard:    " + _dashboard_line(dash),
    ]
    if stream:
        count = len(stream.get("devices") or [])
        lines.append(f"Devices:      {count} (see: vpstream devices)")
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


def _show(info: dict[str, Any] | None, devices: bool = False) -> int:
    """Print what a change left behind: the stream, or its devices."""
    if info is None:
        return 1
    stream = info.get("stream")
    if not stream:
        print("No stream is running.")
    elif devices:
        print(f"Policy: {stream.get('policy')}")
        print(format_devices(stream, float(info.get("now") or 0)))
    else:
        print(format_stream(stream))
    if info.get("firewall_problem"):
        print(f"WARNING: {info['firewall_problem']}", file=sys.stderr)
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    options: dict[str, Any] = {"mode": args.mode, "ttl": args.ttl}
    # The daemon runs elsewhere, so it is told the whole path.
    options["file" if args.file else "dir"] = os.path.abspath(args.file or args.dir)
    for name in ("alias", "policy"):
        if getattr(args, name):
            options[name] = getattr(args, name)
    info = _daemon("stream/start", options)
    if info is not None and info.get("stream"):
        dash = info.get("dashboard") or {}
        print(format_stream(info["stream"]))
        print("Dashboard:    " + _dashboard_line(dash))
        print("Stop it with: vpstream stop")
        return 0
    return _show(info) or 1


def cmd_stop(args: argparse.Namespace) -> int:
    info = _daemon("stream/stop", {})
    if info is not None:
        print(f"Stopped. Stream port {info.get('stream_port')}: {info.get('stream_port_state')}")
    return _show(info) if info is None or info.get("firewall_problem") else 0


def cmd_alias(args: argparse.Namespace) -> int:
    return _show(_daemon("stream/alias", {"alias": args.name}))


def cmd_devices(args: argparse.Namespace) -> int:
    action = args.action or "list"
    if action in ("enable", "disable"):
        if not args.ip:
            return _error(f"give the address to {action}: vpstream devices {action} ADDRESS", 2)
        info = _daemon("devices", {"ip": args.ip, "allowed": action == "enable"})
    elif action == "lock":
        info = _daemon("stream/policy", {"policy": "locked"})
    elif action == "unlock":
        info = _daemon("stream/unlock", {})
    else:
        info = _daemon("status")
    return _show(info, devices=True)


def cmd_daemon(args: argparse.Namespace) -> int:
    from vpstream import config, daemon, firewall
    from vpstream.tools import SystemRunner

    paths = config.default_paths()
    runner = SystemRunner()
    # The helper always works on the real host, so a daemon tried out under $VPSTREAM_ROOT
    # gets none: it must not close the real stream port.
    trial = bool(os.environ.get(config.ROOT_ENV))
    fw = None if trial else firewall.SudoFirewall(runner, firewall.helper_path())
    try:
        return daemon.run(config.load(paths.config_file), paths, runner, fw)
    except (config.ConfigError, daemon.DaemonError) as exc:
        return _error(str(exc))
    except OSError as exc:  # the run folder cannot be made, for example
        return _error(f"the daemon cannot start: {exc}")


def _service(args: argparse.Namespace) -> Any:
    from vpstream import service
    from vpstream.tools import SystemRunner

    return service.Service(SystemRunner(), sudo_user=os.environ.get("SUDO_USER"))


def cmd_init(args: argparse.Namespace) -> int:
    from vpstream import service

    try:
        _service(args).init()
    except (service.ServiceError, OSError) as exc:
        return _error(str(exc))
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
    if args.yes:
        confirm = lambda question: True  # noqa: E731
    elif sys.stdin.isatty():
        confirm = _ask
    else:

        def confirm(question: str) -> bool:
            print(question, file=sys.stderr)
            print("No terminal to answer on; pass --yes to proceed.", file=sys.stderr)
            return False

    setup = ffmpeg_install.Setup(
        runner=SystemRunner(),
        http=ffmpeg_install.UrlHttp(),
        confirm=confirm,
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

    sub.add_parser(
        "init", help="set the service up on this host (as root; safe to run again)"
    ).set_defaults(func=cmd_init)
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
        "--policy", choices=gate.POLICIES, help="who gets in (default for direct mode: open)"
    )
    go.set_defaults(func=cmd_start)
    sub.add_parser("stop", help="stop the stream and close its port").set_defaults(func=cmd_stop)
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
    dv.add_argument("ip", nargs="?", metavar="ADDRESS", help="for enable and disable")
    dv.set_defaults(func=cmd_devices)

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

    for name, text in (
        ("install", "install a program vpstream needs (ffmpeg: the newest stable static build)"),
        ("update", "update a program installed by `vpstream install`"),
    ):
        i = sub.add_parser(name, help=text)
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
