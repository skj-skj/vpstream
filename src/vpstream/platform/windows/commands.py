"""The command line on Windows: a few commands, and the window for everything else.

`vpstream` alone opens the window. `doctor`, `selftest` and `--version` work as elsewhere,
and `windows-install` / `windows-uninstall` add and remove the programs vpstream downloads.
Every other command is for Linux and Termux, where a daemon runs: on Windows the window
does what they do.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from collections.abc import Callable

from vpstream.platform import windows

WORKS = frozenset({"selftest", "doctor", "windows-install", "windows-uninstall"})


def refusal(command: str | None) -> str | None:
    """What a command that does not work on Windows says instead, or None."""
    if command is None or command in WORKS:
        return None
    return (
        f"`vpstream {command}` is for Linux and Termux. On Windows, use {windows.WINDOW}. "
        f"In a terminal: vpstream doctor, vpstream selftest, {windows.INSTALL}, "
        "vpstream windows-uninstall, vpstream --version"
    )


def doctor() -> int:
    from vpstream import doctor as checks
    from vpstream.tools import SystemRunner

    found = checks.run_checks(SystemRunner())
    print(checks.format_report(found))
    return checks.exit_code(found)


def open_window() -> int:
    """`vpstream` with no command."""
    print("The vpstream window is not in this version yet; see vpstream doctor.")
    return 2


def add_parsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """The commands only Windows has (Linux's `--help` stays as it was)."""
    from vpstream.platform.windows import install

    i = sub.add_parser(
        "windows-install",
        help="download and install the programs vpstream needs, and the desktop icon",
    )
    i.add_argument(
        "programs",
        nargs="*",
        metavar="PROGRAM",
        help=f"{', '.join(install.PROGRAMS)} (default: every one that is missing)",
    )
    i.add_argument("--yes", action="store_true", help="do not ask first")
    i.set_defaults(func=cmd_install)
    u = sub.add_parser(
        "windows-uninstall",
        help="remove the programs vpstream installed and its desktop icon",
    )
    u.add_argument("--yes", action="store_true", help="do not ask first")
    u.set_defaults(func=cmd_uninstall)


class Printed:
    """Progress on the terminal: a line for each tenth of a download."""

    def __init__(self) -> None:
        self.shown: tuple[str, int] = ("", -1)

    def __call__(self, what: str, done: int, total: int) -> None:
        tenth = done * 10 // total if total else 0
        if (what, tenth) == self.shown:
            return
        self.shown = (what, tenth)
        if total and done:
            print(f"... {what}: {done // 1_000_000} of {total // 1_000_000} MB", flush=True)
        elif not done:
            print(f"... {what}", flush=True)


def cmd_install(
    args: argparse.Namespace, confirm: Callable[[str, bool], bool] | None = None
) -> int:
    from vpstream import cli
    from vpstream.platform.common import SetupError, UrlHttp
    from vpstream.platform.windows import install, shortcut
    from vpstream.tools import SystemRunner

    confirm = confirm or cli._confirm
    unknown = [p for p in args.programs if p not in install.PROGRAMS]
    if unknown:
        return cli._error(
            f"cannot install {', '.join(unknown)}: choose from {', '.join(install.PROGRAMS)}", 2
        )
    runner, tools = SystemRunner(), windows.tools_dir()
    names = list(dict.fromkeys(args.programs)) or install.missing(runner)
    if install.MKVTOOLNIX in names and args.programs and runner.which("mkvmerge"):
        print(f"MKVToolNix is already installed: {runner.which('mkvmerge')}")
        names.remove(install.MKVTOOLNIX)
    if names:
        question = f"vpstream will download and install:\n{install.plan_text(names, tools)}\nGo on?"
        if not confirm(question, args.yes):
            print("Cancelled; nothing was changed.")
            return 1
    failed = 0
    progress = Printed()
    for name in names:
        try:
            if name == install.MKVTOOLNIX:
                done = install.install_mkvtoolnix(runner, UrlHttp(), tools, progress)
            else:
                done = install.install_build(
                    install.BUILDS[name], tools, runner, UrlHttp(), progress
                )
            print(done)
        except (SetupError, OSError, zipfile.BadZipFile) as exc:
            print(f"error: {name}: {exc}", file=sys.stderr)
            failed += 1
    if not names:
        print("Every program vpstream needs is there.")
    try:
        print(f"The desktop icon is at {shortcut.create(runner)}")
    except SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        failed += 1
    return 1 if failed else 0


def cmd_uninstall(
    args: argparse.Namespace, confirm: Callable[[str, bool], bool] | None = None
) -> int:
    from vpstream import cli, config
    from vpstream.platform.common import SetupError
    from vpstream.platform.windows import install, shortcut
    from vpstream.tools import SystemRunner

    confirm = confirm or cli._confirm
    tools = windows.tools_dir()
    question = f"Remove the ffmpeg and Caddy vpstream installed ({tools}) and the desktop icon?"
    if not confirm(question, args.yes):
        print("Cancelled; nothing was changed.")
        return 1
    runner = SystemRunner()
    try:
        removed = install.uninstall(tools)
        icon = shortcut.remove(runner)
    except (SetupError, OSError) as exc:
        return cli._error(str(exc))
    for what in removed:
        print(f"removed {what}")
    if icon:
        print(f"removed the desktop icon ({icon})")
    if not removed and not icon:
        print("vpstream had installed nothing here.")
    paths = config.default_paths()
    print(
        "Kept: your settings and prepared videos "
        f"({paths.config_file.parent}, {paths.cache_dir.parent}); delete them by hand if you "
        "like. MKVToolNix stays installed (it is not vpstream's: `winget uninstall "
        f"{install.WINGET_ID}` removes it). `pip uninstall vpstream` removes vpstream."
    )
    return 0
