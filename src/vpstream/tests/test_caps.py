from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from vpstream.caps import clips, server
from vpstream.tests.fakes import FakeRunner


@pytest.mark.parametrize(
    ("header", "size", "expected"),
    [
        (None, 100, None),
        ("bytes=0-9", 100, (0, 9)),
        ("bytes=90-", 100, (90, 99)),
        ("bytes=-10", 100, (90, 99)),
        ("bytes=-500", 100, (0, 99)),
        ("bytes=50-500", 100, (50, 99)),
        ("bytes=5-3", 100, None),  # malformed: ignored, whole file
        ("bytes=0-1,5-6", 100, None),
        ("items=0-1", 100, None),
    ],
)
def test_parse_range(header: str | None, size: int, expected: object) -> None:
    assert server.parse_range(header, size) == expected


@pytest.mark.parametrize(
    ("header", "size"), [("bytes=100-", 100), ("bytes=-0", 100), ("bytes=0-", 0)]
)
def test_parse_range_not_satisfiable(header: str, size: int) -> None:
    with pytest.raises(server.RangeNotSatisfiable):
        server.parse_range(header, size)


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
    httpd = server.make_server("127.0.0.1", 0, server.make_handler(state))
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


def _post(port: int, body: bytes, headers: dict[str, str] | None = None) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/caps/report", body=body, headers=headers or {})
    status = conn.getresponse().status
    conn.close()
    return status


def test_report_cannot_overwrite_server_fields(caps_server: tuple[int, server.CapsState]) -> None:
    port, state = caps_server
    body = json.dumps({"session": "s", "client": "spoof", "user_agent": "spoof", "x": 1})
    assert _post(port, body.encode(), {"User-Agent": "TV"}) == 200
    saved = json.loads(state.results_path.read_text())["s"]
    assert saved["client"] == "127.0.0.1" and saved["user_agent"] == "TV" and saved["x"] == 1


def test_report_with_odd_shapes_is_still_answered(
    caps_server: tuple[int, server.CapsState],
) -> None:
    port, _ = caps_server
    body = json.dumps({"session": "s", "playback": [1, {"name": {"a": 1}}], "stage": ["x"]})
    assert _post(port, body.encode()) == 200


def test_summarize_accepts_any_shape() -> None:
    assert server.summarize({"playback": "nope", "speed": {"best_mbps": 9}}) == [
        f"  {'speed':<28} " + '{"best_mbps": 9}'
    ]
    assert server.summarize({"playback": [None, {"name": 3, "result": "pass"}]}) == [
        f"  {'3':<28} pass"
    ]


def test_report_sessions_are_capped(
    caps_server: tuple[int, server.CapsState], monkeypatch: pytest.MonkeyPatch
) -> None:
    port, _ = caps_server
    monkeypatch.setattr(server, "MAX_SESSIONS", 2)
    assert [_post(port, json.dumps({"session": f"s{n}"}).encode()) for n in range(3)] == [
        200,
        200,
        429,
    ]
    assert _post(port, json.dumps({"session": "s0", "stage": "again"}).encode()) == 200


def test_report_length_errors_close_the_connection(
    caps_server: tuple[int, server.CapsState],
) -> None:
    port, _ = caps_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest("POST", "/caps/report")
    conn.putheader("Content-Length", "0")
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 400 and resp.getheader("Connection") == "close"
    conn.close()
    big = b"x" * (server.MAX_REPORT_BYTES + 1)
    assert _post(port, big) == 413


def test_report_save_failure_is_a_500(
    caps_server: tuple[int, server.CapsState], tmp_path: Path
) -> None:
    port, state = caps_server
    state.results_path = tmp_path / "missing" / "r.json"
    assert _post(port, json.dumps({"session": "s"}).encode()) == 500
    assert _post(port, json.dumps({"session": "s"}).encode()) == 500  # and the server lives on


