"""The command line on Windows: a few commands, and the window for everything else.

`vpstream` alone opens the window. `doctor`, `selftest` and `--version` work as elsewhere,
and `windows-install` / `windows-uninstall` add and remove the programs vpstream downloads.
Every other command is for Linux and Termux, where a daemon runs: on Windows the window
does what they do.
"""

from __future__ import annotations

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
