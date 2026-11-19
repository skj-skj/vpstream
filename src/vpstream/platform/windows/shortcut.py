"""The desktop icon, and the program it starts: vpstream.exe.

vpstream.exe (in %LOCALAPPDATA%\\vpstream) is the launcher pip makes for a script, without a
console window, that runs vpstream with this Python's pythonw.exe. It is there so that
Windows names vpstream, not Python, when it asks for administrator rights. Without it (no
pip), the icon runs pythonw.exe with `-P -m vpstream`: -P keeps the folder Windows starts
it in off sys.path.

Windows makes shortcuts through its WScript.Shell object, which PowerShell reaches. A
shortcut is vpstream's when it runs vpstream.exe or `-m vpstream`; uninstalling removes
only that one.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import zipfile
from pathlib import Path

from vpstream.platform.common import SetupError
from vpstream.tools import Runner, tail

NAME = "vpstream.lnk"
ARGUMENTS = "-P -m vpstream"
DESCRIPTION = "vpstream: stream videos to the phones and TVs at home"
ICON = Path(__file__).with_name("vpstream.ico")


LAUNCHER = "vpstream.exe"
MAIN = "import sys\nfrom vpstream.cli import main\n\nsys.exit(main())\n"


def pythonw(executable: str = sys.executable) -> Path:
    """The Python that shows no console window, next to the one running this."""
    return Path(executable).with_name("pythonw.exe")


def launcher_bytes(python: Path) -> bytes | None:
    """vpstream.exe: pip's window launcher, the Python it runs, and what that runs (a zip
    with __main__.py), as pip lays out a script. None without pip."""
    from importlib.resources import files

    try:
        stub = files("pip._vendor.distlib").joinpath("w64.exe").read_bytes()
    except (ImportError, OSError):
        return None
    packed = io.BytesIO()
    with zipfile.ZipFile(packed, "w") as zf:
        # A fixed time: the same Python gives the same bytes, so it is not written again.
        zf.writestr(zipfile.ZipInfo("__main__.py", date_time=(2026, 1, 1, 0, 0, 0)), MAIN)
    return stub + f'#!"{python}"\n'.encode() + packed.getvalue()


def launcher(folder: Path | None = None, python: Path | None = None) -> Path | None:
    """vpstream.exe, made or made again when this Python is another; None when it cannot
    be (no pip, a folder it cannot write)."""
    from vpstream.platform.windows import app_dirs

    path = (folder or app_dirs()[1]) / LAUNCHER
    data = launcher_bytes(python or pythonw())
    if data is None:
        return path if path.is_file() else None
    with contextlib.suppress(OSError):
        if path.read_bytes() == data:
            return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        new = path.with_suffix(".new")
        new.write_bytes(data)
        os.replace(new, path)  # fails while it runs: the one there runs vpstream as well
    except OSError:
        return path if path.is_file() else None
    return path


def launch(folder: Path | None = None) -> tuple[Path, str]:
    """What the icon and the elevated window run: vpstream.exe, else pythonw.exe."""
    found = launcher(folder)
    return (found, "") if found else (pythonw(), ARGUMENTS)


def quote(text: str | Path) -> str:
    """A PowerShell string that is `text` exactly: nothing in '...' is expanded."""
    return "'" + str(text).replace("'", "''") + "'"


OURS = quote("*\\vpstream\\" + LAUNCHER)  # where vpstream.exe is: %LOCALAPPDATA%\vpstream
IS_OURS = f"($s.Arguments -eq {quote(ARGUMENTS)} -or $s.TargetPath -like {OURS})"
DESKTOP = "$path = Join-Path ([Environment]::GetFolderPath('Desktop')) " + quote(NAME)


def create_script(
    target: Path, arguments: str = ARGUMENTS, icon: Path = ICON, only_ours: bool = False
) -> str:
    """Make the icon; with `only_ours`, only bring vpstream's up to date where it is (one
    the person removed stays removed)."""
    keep = f"if (-not ((Test-Path -LiteralPath $path) -and {IS_OURS})) {{ exit 0 }}"
    return "; ".join(
        [
            DESKTOP,
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($path)",
            *([keep] if only_ours else []),
            f"$s.TargetPath = {quote(target)}",
            f"$s.Arguments = {quote(arguments)}",
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
        f" if {IS_OURS} {{ Remove-Item -LiteralPath $path; $path }} }}",
    ]
)


# PowerShell 5.1 writes in the console's code page, which mangles a path like C:\Users\José;
# vpstream reads what programs write as UTF-8.
UTF8 = "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "


def powershell(script: str) -> list[str]:
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", UTF8 + script]


def create(runner: Runner, target: Path | None = None) -> str:
    """Put the icon on the desktop (again: an old one is replaced). Returns its path."""
    program, arguments = (target, ARGUMENTS) if target else launch()
    res = runner.run(powershell(create_script(program, arguments)), timeout=60)
    if res.returncode != 0:
        raise SetupError(f"the desktop icon could not be made: {tail(res)}")
    return res.stdout.strip()


def refresh(runner: Runner) -> str:
    """vpstream's icon, where there is one, made to run what `launch` gives now (an
    icon from before vpstream.exe ran pythonw.exe). Returns its path, or ""."""
    program, arguments = launch()
    res = runner.run(powershell(create_script(program, arguments, only_ours=True)), timeout=60)
    if res.returncode != 0:
        raise SetupError(f"the desktop icon could not be brought up to date: {tail(res)}")
    return res.stdout.strip()


def remove(runner: Runner) -> str:
    """Remove vpstream's desktop icon. Returns its path, or "" when there was none."""
    res = runner.run(powershell(REMOVE_SCRIPT), timeout=60)
    if res.returncode != 0:
        raise SetupError(f"the desktop icon could not be removed: {tail(res)}")
    return res.stdout.strip()
