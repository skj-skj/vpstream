"""What the window asks of Windows itself, through ctypes: administrator rights, one window
at a time, the programs it starts ending with it, the PC kept awake while a stream runs,
and the boot a recorded stream belongs to.

Every function here imports on Linux (for the tests and mypy) and does nothing useful
there; only Windows calls them for real.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import sys
from pathlib import Path
from typing import Any

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
PROCESS_SET_INFORMATION = 0x0200
SYNCHRONIZE = 0x00100000
STILL_ACTIVE = 259
NORMAL_PRIORITY_CLASS = 0x20
BELOW_NORMAL_PRIORITY_CLASS = 0x4000
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ERROR_ALREADY_EXISTS = 183
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
MUTEX_NAME = "Local\\vpstream-window"
BOOT_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters"


def _kernel32() -> Any:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
    # Handles are pointers: as the default int, GetCurrentProcess's -1 loses its meaning.
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    return kernel32


def _me(kernel32: Any) -> Any:
    return ctypes.c_void_p(kernel32.GetCurrentProcess())


def is_admin() -> bool:
    """Whether this runs with administrator rights (an elevated token)."""
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def relaunch_elevated(user: str) -> bool:
    """Start the window again with administrator rights: Windows asks the person. Returns
    False when they said no (or it could not ask). `user` is who asked, to tell an
    administrator of another account apart (see cli)."""
    if sys.platform != "win32":
        return False
    from vpstream.platform.windows.shortcut import ARGUMENTS, pythonw

    program = pythonw()
    if not program.exists():
        program = Path(sys.executable)
    args = f'{ARGUMENTS} --elevated-for "{user}"'
    done = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(program), args, None, 1)
    return int(done) > 32  # ShellExecute's success


class SingleInstance:
    """A named mutex: the second window to open finds the first's."""

    def __init__(self, name: str = MUTEX_NAME) -> None:
        self.handle = None
        self.first = True
        if sys.platform != "win32":
            return
        kernel32 = _kernel32()
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        self.handle = kernel32.CreateMutexW(None, False, name)
        self.first = ctypes.get_last_error() != ERROR_ALREADY_EXISTS


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_JOB: list[Any] = []  # the handle, kept open for as long as this process lives


def end_children_with_this_process() -> bool:
    """Put this process in a job that Windows ends, with every program started from here
    (Caddy, ffmpeg), when this process ends however it ends. Returns False when Windows
    would not; the pid file of Caddy is the fallback (see `stop_left_over`)."""
    if sys.platform != "win32" or _JOB:
        return bool(_JOB)
    kernel32 = _kernel32()
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return False
    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    size = ctypes.sizeof(limits)
    ok = kernel32.SetInformationJobObject(
        ctypes.c_void_p(job), JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), size
    )
    me = _me(kernel32)
    if not ok or not kernel32.AssignProcessToJobObject(ctypes.c_void_p(job), me):
        kernel32.CloseHandle(ctypes.c_void_p(job))
        return False
    _JOB.append(job)
    return True


def lower_priority() -> None:
    """This process and what it starts run below normal: preparing a video never slows the
    PC down. Caddy is put back to normal (`normal_priority`), so streaming keeps up."""
    if sys.platform == "win32":
        kernel32 = _kernel32()
        kernel32.SetPriorityClass(_me(kernel32), BELOW_NORMAL_PRIORITY_CLASS)


def normal_priority(pid: int) -> None:
    if sys.platform == "win32":
        kernel32 = _kernel32()
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(PROCESS_SET_INFORMATION, False, pid)
        if handle:
            kernel32.SetPriorityClass(ctypes.c_void_p(handle), NORMAL_PRIORITY_CLASS)
            kernel32.CloseHandle(ctypes.c_void_p(handle))


def keep_awake(on: bool) -> None:
    """While a stream runs, Windows does not go to sleep (the screen may go off). Called
    from the window's own thread: the request belongs to the thread that made it."""
    if sys.platform == "win32":
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint32(flags))


def boot_id() -> str:
    """A name for this boot of Windows: a recorded stream is never taken up after a
    restart. Windows counts its boots in the registry."""
    if sys.platform != "win32":
        return ""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, BOOT_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "BootId")
    except OSError:
        return ""
    return f"windows-{value}"


# --- a process by its number


def _start_time(handle: Any) -> int:
    times = [ctypes.c_ulonglong() for _ in range(4)]
    ok = _kernel32().GetProcessTimes(handle, *(ctypes.byref(t) for t in times))
    return times[0].value if ok else 0


def describe(pid: int) -> dict[str, Any] | None:
    """The program a running process runs, and when it started; None when it has ended or
    cannot be looked at."""
    if sys.platform != "win32":
        return None
    kernel32 = _kernel32()
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        code = ctypes.c_uint32()
        if not kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
            return None
        if code.value != STILL_ACTIVE:
            return None
        size = ctypes.c_uint32(32768)
        name = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            ctypes.c_void_p(handle), 0, name, ctypes.byref(size)
        ):
            return None
        return {"program": name.value, "started": _start_time(ctypes.c_void_p(handle))}
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def terminate(pid: int) -> None:
    if sys.platform == "win32":
        kernel32 = _kernel32()
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
        if handle:
            kernel32.TerminateProcess(ctypes.c_void_p(handle), 1)
            kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 5000)
            kernel32.CloseHandle(ctypes.c_void_p(handle))


def write_pid(path: Path, pid: int) -> None:
    """Note a started program: its number, what it runs and when it started, so a later
    window stops it only when it is still that program (numbers are used again)."""
    found = describe(pid)
    if found is not None:
        path.write_text(json.dumps({"pid": pid, **found}), encoding="utf-8")


def stop_noted(path: Path, look: Any = describe, end: Any = terminate) -> bool:
    """Stop the program a pid file names, if it is still that one. Returns whether one was
    stopped. The file is removed either way."""
    try:
        noted = json.loads(path.read_text(encoding="utf-8"))
        pid = int(noted["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        with contextlib.suppress(OSError):
            path.unlink()
        return False
    found = look(pid)
    same = found is not None and all(found.get(k) == noted.get(k) for k in ("program", "started"))
    if same:
        end(pid)
    with contextlib.suppress(OSError):
        path.unlink()
    return same


def user() -> str:
    return os.environ.get("USERNAME", "")
