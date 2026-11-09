"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from vpstream import __version__


def _progress(msg: str) -> None:
    print(f"... {msg}", file=sys.stderr, flush=True)


def cmd_selftest(args: argparse.Namespace) -> int:
    from vpstream import selftest

    return selftest.run(integration=args.integration, extra=args.pytest_args)


def cmd_bench(args: argparse.Namespace) -> int:
    from vpstream import bench
    from vpstream.tools import SystemRunner, ToolMissing

    path = Path(args.file)
    if not path.is_file():
        print(f"not a file: {path}", file=sys.stderr)
        return 2
    presets = tuple(p.strip() for p in args.presets.split(",") if p.strip())
    if not presets:
        print("--presets needs at least one x264 preset", file=sys.stderr)
        return 2
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
    except (ToolMissing, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(bench.format_report(report))
    if args.json:
        bench.write_json(report, Path(args.json))
        print(f"\nSaved {args.json}")
    return 0 if all(s.ok or s.skipped for s in report.steps) else 1


def cmd_caps(args: argparse.Namespace) -> int:
    from vpstream.caps import clips, server
    from vpstream.tools import SystemRunner, ToolMissing

    clip_dir = Path(args.clips) if args.clips else clips.default_cache_dir()
    try:
        manifest = clips.generate(SystemRunner(), clip_dir, regen=args.regen, progress=_progress)
    except ToolMissing as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    clip_list = manifest.get("clips")
    for c in clip_list if isinstance(clip_list, list) else []:
        if isinstance(c, dict) and c.get("error"):
            print(f"note: {c.get('label')} not available: {c['error']}", file=sys.stderr)
    out = Path(args.out or time.strftime("caps-results-%Y%m%d-%H%M%S.json"))
    state = server.CapsState(
        clip_dir=clip_dir, results_path=out, log=lambda m: print(m, flush=True)
    )
    ttl = args.ttl * 60 if args.ttl > 0 else None
    shown = "<server-ip>" if args.host in ("0.0.0.0", "::") else args.host
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
        print(f"error: cannot listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1
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
    c.add_argument("--port", type=int, default=1337, help="port to listen on (default 1337)")
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
