"""A stream through the real Caddy: the daemon in a temporary folder, a free port on this
host, no firewall helper. Nothing outside the temporary folder is touched."""

from __future__ import annotations

import http.client
import os
import shutil
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from vpstream import caddy, client, config, daemon
from vpstream.tools import SystemRunner

pytestmark = pytest.mark.integration

BODY = bytes(range(256)) * 40  # 10240 bytes, every position different from its neighbours
ODD_NAME = "e2 100%? #1 {http.request.host} é.mkv"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Live:
    def __init__(self, root: Path) -> None:
        self.paths = config.Paths.under(root)
        self.cfg = config.Config(stream_port=_free_port(), dashboard_port=_free_port())
        self.log: list[str] = []
        runner = SystemRunner()
        self.server = caddy.CaddyServer(runner, self.cfg.stream_port, self.paths)
        self.daemon = daemon.Daemon(
            self.cfg, self.paths, runner, None, self.server, log=self.log.append
        )
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.daemon.serve, args=(self.stop,), daemon=True)

    def api(self, route: str, payload: dict[str, Any] | None = None) -> Any:
        return client.call(self.paths.socket, "GET" if payload is None else "POST", route, payload)

    def get(
        self, target: str, method: str = "GET", headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.cfg.stream_port, timeout=10)
        try:
            conn.request(method, target, headers=headers or {})
            answer = conn.getresponse()
            return answer.status, {k.lower(): v for k, v in answer.getheaders()}, answer.read()
        finally:
            conn.close()


@pytest.fixture
def live(short_tmp: Path) -> Iterator[Live]:
    if not shutil.which("caddy"):
        pytest.skip("caddy is not installed on this host")
    run = Live(short_tmp)
    run.thread.start()
    assert run.daemon.ready.wait(10)
    yield run
    run.stop.set()
    run.thread.join(15)


@pytest.fixture
def show(tmp_path: Path) -> Path:
    folder = tmp_path / "a show"
    folder.mkdir()
    (folder / "e1.mp4").write_bytes(BODY)
    (folder / ODD_NAME).write_bytes(BODY[::-1])
    (folder / "notes.txt").write_text("not a video")
    return folder


def test_a_file_is_served_whole_in_ranges_and_with_its_type(live: Live, show: Path) -> None:
    info = live.api("stream/start", {"file": str(show / "e1.mp4"), "alias": "aaa"})
    assert info["stream"]["url"].endswith(f":{live.cfg.stream_port}/aaa")

    status, headers, body = live.get("/aaa")
    assert status == 200 and body == BODY
    assert headers["content-type"] == "video/mp4" and headers["accept-ranges"] == "bytes"
    assert "x-vpstream-file" not in headers and "x-vpstream-type" not in headers
    assert "werkzeug" not in live.get("/nope")[1].get("server", "").lower()

    status, headers, body = live.get("/aaa/1", headers={"Range": "bytes=1000-1999"})
    assert status == 206 and body == BODY[1000:2000]
    assert headers["content-range"] == f"bytes 1000-1999/{len(BODY)}"
    status, headers, body = live.get("/aaa", headers={"Range": "bytes=-10"})
    assert status == 206 and body == BODY[-10:]

    status, headers, body = live.get("/aaa", method="HEAD")
    assert status == 200 and body == b"" and headers["content-length"] == str(len(BODY))
    assert live.get("/aaa/2")[0] == 404


def test_a_folder_with_an_awkward_file_name(live: Live, show: Path) -> None:
    live.api("stream/start", {"dir": str(show), "alias": "aaa"})
    status, headers, body = live.get("/aaa/2")
    assert status == 200 and body == BODY[::-1]
    assert headers["content-type"] == "video/x-matroska"

    status, headers, body = live.get("/aaa", headers={"Accept": "text/html"})
    assert status == 200 and headers["content-type"].startswith("text/html")
    assert b'href="/aaa/2"' in body
    status, headers, body = live.get("/aaa", headers={"Accept": "*/*"})
    assert headers["content-type"].startswith("audio/x-mpegurl")
    assert f"http://127.0.0.1:{live.cfg.stream_port}/aaa/2".encode() in body
    assert live.get("/aaa/playlist.m3u")[0] == 200
    assert live.get("/aaa/3")[0] == 404  # notes.txt is not a video


def test_only_what_the_gate_names_can_be_fetched(live: Live, show: Path) -> None:
    live.api("stream/start", {"file": str(show / "e1.mp4"), "alias": "aaa"})
    secret = show / "notes.txt"
    for target in (
        "/",
        "/aaa/../notes.txt",
        "/aaa/%2e%2e/notes.txt",
        f"/aaa/1/..{secret}",
        f"/{secret}",
        f"//{secret}",
        "/aaa/1/../../../../etc/hostname",
    ):
        status, _, body = live.get(quote(target))
        assert b"not a video" not in body, target
        assert status in (301, 308, 400, 404) or body == BODY, (target, status)
    # A viewer cannot name a file itself, in a request header.
    status, _, body = live.get("/nope", headers={"X-Vpstream-File": str(secret)})
    assert status == 404 and b"not a video" not in body
    for method in ("POST", "PUT", "DELETE"):
        assert live.get("/aaa", method=method)[0] == 405


