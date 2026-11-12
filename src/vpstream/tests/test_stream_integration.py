"""A stream through the real Caddy: the daemon in a temporary folder, a free port on this
host, no firewall helper. Nothing outside the temporary folder is touched."""

from __future__ import annotations

import http.client
import json
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

    live.api("devices", {"device": "127.0.0.1", "allowed": True})
    assert live.get("/aaa")[0] == 200
    live.api("devices", {"device": "127.0.0.1", "allowed": False})
    assert live.get("/aaa")[0] == 403
    live.api("stream/policy", {"policy": "open"})
    assert live.get("/aaa")[0] == 403  # disabled stays out, even when open
    live.api("devices", {"device": "127.0.0.1", "allowed": True})

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


# --- browser mode: prepared HLS versions through the real ffmpeg and Caddy


def _wait_prepared(live: Live, timeout: float = 120) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = live.api("status")["prepare"]
        if not any(j["state"] in ("queued", "running") for j in jobs):
            return list(jobs)
        time.sleep(0.2)
    raise AssertionError("the prepare did not finish")


HTML = {"Accept": "text/html"}


def _fetch_all(live: Live, url: str) -> set[str]:
    """Fetch a playlist and, as a player does, every file it names; fail on any that does
    not come. Returns every address fetched."""
    from urllib.parse import urljoin

    from vpstream.prepared import playlist_uris

    seen: set[str] = set()
    todo = [url]
    while todo:
        target = todo.pop()
        if target in seen:
            continue
        seen.add(target)
        status, _, body = live.get(target)
        assert status == 200, (target, status)
        if target.endswith(".m3u8"):
            todo += [urljoin(target, uri) for uri in playlist_uris(body.decode())]
    return seen


def test_a_prepared_video_plays_through_caddy(live: Live, sample: Path) -> None:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is not installed on this host")
    answer = live.api("prepare", {"file": str(sample), "sub": "1,2", "confirmed": True})
    assert [t["task"] for t in answer["plans"][0]["tasks"]] == [
        "base version (copy)",
        "subtitle 1 (soft)",
        "subtitle 2 (burned in)",
    ]
    jobs = _wait_prepared(live)
    assert {j["state"] for j in jobs} == {"done"}, jobs

    live.api("stream/start", {"file": str(sample), "alias": "aaa", "mode": "browser",
                              "policy": "open", "sub": "1"})  # fmt: skip
    status, headers, body = live.get("/aaa", headers=HTML)
    assert status == 200 and headers["content-type"].startswith("text/html")
    page = body.decode()
    settings = json.loads(page.split('type="application/json">')[1].split("</script>")[0])
    assert [a["label"] for a in settings["audio"]] == ["English", "Japanese"]
    assert [(s["number"], s["kind"]) for s in settings["subs"]] == [(1, "soft"), (2, "burn")]
    assert settings["start_sub"] == 0 and settings["subs"][1]["url"].endswith("burn/2/master.m3u8")

    # A player that is not a browser is sent to the playlist, which Caddy follows to files.
    status, headers, _ = live.get("/aaa")
    assert status == 302 and headers["location"] == "/aaa/1/hls/base/master.m3u8"
    status, headers, body = live.get("/aaa/1/hls/base/master.m3u8")
    assert status == 200 and headers["content-type"] == "application/vnd.apple.mpegurl"
    assert b'URI="a1.m3u8"' in body and b'URI="a2.m3u8"' in body
    status, _, body = live.get("/aaa/1/hls/base/video.m3u8")
    segment = next(line for line in body.decode().splitlines() if line.endswith(".m4s"))
    status, headers, part = live.get(f"/aaa/1/hls/base/{segment}", headers={"Range": "bytes=0-99"})
    assert status == 206 and len(part) == 100 and headers["content-type"] == "video/iso.segment"
    status, headers, body = live.get("/aaa/1/hls/subs/1.vtt")
    assert status == 200 and headers["content-type"].startswith("text/vtt") and b"Hello" in body
    status, _, body = live.get("/aaa/1/hls/burn/2/master.m3u8")
    assert status == 200 and b'URI="../../base/a1.m3u8"' in body
    # Every file each playlist names, as a browser resolves it, through Caddy. (Found on the
    # TV: the burned-in version named "%v_init.mp4", which only a reader of the disk finds.)
    for master in ("/aaa/1/hls/base/master.m3u8", "/aaa/1/hls/burn/2/master.m3u8"):
        fetched = _fetch_all(live, master)
        assert any(url.endswith("_init.mp4") for url in fetched), fetched
        assert sum(url.endswith(".m3u8") for url in fetched) >= 4, fetched  # video, a1, a2
    status, headers, body = live.get("/aaa/assets/hls.min.js")
    assert status == 200 and headers["content-type"].startswith("text/javascript") and body

    # Only the files of the versions: not the record of the source, nothing outside.
    for target in (
        "/aaa/1/hls/meta.json",
        "/aaa/1/hls/base/../meta.json",
        "/aaa/1/hls/base",
        "/aaa/1/hls/subs/1.vtt/x",
        "/aaa/assets/dashboard.js",
        "/aaa/2/hls/base/master.m3u8",
    ):
        assert live.get(target)[0] == 404, target


