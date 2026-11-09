"""Command line entry point."""

from __future__ import annotations

import argparse
import os
import sys
import tarfile
import time
from collections.abc import Sequence
from pathlib import Path

from vpstream import __version__


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


def _url_host(host: str) -> str:
    """The host as it goes in a URL; a wildcard address becomes a placeholder."""
    if host in ("", "0.0.0.0", "::"):
        return "<server-ip>"
    return f"[{host}]" if ":" in host else host


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
    shown = _url_host(args.host)
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
