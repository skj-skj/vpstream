"""The desktop icon: a shortcut that opens the vpstream window.

It runs pythonw.exe (Python without a console window) with `-P -m vpstream`: -P keeps the
folder Windows starts it in off sys.path. Windows makes shortcuts through its WScript.Shell
object, which PowerShell reaches. A shortcut is vpstream's when it runs `-m vpstream`;
uninstalling removes only that one.
"""

from __future__ import annotations

import sys
from pathlib import Path

from vpstream.platform.common import SetupError
from vpstream.tools import Runner, tail

NAME = "vpstream.lnk"
ARGUMENTS = "-P -m vpstream"
DESCRIPTION = "vpstream: stream videos to the phones and TVs at home"
ICON = Path(__file__).with_name("vpstream.ico")


def pythonw(executable: str = sys.executable) -> Path:
    """The Python that shows no console window, next to the one running this."""
    return Path(executable).with_name("pythonw.exe")


def quote(text: str | Path) -> str:
    """A PowerShell string that is `text` exactly: nothing in '...' is expanded."""
    return "'" + str(text).replace("'", "''") + "'"


DESKTOP = "$path = Join-Path ([Environment]::GetFolderPath('Desktop')) " + quote(NAME)


def create_script(target: Path, icon: Path = ICON) -> str:
    return "; ".join(
        [
            DESKTOP,
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($path)",
            f"$s.TargetPath = {quote(target)}",
            f"$s.Arguments = {quote(ARGUMENTS)}",
            f"$s.IconLocation = {quote(f'{icon},0')}",
            "$s.WorkingDirectory = $HOME",
            f"$s.Description = {quote(DESCRIPTION)}",
            "$s.Save()",
            "$path",
        ]
    )


# Removes the shortcut only when it is vpstream's; prints its path when it did.
REMOVE_SCRIPT = "; ".join(
    [
        DESKTOP,
        "if (Test-Path -LiteralPath $path) {"
        " $s = (New-Object -ComObject WScript.Shell).CreateShortcut($path);"
        f" if ($s.Arguments -eq {quote(ARGUMENTS)}) {{ Remove-Item -LiteralPath $path; $path }} }}",
    ]
)


# PowerShell 5.1 writes in the console's code page, which mangles a path like C:\Users\José;
# vpstream reads what programs write as UTF-8.
UTF8 = "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "


def powershell(script: str) -> list[str]:
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", UTF8 + script]


def create(runner: Runner, target: Path | None = None) -> str:
    """Put the icon on the desktop (again: an old one is replaced). Returns its path."""
    res = runner.run(powershell(create_script(target or pythonw())), timeout=60)
    if res.returncode != 0:
        raise SetupError(f"the desktop icon could not be made: {tail(res)}")
    return res.stdout.strip()


def remove(runner: Runner) -> str:
    """Remove vpstream's desktop icon. Returns its path, or "" when there was none."""
    res = runner.run(powershell(REMOVE_SCRIPT), timeout=60)
    if res.returncode != 0:
        raise SetupError(f"the desktop icon could not be removed: {tail(res)}")
    return res.stdout.strip()