def test_a_browser_stream_makes_its_copy_and_says_so_meanwhile(
    live: Live, sample: Path, tmp_path: Path
) -> None:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is not installed on this host")
    film = tmp_path / "copy of sample.mkv"
    shutil.copy(sample, film)
    info = live.api("stream/start", {"file": str(film), "alias": "aaa", "mode": "browser"})
    assert info["stream"]["policy"] == "approval"
    # Approval mode: a browser waits with a code, which the dashboard shows.
    status, headers, body = live.get("/aaa", headers=HTML)
    code = live.api("status")["stream"]["devices"][0]["code"]
    assert status == 403 and code.encode() in body
    first = _cookie(headers)
    status, headers, body = live.get("/aaa", headers=HTML)
    assert status == 403 and code.encode() not in body, "each new browser waits with its own"
    browser = {**HTML, "Cookie": first}
    (device,) = [d for d in live.api("status")["stream"]["devices"] if d["code"] == code]
    assert device["kind"] == "browser"
    live.api("devices", {"device": device["id"], "allowed": True})
    assert live.get("/aaa", headers=HTML)[0] == 403, "approved: that browser, not the address"
    status, _, body = live.get("/aaa", headers=browser)
    assert status in (200, 503)  # done already on a fast host
    jobs = _wait_prepared(live)
    assert {j["task"] for j in jobs} == {"base version (copy)", "subtitle 1 (soft)"}
    assert live.get("/aaa", headers=browser)[0] == 200
    listed = live.api("prepared")["prepared"]
    assert [(e["source"], e["in_use"]) for e in listed if e["source"] == str(film)] == [
        (str(film), True)
    ]
    assert live.api("prepared/rm", {"path": str(film)})["kept"] == [str(film)]


def _cookie(headers: dict[str, str]) -> str:
    """The approval cookie a response sets, as a browser sends it back."""
    return headers["set-cookie"].split(";")[0]


def test_a_tv_is_known_by_its_cookie_and_obeys_through_caddy(live: Live, sample: Path) -> None:
    """The cookie, the player's reports and the owner's commands over real HTTP: Caddy must
    pass the cookie on, return the one the gate sets, and send a report's answer whole."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is not installed on this host")
    live.api("prepare", {"file": str(sample), "sub": "1", "confirmed": True})
    _wait_prepared(live)
    live.api("stream/start", {"file": str(sample), "alias": "aaa", "mode": "browser"})
    status, headers, _ = live.get("/aaa", headers=HTML)
    assert status == 403
    set_cookie = headers["set-cookie"]
    assert "HttpOnly" in set_cookie and "SameSite=Lax" in set_cookie
    tv = {"Cookie": _cookie(headers)}
    device = live.api("status")["stream"]["devices"][0]
    live.api("devices", {"device": device["id"], "allowed": True})

    status, headers, body = live.get("/aaa", headers={**HTML, **tv})
    assert status == 200 and "set-cookie" not in headers
    page = json.loads(body.decode().split('type="application/json">')[1].split("</script>")[0])
    assert page["report"] == "/aaa/1/client" and page["start"] == 0
    # Segments carry the cookie and no page is asked for: Caddy sends them.
    assert live.get("/aaa/1/hls/base/master.m3u8", headers=tv)[0] == 200
    assert live.get("/aaa/1/hls/base/master.m3u8")[0] == 403, "not without it"

    report = page["report"]
    tv_player = {**tv, "X-Vpstream-Player": "1"}  # as player.js sends a report
    status, headers, body = live.get(f"{report}?t=4.5&d=12&p=0&a=1&s=off&ack=0", headers=tv_player)
    assert status == 200 and headers["content-type"].startswith("application/json")
    assert headers["cache-control"] == "no-store" and json.loads(body) == {"commands": []}
    live.api("clients", {"devices": [device["id"]], "audio": "2", "seek": "0:08"})
    status, _, body = live.get(f"{report}?t=5&ack=0", headers=tv_player)
    assert json.loads(body) == {"commands": [{"seq": 1, "audio": 2}, {"seq": 2, "seek": 8.0}]}
    live.get(f"{report}?t=8&a=2&ack=2", headers=tv_player)
    (shown,) = [d for d in live.api("status")["stream"]["devices"] if d["id"] == device["id"]]
    assert shown["client"]["audio"] == 2 and shown["client"]["pending"] == {}
    assert shown["client"]["position"] == 8

    # Another browser on the same address is a stranger: it waits, and has no record.
    status, headers, _ = live.get("/aaa", headers=HTML)
    assert status == 403 and _cookie(headers) != tv["Cookie"]
    stranger = {"Cookie": _cookie(headers)}
    assert live.get(f"{report}?t=1", headers={**stranger, "X-Vpstream-Player": "1"})[0] == 403
    assert (
        live.get(
            f"{report}?t=1", headers={"Cookie": "vpstream=" + "A" * 43, "X-Vpstream-Player": "1"}
        )[0]
        == 403
    )
    # Without the header it is no report, even with the cookie (a request from another site).
    assert live.get(f"{report}?ack=99", headers=tv)[0] == 404