def test_head_sends_headers_only(caps_server: tuple[int, server.CapsState]) -> None:
    port, _ = caps_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("HEAD", "/caps/media/a.mp4")
    resp = conn.getresponse()
    assert resp.status == 200 and resp.getheader("Content-Length") == "1024"
    assert resp.read() == b""
    conn.close()


def test_missing_manifest_is_a_404(caps_server: tuple[int, server.CapsState]) -> None:
    port, state = caps_server
    (state.clip_dir / "clips.json").unlink()
    assert _get(port, "/caps/clips.json")[0] == 404


def test_ipv6_server_binds() -> None:
    try:
        httpd = server.make_server("::1", 0, server.make_handler(_state(Path("."))))
    except OSError:
        pytest.skip("no IPv6 on this host")
    assert isinstance(httpd, server.CapsServer6)
    httpd.server_close()


def _state(root: Path) -> server.CapsState:
    return server.CapsState(clip_dir=root, results_path=root / "r.json", log=lambda _m: None)


def _clip_runner() -> FakeRunner:
    r = FakeRunner()
    r.on("ffmpeg", "-hide_banner", "-encoders", stdout=" V....D libx264  H.264\n")
    return r


def test_generate_refuses_a_folder_it_did_not_make(tmp_path: Path) -> None:
    mine = tmp_path / "videos"
    mine.mkdir()
    (mine / "film.mkv").write_text("x")
    for regen in (False, True):
        with pytest.raises(clips.ClipDirError):
            clips.generate(_clip_runner(), mine, regen=regen)
    # Refused before ffmpeg is even looked for.
    with pytest.raises(clips.ClipDirError):
        clips.generate(FakeRunner(available=()), mine)
    assert (mine / "film.mkv").is_file()


def test_generate_uses_an_empty_folder_and_regenerates_its_own(tmp_path: Path) -> None:
    out = tmp_path / "c"
    out.mkdir()
    r = _clip_runner()
    manifest = clips.generate(r, out, nice=False)
    assert (out / clips.MARKER).is_file()
    h264 = next(c for c in manifest["clips"] if c["id"] == "h264-8")  # type: ignore[union-attr]
    assert h264["encoder"] == "libx264"
    # The fake writes no files, so the extras are recorded as errors, not crashes.
    assert set(manifest["errors"]) == {"long", "multi_audio"}  # type: ignore[arg-type]
    (out / "stale.txt").write_text("x")
    clips.generate(r, out, regen=True)
    assert not (out / "stale.txt").exists()


def test_generate_replaces_a_corrupt_manifest(tmp_path: Path) -> None:
    out = tmp_path / "c"
    out.mkdir()
    (out / clips.MARKER).write_text("")
    (out / "clips.json").write_text("{truncated")
    assert "clips" in clips.generate(_clip_runner(), out)


def test_generate_runs_at_low_priority(tmp_path: Path) -> None:
    r = _clip_runner()
    r.available |= {"nice", "ionice"}
    clips.generate(r, tmp_path / "c")
    encodes = [c for c in r.calls if "-encoders" not in c]
    assert encodes and all(c[:3] == ["nice", "-n", "19"] for c in encodes)


def test_generate_records_a_timeout(tmp_path: Path) -> None:
    r = _clip_runner()
    r.on("ffmpeg", "-nostdin", returncode=124, stderr="stopped: no result after 600 seconds")
    manifest = clips.generate(r, tmp_path / "c")
    first = manifest["clips"][0]  # type: ignore[index]
    assert "600 seconds" in first["error"]


def test_a_clip_set_made_while_ffmpeg_failed_is_not_reused(tmp_path: Path) -> None:
    out = tmp_path / "c"
    broken = clips.generate(FakeRunner(), out)  # `ffmpeg -encoders` listed nothing
    assert not any(c["mp4"] for c in broken["clips"])  # type: ignore[union-attr]
    manifest = clips.generate(_clip_runner(), out)
    assert manifest["clips"][0]["encoder"] == "libx264"  # type: ignore[index]
    r = _clip_runner()
    assert clips.generate(r, out) == manifest and r.calls == []  # a good set is reused
