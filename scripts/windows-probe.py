#!/usr/bin/env python3
"""What vpstream's Windows support takes for granted, looked at on a real Windows.

Prints one line per fact; nothing is changed outside a temporary folder (one call to
SetThreadExecutionState is made and taken back). Run it twice, in a normal terminal and in
one opened with "Run as administrator", and send both outputs:

    python -I windows-probe.py > probe-user.txt
    python -I windows-probe.py > probe-admin.txt
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any


def fact(name: str, get: Callable[[], Any]) -> None:
    try:
        value = get()
    except Exception as exc:
        value = f"FAILED {type(exc).__name__}: {exc}"
        if os.environ.get("PROBE_TRACE"):
            value += "\n" + traceback.format_exc()
    print(f"{name}: {value}", flush=True)


def powershell(command: str) -> str:
    res = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return (res.stdout.strip() or res.stderr.strip())[:3000]


def is_admin() -> bool:
    return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]


def fromfd() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        dup = socket.fromfd(s.fileno(), socket.AF_INET, socket.SOCK_STREAM)
        dup.close()
    return "works"


def exclusive_bind() -> str:
    with socket.socket() as a:
        a.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined]
        a.bind(("127.0.0.1", 0))
        a.listen()
        port = a.getsockname()[1]
        with socket.socket() as b:
            b.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                b.bind(("127.0.0.1", port))
            except OSError as exc:
                return f"a second bind is refused ({exc})"
    return "a second bind SUCCEEDED (not exclusive)"


def werkzeug_fd() -> str:
    from werkzeug.serving import make_server

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    server = make_server("127.0.0.1", sock.getsockname()[1], lambda e, s: [], fd=sock.fileno())
    port = server.server_port
    server.server_close()
    sock.close()
    return f"works (port {port})"


def files(tmp: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    target = tmp / "target é.mkv"
    target.write_bytes(b"x" * 100)
    folder = tmp / "folder"
    folder.mkdir()
    (folder / "seg.ts").write_bytes(b"y")

    def step(name: str, run: Callable[[], Any]) -> None:
        try:
            run()
            out[name] = "ok"
        except Exception as exc:
            out[name] = f"FAILED {type(exc).__name__}: {exc}"

    step("file symlink", lambda: (tmp / "1").symlink_to(target))
    step("dir symlink", lambda: (tmp / "2").symlink_to(folder, target_is_directory=True))
    step("read through dir symlink", lambda: (tmp / "2" / "seg.ts").read_bytes())
    step("dir symlink made without the flag works", lambda: _no_flag(tmp, folder))

    def replace_link() -> None:
        part = tmp / ".1.new"
        part.symlink_to(target)
        os.replace(part, tmp / "1")

    step("os.replace over a symlink", replace_link)
    with open(target, "rb"):
        step("unlink a symlink while its target is open", lambda: (tmp / "1").unlink())
    with open(folder / "seg.ts", "rb"):
        step("rmtree a folder with an open file", lambda: shutil.rmtree(folder))
    folder.mkdir(exist_ok=True)
    state = tmp / "state.json"
    state.write_text("{}", encoding="utf-8")
    with open(state, encoding="utf-8"):
        step("os.replace over an open file", lambda: _replace(tmp, state))
    log = tmp / "access.log"
    log.write_text("a\n", encoding="utf-8")
    with open(log, "rb"):
        step("rename a file another handle reads", lambda: os.rename(log, tmp / "access-1.log"))
    out["st_ino"] = str(os.stat(target).st_ino)
    out["resolve"] = str(target.resolve())
    return out


def _no_flag(tmp: Path, folder: Path) -> None:
    link = tmp / "3"
    link.symlink_to(folder)
    (link / "seg.ts").read_bytes()


def _replace(tmp: Path, state: Path) -> None:
    new = tmp / "state.json.tmp"
    new.write_text("{1}", encoding="utf-8")
    os.replace(new, state)


def boot() -> dict[str, Any]:
    import winreg  # type: ignore[import-not-found,unused-ignore]

    found: dict[str, Any] = {}
    key = r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
            found["BootId"] = winreg.QueryValueEx(k, "BootId")[0]
    except OSError as exc:
        found["BootId"] = f"none ({exc})"
    tick = ctypes.windll.kernel32.GetTickCount64  # type: ignore[attr-defined]
    tick.restype = ctypes.c_ulonglong
    booted = time.localtime(time.time() - tick() / 1000)
    found["boot_time"] = time.strftime("%Y-%m-%d %H:%M:%S", booted)
    return found


def long_paths() -> Any:
    import winreg  # type: ignore[import-not-found,unused-ignore]

    key = r"SYSTEM\CurrentControlSet\Control\FileSystem"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
        return winreg.QueryValueEx(k, "LongPathsEnabled")[0]


def path_search(tmp: Path) -> dict[str, str]:
    """Is a program in a folder put on this process's PATH found by name, and is one in the
    current folder not, with NoDefaultCurrentDirectoryInExePath?"""
    out = {}
    whoami = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "whoami.exe"
    tools = tmp / "tools"
    tools.mkdir()
    shutil.copy(whoami, tools / "vpprobe.exe")
    here = tmp / "here"
    here.mkdir()
    shutil.copy(whoami, here / "vpprobe2.exe")
    old = os.environ.copy()
    try:
        os.environ["PATH"] = str(tools) + os.pathsep + os.environ["PATH"]
        res = subprocess.run(["vpprobe"], capture_output=True, text=True, check=False)
        out["found on this process's PATH"] = f"exit {res.returncode}"
        os.environ["NODEFAULTCURRENTDIRECTORYINEXEPATH"] = "1"
        try:
            res = subprocess.run(["vpprobe2"], capture_output=True, cwd=here, check=False)
            out["in the current folder, with the variable"] = f"RAN (exit {res.returncode})"
        except FileNotFoundError:
            out["in the current folder, with the variable"] = "not found (good)"
        os.environ.pop("NODEFAULTCURRENTDIRECTORYINEXEPATH")
        try:
            res = subprocess.run(["vpprobe2"], capture_output=True, cwd=here, check=False)
            out["in the current folder, without it"] = f"ran (exit {res.returncode})"
        except FileNotFoundError:
            out["in the current folder, without it"] = "not found"
    finally:
        os.environ.clear()
        os.environ.update(old)
    return out


def awake() -> str:
    flags = 0x80000000 | 0x00000001  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
    before = kernel.SetThreadExecutionState(flags)
    requests = powershell("powercfg /requests")
    kernel.SetThreadExecutionState(0x80000000)
    return f"returned {before:#x}; powercfg /requests while set:\n{requests}"


def in_job() -> bool:
    result = ctypes.c_int()
    kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel.IsProcessInJob(kernel.GetCurrentProcess(), None, ctypes.byref(result))
    return bool(result.value)


def vpstream_imports() -> str:
    import importlib

    names = ["config", "control", "caddy", "prepare", "public", "gate", "daemon", "stream"]
    names += ["subtitles", "probe", "platform.windows", "platform.windows.doctor"]
    for name in names:
        importlib.import_module(f"vpstream.{name}")
    import vpstream

    return f"all import ({vpstream.__version__})"


def addresses() -> dict[str, Any]:
    from vpstream import local, net

    found: dict[str, Any] = {"outgoing": net.outgoing_address()}
    try:
        found["local.find"] = local.find()
    except Exception as exc:
        found["local.find"] = f"FAILED {exc}"
    infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    found["getaddrinfo"] = sorted({i[4][0] for i in infos})
    return found


def fonts(tmp: Path) -> str:
    """Which font provider libass uses in this ffmpeg, from a burn of one frame."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return "no ffmpeg on PATH (run again after windows-install)"
    ass = tmp / "a.ass"
    ass.write_text(
        "[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize\n"
        "Style: Default,Arial,40\n\n[Events]\nFormat: Layer, Start, End, Style, Text\n"
        "Dialogue: 0,0:00:00.00,0:00:01.00,Default,Hello 日本語\n",
        encoding="utf-8",
    )
    res = subprocess.run(
        [ffmpeg, "-hide_banner", "-v", "verbose", "-f", "lavfi", "-i", "color=s=320x240:d=1",
         "-vf", "subtitles=a.ass", "-frames:v", "1", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace", cwd=tmp, check=False,
    )  # fmt: skip
    lines = [x for x in res.stderr.splitlines() if "font" in x.lower() or "libass" in x.lower()]
    return f"exit {res.returncode}\n" + "\n".join(lines[:25])


def main() -> None:
    fact("python", lambda: f"{sys.version} at {sys.executable}")
    fact("pythonw next to it", lambda: (Path(sys.executable).with_name("pythonw.exe")).is_file())
    fact("venv", lambda: sys.prefix != sys.base_prefix)
    fact("windows", lambda: f"{sys.getwindowsversion()}")  # type: ignore[attr-defined]
    fact("user", lambda: os.environ.get("USERNAME"))
    fact("administrator", is_admin)
    fact("in a job", in_job)
    fact("tkinter", lambda: __import__("tkinter").TkVersion)
    fact("vpstream", vpstream_imports)
    fact("socket.AF_UNIX", lambda: hasattr(socket, "AF_UNIX"))
    fact("socket.fromfd", fromfd)
    fact("SO_EXCLUSIVEADDRUSE", exclusive_bind)
    fact("werkzeug make_server(fd=)", werkzeug_fd)
    fact("LongPathsEnabled", long_paths)
    fact("boot", boot)
    fact("addresses", addresses)
    fact(
        "network profiles",
        lambda: powershell(
            "Get-NetConnectionProfile | Select-Object InterfaceAlias,InterfaceIndex,"
            "NetworkCategory,Name | ConvertTo-Json -Compress"
        ),
    )
    fact(
        "IPv4 addresses",
        lambda: powershell(
            "Get-NetIPAddress -AddressFamily IPv4 | Select-Object IPAddress,InterfaceAlias,"
            "InterfaceIndex,PrefixOrigin | ConvertTo-Json -Compress"
        ),
    )
    fact(
        "default route",
        lambda: powershell(
            "Get-NetRoute -DestinationPrefix 0.0.0.0/0 | Select-Object InterfaceIndex,"
            "InterfaceAlias,RouteMetric,InterfaceMetric | ConvertTo-Json -Compress"
        ),
    )
    fact("winget", lambda: subprocess.run(["winget", "--version"], capture_output=True,
                                          text=True, timeout=60, check=False).stdout)  # fmt: skip
    fact("netsh", lambda: shutil.which("netsh"))
    fact("desktop", lambda: powershell("[Environment]::GetFolderPath('Desktop')"))
    fact("keep awake", awake)
    with tempfile.TemporaryDirectory(prefix="vpprobe ") as name:
        tmp = Path(name)
        (tmp / "f").mkdir()
        fact("files", lambda: json.dumps(files(tmp / "f"), indent=1, ensure_ascii=False))
        fact("program search", lambda: json.dumps(path_search(tmp), indent=1))
        fact("libass fonts", lambda: fonts(tmp))


if __name__ == "__main__":
    if sys.platform != "win32":
        sys.exit("this probe is for Windows")
    main()
