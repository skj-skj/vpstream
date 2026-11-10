from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from flask.testing import FlaskClient

from vpstream import api, config, public
from vpstream.control import Control
from vpstream.tests.fakes import FakeFirewall, FakeServer

IP = {"X-Vpstream-Ip": "192.0.2.1"}
JSON = {"Content-Type": "application/json"}


@pytest.fixture
def control(tmp_path: Path) -> Control:
    return Control(
        config.Config(stream_host="192.0.2.9"),
        config.Paths.under(tmp_path / "root"),
        FakeFirewall(),
        FakeServer(),
        log=lambda m: None,
    )


@pytest.fixture
def show(tmp_path: Path) -> Path:
    folder = tmp_path / "a show"
    folder.mkdir()
    for name in ("e2 <b>.mkv", "e1 100%?.mp4"):
        (folder / name).write_bytes(b"x")
    return folder


@pytest.fixture
def viewer(control: Control) -> FlaskClient:
    return public.create_app(control).test_client()


@pytest.fixture
def dash(control: Control) -> FlaskClient:
    return api.create_app(control.status, control).test_client()


# --- what viewers get


def test_a_video_is_answered_with_the_file_for_caddy_to_send(
    control: Control, viewer: FlaskClient, show: Path
) -> None:
    control.start({"dir": str(show), "alias": "aaa"})
    answer = viewer.get("/aaa/1", headers=IP)
    assert answer.status_code == 200 and answer.data == b""
    assert answer.headers["X-Vpstream-File"] == f"{show}/e1 100%?.mp4".replace(" ", "%20").replace(
        "%?", "%25%3F"
    )
    assert answer.headers["X-Vpstream-Type"] == "video/mp4"
    assert answer.headers["Cache-Control"] == "no-store"
    assert viewer.head("/aaa/2", headers=IP).headers["X-Vpstream-Type"] == "video/x-matroska"


def test_a_browser_gets_the_index_and_a_player_the_playlist(
    control: Control, viewer: FlaskClient, show: Path
) -> None:
    control.start({"dir": str(show), "alias": "aaa"})
    page = viewer.get("/aaa", headers={**IP, "Accept": "text/html,application/xhtml+xml,*/*"})
    assert page.mimetype == "text/html"
    text = page.get_data(as_text=True)
    assert '<a href="/aaa/2"><b>2</b> e2 &lt;b&gt;.mkv</a>' in text and "<b>.mkv" not in text
    assert "X-Vpstream-File" not in page.headers

    for accept in ("*/*", ""):  # VLC and friends: anything will do
        listing = viewer.get("/aaa", headers={**IP, "Accept": accept, "Host": "192.0.2.9:1337"})
        assert listing.mimetype == "audio/x-mpegurl"
        assert listing.get_data(as_text=True).splitlines() == [
            "#EXTM3U",
            "#EXTINF:-1,e1 100%?.mp4",
            "http://192.0.2.9:1337/aaa/1",
            "#EXTINF:-1,e2 <b>.mkv",
            "http://192.0.2.9:1337/aaa/2",
        ]
    named = viewer.get("/aaa/playlist.m3u", headers={**IP, "Accept": "text/html"})
    assert named.mimetype == "audio/x-mpegurl"


def test_playlist_entries_never_carry_a_made_up_host() -> None:
    for host in ("evil.example/x?", "a b", "", "x" * 300, "h\r\nX: y"):
        lines = public.playlist("aaa", ["one\ntwo.mkv"], host).splitlines()
        assert lines == ["#EXTM3U", "#EXTINF:-1,one two.mkv", "/aaa/1"], host
    assert public.playlist("aaa", ["a.mkv"], "[2001:db8::1]:1337").splitlines()[2] == (
        "http://[2001:db8::1]:1337/aaa/1"
    )


def test_refusals(control: Control, viewer: FlaskClient, show: Path) -> None:
    assert viewer.get("/aaa", headers=IP).status_code == 404  # no stream
    control.start({"dir": str(show), "alias": "aaa", "policy": "approval"})
    refused = viewer.get("/aaa/1", headers={**IP, "Accept": "text/html"})
    assert refused.status_code == 403 and refused.mimetype == "text/plain"
    assert "X-Vpstream-File" not in refused.headers
    assert viewer.get("/", headers=IP).status_code == 404
    assert viewer.get("/bbb", headers=IP).status_code == 404
    assert viewer.get("/aaa//1/", headers=IP).status_code == 403  # no redirect gives it away


def test_the_address_comes_from_caddy_only(
    control: Control, viewer: FlaskClient, show: Path
) -> None:
    control.start({"dir": str(show), "alias": "aaa"})
    assert viewer.get("/aaa").status_code == 400  # not through Caddy
    assert viewer.get("/aaa", headers={"X-Vpstream-Ip": "nonsense"}).status_code == 400
    viewer.get("/aaa", headers={**IP, "X-Forwarded-For": "203.0.113.50", "User-Agent": "VLC/3"})
    (device,) = control.status()["stream"]["devices"]
    assert device["ip"] == "192.0.2.1" and device["user_agent"] == "VLC/3"


def test_viewers_cannot_change_anything(control: Control, viewer: FlaskClient, show: Path) -> None:
    control.start({"dir": str(show), "alias": "aaa"})
    assert viewer.post("/aaa", headers=IP).status_code == 405
    assert viewer.post("/api/v1/stream/stop", headers={**IP, **JSON}, data="{}").status_code == 405
    assert viewer.get("/api/v1/status", headers=IP).status_code == 404
    assert control.stream is not None