def test_policies_and_alias_apply_on_the_next_request(live: Live, show: Path) -> None:
    live.api("stream/start", {"file": str(show / "e1.mp4"), "alias": "aaa", "policy": "approval"})
    spoof = {"X-Vpstream-Ip": "203.0.113.7", "X-Forwarded-For": "203.0.113.8", "User-Agent": "TV"}
    assert live.get("/aaa", headers=spoof)[0] == 403
    (device,) = live.api("status")["stream"]["devices"]
    assert device["ip"] == "127.0.0.1"  # the address the request came from, not one it claimed
    assert device["wl"] is True and device["user_agent"] == "TV"

    live.api("devices", {"ip": "127.0.0.1", "allowed": True})
    assert live.get("/aaa")[0] == 200
    live.api("devices", {"ip": "127.0.0.1", "allowed": False})
    assert live.get("/aaa")[0] == 403
    live.api("stream/policy", {"policy": "open"})
    assert live.get("/aaa")[0] == 403  # disabled stays out, even when open
    live.api("devices", {"ip": "127.0.0.1", "allowed": True})

    live.api("stream/alias", {"alias": "bbb"})
    assert live.get("/aaa")[0] == 404 and live.get("/bbb")[0] == 200


def test_bytes_sent_are_counted(live: Live, show: Path) -> None:
    live.api("stream/start", {"file": str(show / "e1.mp4"), "alias": "aaa"})
    live.get("/aaa")
    live.get("/aaa", headers={"Range": "bytes=0-99"})
    for _ in range(100):
        (device,) = live.api("status")["stream"]["devices"]
        if device["bytes"] >= len(BODY) + 100:
            break
        live.stop.wait(0.1)
    # From the access log, or from the kernel when it saw a connection open, which counts
    # the response headers too.
    assert len(BODY) + 100 <= device["bytes"] <= len(BODY) + 100 + 2000
    assert device["requests"] == 2


def test_a_transfer_the_player_cuts_off_is_counted_by_what_was_sent(
    live: Live, tmp_path: Path
) -> None:
    """Caddy 2.6.2 logs about 512 bytes for this; the kernel knows better."""
    if not shutil.which("ss"):
        pytest.skip("ss is not installed on this host")
    film = tmp_path / "big.mkv"
    film.write_bytes(os.urandom(48 * 1024 * 1024))
    live.api("stream/start", {"file": str(film), "alias": "aaa"})
    with socket.create_connection(("127.0.0.1", live.cfg.stream_port), timeout=10) as conn:
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
        conn.sendall(b"GET /aaa HTTP/1.1\r\nHost: x\r\nRange: bytes=0-\r\n\r\n")
        # About 4 MB/s, a fast phone, for long enough to be seen a few times.
        got, started = 0, time.monotonic()
        while time.monotonic() - started < 3.5:
            chunk = conn.recv(65536)
            assert chunk
            got += len(chunk)
            time.sleep(0.016)
    # Hung up like a player that seeks: far from the whole 48 MB was sent. What was sent
    # after the last look may be missed (Caddy 2.6.2 logs 512 bytes): about a second of it.
    for _ in range(50):
        (device,) = live.api("status")["stream"]["devices"]
        if device["bytes"] >= got - 6 * 1024 * 1024:
            break
        live.stop.wait(0.2)
    window = 8 * 1024 * 1024  # what the kernel's buffers can hold past what was read
    assert got - 6 * 1024 * 1024 <= device["bytes"] <= got + window, (got, device["bytes"])


def test_caddy_runs_only_while_a_stream_does_and_opens_no_admin_port(
    live: Live, show: Path
) -> None:
    port = live.cfg.stream_port
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1)
    admin_before = caddy._accepts(2019)
    live.api("stream/start", {"file": str(show / "e1.mp4")})
    assert live.server.running() and caddy._accepts(port)
    assert caddy._accepts(2019) == admin_before
    assert not (live.paths.caddy_dir / "config" / "caddy" / "autosave.json").exists()
    live.api("stream/stop", {})
    assert not live.server.running()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1)


def test_a_stream_is_stopped_when_caddy_dies(live: Live, show: Path) -> None:
    live.api("stream/start", {"file": str(show / "e1.mp4")})
    assert live.server._proc is not None
    live.server._proc.kill()
    for _ in range(50):
        if live.api("status")["stream"] is None:
            break
        live.stop.wait(0.1)
    assert live.api("status")["stream"] is None
    assert any("Caddy stopped unexpectedly" in line for line in live.log)


def test_an_mkv_without_audio_in_its_index_is_sent_with_a_full_one(
    live: Live, tmp_path: Path, need_ffmpeg: None
) -> None:
    """What makes VLC for Android resume quickly. ffmpeg indexes only the video."""
    import subprocess

    from vpstream import mkv

    if not shutil.which("mkvmerge"):
        pytest.skip("mkvmerge is not installed on this host")
    film = tmp_path / "film.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=25",
            "-f",
            "lavfi",
            "-i",
            "sine",
            "-t",
            "4",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(film),
        ],
        check=True,
    )
    assert mkv.index_problem(film) == "its seek index has no audio entries"
    live.api("stream/start", {"file": str(film), "alias": "aaa"})
    for _ in range(100):
        status, headers, body = live.get("/aaa")
        if status == 200:
            break
        assert status == 503 and headers["retry-after"] == "30"
        live.stop.wait(0.1)
    got = tmp_path / "got.mkv"
    got.write_bytes(body)
    assert status == 200 and mkv.index_problem(got) is None
    assert live.api("status")["stream"]["videos"][0]["preparing"] is False
    copies = list((live.paths.cache_dir / "index").iterdir())
    assert len(copies) == 1 and copies[0].read_bytes() == body
