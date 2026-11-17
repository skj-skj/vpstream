"""Termux, for real: local mode through the real Caddy, prepares with the real ffmpeg, the
real samples on the phone's storage (FUSE), and a daemon killed as Android kills it.

The stream listens on 127.0.0.2, which stands in for the Wi-Fi address (a loopback address
other than 127.0.0.1, so a listener on 127.0.0.1 alone does not answer). The resume test
runs a daemon of its own and uses this phone's real network address.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from vpstream import client, config, local
from vpstream.tests.fakes import free_port
from vpstream.tests.test_stream_integration import HTML, Live, _cookie, _wait_prepared

pytestmark = pytest.mark.integration

SAMPLES = Path("/storage/emulated/0/Download/vpstream-samples")
LAN = "127.0.0.2"


def _sample(name: str) -> Path:
    path = SAMPLES / name
    if not path.is_file():
        pytest.skip(f"the real samples are not on this host ({path})")
    return path


class LanLive(Live):
    """A local-mode daemon with Termux's bounds, serving on LAN."""

    def __init__(self, root: Path) -> None:
        settings = {**config.TERMUX_LIMITS, "local": True}
        super().__init__(root, **settings)

    def at(
        self, host: str, target: str, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection(host, self.cfg.stream_port, timeout=10)
        try:
            conn.request("GET", target, headers=headers or {})
            answer = conn.getresponse()
            return answer.status, {k.lower(): v for k, v in answer.getheaders()}, answer.read()
        finally:
            conn.close()


@pytest.fixture
def lan(on_termux: None, short_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[LanLive]:
    if not shutil.which("caddy"):
        pytest.skip("caddy is not installed on this host")
    monkeypatch.setattr(local, "find", lambda configured="": LAN)
    monkeypatch.setattr(local, "is_mine", lambda address: address == LAN)
    run = LanLive(short_tmp)
    run.thread.start()
    assert run.daemon.ready.wait(10)
    yield run
    run.stop.set()
    run.thread.join(15)


def test_a_direct_stream_of_a_sample_on_the_lan_address_only(lan: LanLive) -> None:
    film = _sample("h264-1080p-movtext.mp4")
    info = lan.api("stream/start", {"file": str(film), "alias": "aaa"})
    assert info["stream"]["url"] == f"http://{LAN}:{lan.cfg.stream_port}/aaa"
    config_file = json.loads((lan.paths.caddy_dir / "caddy.json").read_text())
    server = config_file["apps"]["http"]["servers"]["vpstream"]
    assert server["listen"] == [f"{LAN}:{lan.cfg.stream_port}"]
    status, headers, body = lan.at(LAN, "/aaa", {"Range": "bytes=1000-1999"})
    assert status == 206 and len(body) == 1000 and headers["content-type"] == "video/mp4"
    assert body == film.read_bytes()[1000:2000]  # through FUSE, whole and in place
    with pytest.raises(OSError):
        lan.at("127.0.0.1", "/aaa")  # not on every address
    time.sleep(2.5)  # the access log is read every second (no `ss` on Android)
    device = lan.api("status")["stream"]["devices"][0]
    assert device["bytes"] >= 1000


def test_a_browser_stream_with_its_code_and_subtitles(lan: LanLive) -> None:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is not installed on this host")
    film = _sample("h264-1080p-all-tracks.mkv")
    plans = lan.api("prepare", {"file": str(film), "sub": "1,4", "confirmed": True})["plans"]
    assert [t["task"] for t in plans[0]["tasks"]] == [
        "base version (copy)",
        "subtitle 1 (soft)",
        "subtitle 4 (burned in)",
    ]
    jobs = _wait_prepared(lan, timeout=600)
    assert {j["state"] for j in jobs} == {"done"}, jobs
    lan.api("stream/start", {"file": str(film), "alias": "tv", "mode": "browser", "sub": "1"})
    status, headers, body = lan.at(LAN, "/tv", HTML)
    code = lan.api("status")["stream"]["devices"][0]["code"]
    assert status == 403 and code.encode() in body
    browser = {**HTML, "Cookie": _cookie(headers)}
    device = lan.api("status")["stream"]["devices"][0]
    lan.api("devices", {"device": device["id"], "allowed": True})
    status, _, body = lan.at(LAN, "/tv", browser)
    assert status == 200
    page = json.loads(body.decode().split('type="application/json">')[1].split("</script>")[0])
    assert [(s["number"], s["kind"]) for s in page["subs"]] == [(1, "soft"), (4, "burn")]
    status, headers, vtt = lan.at(LAN, "/tv/1/hls/subs/1.vtt", browser)
    assert status == 200 and headers["content-type"].startswith("text/vtt") and b"-->" in vtt
    status, _, master = lan.at(LAN, "/tv/1/hls/burn/4/master.m3u8", browser)
    assert status == 200 and b"#EXTM3U" in master


# --- a daemon killed as Android kills it


def _status(socket: Path) -> dict[str, Any] | None:
    try:
        info: dict[str, Any] = client.call(socket, "GET", "status", timeout=2)
    except (client.DaemonUnavailable, client.DaemonRefused):
        return None
    return info


def _daemon(root: Path, log: Path) -> subprocess.Popen[bytes]:
    env = {**os.environ, config.ROOT_ENV: str(root), "VPSTREAM_PLATFORM": "termux"}
    with log.open("ab") as out:
        return subprocess.Popen(
            [sys.executable, "-P", "-m", "vpstream", "daemon"],
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
            cwd=root,
        )


def _wait_for(check: Any, seconds: float, what: str, log: Path) -> Any:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if found := check():
            return found
        time.sleep(0.2)
    raise AssertionError(f"{what}:\n{log.read_text(errors='replace')[-2000:]}")


def _caddy_pids(config_file: Path) -> set[int]:
    pids = set()
    for entry in Path("/proc").iterdir():
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if os.fsencode(config_file) in argv:
            pids.add(int(entry.name))
    return pids


def test_a_stream_comes_back_after_the_daemon_is_killed(short_tmp: Path) -> None:
    if not shutil.which("caddy"):
        pytest.skip("caddy is not installed on this host")
    try:
        address = local.find()
    except local.LocalError as exc:
        pytest.skip(f"not on a local network: {exc}")
    film = _sample("h264-1080p-movtext.mp4")
    paths = config.Paths.under(short_tmp)
    paths.config_file.parent.mkdir(parents=True)
    port = free_port(address)
    paths.config_file.write_text(
        f"[stream]\nport = {port}\n[dashboard]\nport = {free_port()}\n", encoding="utf-8"
    )
    log = short_tmp / "daemon.log"
    first = _daemon(short_tmp, log)
    second = None
    try:
        _wait_for(lambda: _status(paths.socket), 20, "the daemon did not start", log)
        client.call(paths.socket, "POST", "stream/start", {"file": str(film), "alias": "kil"})
        caddy_config = paths.caddy_dir / "caddy.json"
        (left,) = _caddy_pids(caddy_config)
        first.send_signal(signal.SIGKILL)  # as Android does: no clean-up at all
        first.wait(10)
        assert _caddy_pids(caddy_config) == {left}, "Caddy outlives the daemon"

        second = _daemon(short_tmp, log)
        info = _wait_for(
            lambda: (s := _status(paths.socket)) and s.get("stream") and s, 30, "no resume", log
        )
        assert info["stream"]["alias"] == "kil"
        assert "is running again after a restart of the daemon" in log.read_text()
        assert left not in _caddy_pids(caddy_config), "the one left behind was stopped"
        conn = http.client.HTTPConnection(address, port, timeout=10)
        conn.request("GET", "/kil", headers={"Range": "bytes=0-99"})
        assert conn.getresponse().status == 206
        conn.close()
    finally:
        for proc in (first, second):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                proc.wait(20)
        for pid in _caddy_pids(paths.caddy_dir / "caddy.json"):
            os.kill(pid, signal.SIGKILL)
