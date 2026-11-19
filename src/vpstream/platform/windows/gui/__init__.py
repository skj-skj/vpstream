"""The vpstream window: `model` (plain data, tested anywhere), `app`, `browser` and
`widgets` (Qt), and `main`, what `vpstream` with no command runs on Windows.

Before the window opens: administrator rights (Windows asks; the firewall rule needs them),
one window at a time, and every program the window starts tied to it, so a window that is
killed leaves no Caddy behind.
"""

from __future__ import annotations

import ctypes
import os
import sys
import traceback

from vpstream.platform.windows.doctor import PYSIDE6_FIX

TITLE = "vpstream"
MB_ICONERROR, MB_ICONINFORMATION = 0x10, 0x40
# What would make Qt load other plugins or another platform into a window that runs as
# administrator; the window is Qt's own, from vpstream's PySide6.
QT_SETTINGS = (
    "QT_PLUGIN_PATH",
    "QT_QPA_PLATFORM_PLUGIN_PATH",
    "QT_QPA_PLATFORM",
    "QT_STYLE_OVERRIDE",
)


def message(text: str, error: bool = True) -> None:
    """A message box before there is a window (pythonw has no terminal to print to)."""
    if sys.platform == "win32":
        flags = MB_ICONERROR if error else MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(None, text, TITLE, flags)
    else:
        print(text, file=sys.stderr)


def main(elevated_for: str | None = None) -> int:
    from vpstream.platform.windows import processes

    if not processes.is_admin():
        if processes.relaunch_elevated(processes.user()):
            return 0
        message(
            "vpstream needs administrator rights: it lets the phones and TVs at home through "
            "Windows Firewall while a stream runs. Open it again and choose Yes."
        )
        return 1
    instance = processes.SingleInstance()
    if not instance.first:
        message("vpstream is already open.", error=False)
        return 0
    for name in QT_SETTINGS:
        os.environ.pop(name, None)
    try:
        import PySide6.QtWidgets  # noqa: F401
    except ImportError:
        message(f"The vpstream window needs PySide6: {PYSIDE6_FIX}.")
        return 1
    try:
        return _open(elevated_for or "")
    except Exception as exc:
        message(f"vpstream could not open: {exc}\n\n{traceback.format_exc()[-1500:]}")
        return 1


def _open(elevated_for: str) -> int:
    from vpstream import config
    from vpstream.platform.windows import processes, shortcut
    from vpstream.platform.windows.doctor import log_file
    from vpstream.platform.windows.gui import app
    from vpstream.platform.windows.session import Log, Session
    from vpstream.tools import SystemRunner

    processes.end_children_with_this_process()
    processes.lower_priority()
    paths = config.default_paths()
    log = Log(log_file(paths))
    log(f"the window opens (administrator: {processes.user()})")
    first = not paths.config_file.exists()
    runner = SystemRunner()
    session = Session(paths, runner=runner, log=log)
    try:  # made on the first run; afterwards only kept up to date where it is
        icon = shortcut.create(runner) if first else shortcut.refresh(runner)
        if icon:
            log(f"the desktop icon: {icon}")
    except Exception as exc:  # the window works without it
        log(f"WARNING: the desktop icon: {exc}")
    me = processes.user()
    other = me if elevated_for and elevated_for.lower() != me.lower() else ""
    return app.run(session, runner, log.path, other)