def test_a_failure_does_not_show_its_insides(
    control: Control, viewer: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any) -> None:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(control, "visit", boom)
    answer = viewer.get("/aaa", headers=IP)
    assert answer.status_code == 500 and b"secret" not in answer.data


# --- the API


def test_start_change_and_stop_through_the_api(
    control: Control, dash: FlaskClient, show: Path
) -> None:
    started = dash.post("/api/v1/stream/start", json={"dir": str(show), "alias": "aaa", "ttl": 5})
    assert started.status_code == 200
    info = started.get_json()
    assert info["stream"]["url"] == "http://192.0.2.9:1337/aaa" and info["now"]
    assert [v["name"] for v in info["stream"]["videos"]] == ["e1 100%?.mp4", "e2 <b>.mkv"]

    assert dash.post("/api/v1/stream/alias", json={"alias": "bbb"}).get_json()["stream"][
        "alias"
    ] == ("bbb")
    assert (
        dash.post("/api/v1/stream/policy", json={"policy": "locked"}).get_json()["stream"]["policy"]
        == "locked"
    )
    enabled = dash.post("/api/v1/devices", json={"ip": "192.0.2.4", "allowed": True}).get_json()
    assert enabled["stream"]["devices"][0]["passes"] is True
    assert dash.post("/api/v1/stream/unlock", json={}).get_json()["stream"]["policy"] == "open"
    assert dash.get("/api/v1/status").get_json()["stream"]["alias"] == "bbb"
    assert dash.post("/api/v1/stream/stop", json={}).get_json()["stream"] is None


@pytest.mark.parametrize(
    ("route", "body", "message"),
    [
        ("stream/start", {"file": "/no/such.mkv"}, "not a file"),
        ("stream/start", {}, "exactly one"),
        ("stream/stop", {}, "no stream is running"),
        ("stream/alias", {"alias": "x"}, "no stream is running"),
        ("stream/alias", {}, "not a usable alias"),
        ("devices", {"ip": "192.0.2.1"}, "needs an ip"),
    ],
)
def test_refusals_are_json_with_the_reason(
    dash: FlaskClient, route: str, body: dict[str, Any], message: str
) -> None:
    answer = dash.post(f"/api/v1/{route}", json=body)
    assert answer.status_code == 400 and message in answer.get_json()["error"]


def test_bad_device_address_is_a_refusal(control: Control, dash: FlaskClient, show: Path) -> None:
    control.start({"dir": str(show)})
    answer = dash.post("/api/v1/devices", json={"ip": "example.com", "allowed": True})
    assert answer.status_code == 400 and "not an IP address" in answer.get_json()["error"]


def test_a_host_failure_is_a_503(control: Control, dash: FlaskClient, show: Path) -> None:
    control.server.fail = "exit 1"  # type: ignore[union-attr]
    answer = dash.post("/api/v1/stream/start", json={"dir": str(show)})
    assert answer.status_code == 503 and "Caddy could not be started" in answer.get_json()["error"]


@pytest.mark.parametrize(
    "send",
    [
        {"data": "alias=bbb", "content_type": "application/x-www-form-urlencoded"},
        {"data": '{"alias": "bbb"}', "content_type": "text/plain"},
        {"data": "[]", "content_type": "application/json"},
        {"data": "{", "content_type": "application/json"},
        {},
    ],
)
def test_changes_take_json_only(
    control: Control, dash: FlaskClient, show: Path, send: dict[str, Any]
) -> None:
    """A form on another site can post these types without asking; none may change anything."""
    control.start({"dir": str(show), "alias": "aaa"})
    for route in ("stream/alias", "stream/stop", "stream/policy", "stream/unlock", "devices"):
        answer = dash.post(f"/api/v1/{route}", **send)
        assert answer.status_code == 400 and "JSON" in answer.get_json()["error"], route
    assert control.status()["stream"]["alias"] == "aaa"


def test_changes_are_post_only_and_never_answer_another_site(
    control: Control, dash: FlaskClient
) -> None:
    for route in ("stream/start", "stream/stop", "devices"):
        assert dash.get(f"/api/v1/{route}").status_code == 405
    asked = dash.options(
        "/api/v1/stream/start",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "Access-Control-Allow-Origin" not in asked.headers


def test_without_control_there_are_no_change_routes() -> None:
    http = api.create_app(dict).test_client()
    assert http.post("/api/v1/stream/stop", json={}).status_code == 404


def test_an_unexpected_failure_is_json_too(
    control: Control, dash: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(control, "_save", lambda: (_ for _ in ()).throw(OSError("disk full")))
    answer = dash.post("/api/v1/stream/start", json={"file": __file__})
    assert answer.status_code == 500 and "disk full" in answer.get_json()["error"]


# --- the dashboard page


def test_dashboard_page_and_script_are_served(dash: FlaskClient) -> None:
    page = dash.get("/")
    assert page.mimetype == "text/html" and b'<script src="/assets/dashboard.js">' in page.data
    script = dash.get("/assets/dashboard.js")
    assert script.mimetype == "text/javascript" and b"stream/start" in script.data
    assert dash.get("/assets/hls.min.js").status_code == 404  # only what is listed


def test_dashboard_script_never_builds_html_from_server_text(dash: FlaskClient) -> None:
    script = dash.get("/assets/dashboard.js").get_data(as_text=True)
    for risky in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert risky not in script
    page = dash.get("/").get_data(as_text=True)
    assert "http://" not in page and "https://" not in page  # nothing loads from elsewhere
