from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from vpstream.caps import clips, server


@pytest.mark.parametrize(
    ("header", "size", "expected"),
    [
        (None, 100, None),
        ("bytes=0-9", 100, (0, 9)),
        ("bytes=90-", 100, (90, 99)),
        ("bytes=-10", 100, (90, 99)),
        ("bytes=50-500", 100, (50, 99)),
        ("bytes=100-", 100, "invalid"),
        ("bytes=-0", 100, "invalid"),
        ("bytes=0-1,5-6", 100, None),
        ("items=0-1", 100, None),
    ],
)
def test_parse_range(header: str | None, size: int, expected: object) -> None:
    assert server.parse_range(header, size) == expected


def test_media_file_stays_inside_root(tmp_path: Path) -> None:
    (tmp_path / "a.mp4").write_bytes(b"x")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "index.m3u8").write_text("#EXTM3U")
    assert server.media_file(tmp_path, "a.mp4") == (tmp_path / "a.mp4").resolve()
    assert server.media_file(tmp_path, "d/index.m3u8") is not None
    for bad in ("../a.mp4", "/etc/passwd", "d/../../x", "d/x/y.mp4", ".hidden", "missing.mp4"):
        assert server.media_file(tmp_path, bad) is None


def test_webvtt_text() -> None:
    text = clips.webvtt_text(6, 2)
    assert text.startswith("WEBVTT")
    assert "00:00:02.000 --> 00:00:04.000\nSubtitle line 2" in text
    assert text.count("-->") == 3


def test_multi_audio_args_names_both_tracks() -> None:
    args = clips.multi_audio_args(Path("/o"))
    vmap = args[args.index("-var_stream_map") + 1]
    assert "language:eng" in vmap and "language:jpn" in vmap and "default:yes" in vmap
    assert args[args.index("-master_pl_name") + 1] == "master.m3u8"


@pytest.fixture
def caps_server(tmp_path: Path) -> Iterator[tuple[int, server.CapsState]]:
    clip_dir = tmp_path / "clips"
    clip_dir.mkdir()
    (clip_dir / "clips.json").write_text(json.dumps({"clips": []}))
    (clip_dir / "a.mp4").write_bytes(bytes(range(256)) * 4)
    logs: list[str] = []
    state = server.CapsState(clip_dir=clip_dir, results_path=tmp_path / "r.json", log=logs.append)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(state))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1], state
    httpd.shutdown()
    httpd.server_close()


def _get(
    port: int, path: str, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body


def test_server_routes(caps_server: tuple[int, server.CapsState]) -> None:
    port, _ = caps_server
    status, headers, _ = _get(port, "/")
    assert status == 302 and headers["location"] == "/caps"
    status, headers, body = _get(port, "/caps")
    assert status == 200 and b"capability test" in body
    status, _, body = _get(port, "/caps/hls.min.js")
    assert status == 200 and len(body) > 100_000
    status, _, body = _get(port, "/caps/clips.json")
    assert json.loads(body) == {"clips": []}
    assert _get(port, "/caps/media/../clips.json")[0] == 404
    assert _get(port, "/nothing")[0] == 404


def test_server_range_requests(caps_server: tuple[int, server.CapsState]) -> None:
    port, _ = caps_server
    status, headers, body = _get(port, "/caps/media/a.mp4", {"Range": "bytes=10-19"})
    assert status == 206
    assert headers["content-range"] == "bytes 10-19/1024"
    assert body == bytes(range(10, 20))
    status, headers, body = _get(port, "/caps/media/a.mp4")
    assert status == 200 and len(body) == 1024 and headers["accept-ranges"] == "bytes"
    assert _get(port, "/caps/media/a.mp4", {"Range": "bytes=5000-"})[0] == 416


def test_speed_endpoint_is_capped(caps_server: tuple[int, server.CapsState]) -> None:
    port, _ = caps_server
    status, headers, body = _get(port, "/caps/speed?mb=2")
    assert status == 200 and len(body) == 2 * server.CHUNK
    _, headers, _ = _get(port, "/caps/speed?mb=nope")
    assert headers["content-length"] == str(8 * server.CHUNK)


def test_report_is_saved(caps_server: tuple[int, server.CapsState]) -> None:
    port, state = caps_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    payload = json.dumps(
        {
            "session": "s1",
            "stage": "playback",
            "playback": [{"name": "H.264 8-bit MP4", "result": "pass"}],
        }
    )
    conn.request(
        "POST",
        "/caps/report",
        body=payload,
        headers={"Content-Type": "application/json", "User-Agent": "TV"},
    )
    assert conn.getresponse().status == 200
    conn.close()
    saved = json.loads(state.results_path.read_text())
    assert saved["s1"]["user_agent"] == "TV"
    assert saved["s1"]["playback"][0]["result"] == "pass"


def test_report_rejects_bad_input(caps_server: tuple[int, server.CapsState]) -> None:
    port, _ = caps_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/caps/report", body="[1,2]")
    assert conn.getresponse().status == 400
    conn.close()
