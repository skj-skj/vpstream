from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from flask.testing import FlaskClient

from vpstream import api, config, public
from vpstream.control import Control
from vpstream.tests import harness
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
    assert answer.headers["X-Vpstream-File"] == "1"
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
    refused = viewer.get("/aaa/1", headers=IP)
    assert refused.status_code == 403 and refused.mimetype == "text/plain"
    assert "X-Vpstream-File" not in refused.headers
    # A browser waits on a page with the code the dashboard shows next to its address.
    waiting = viewer.get("/aaa/1", headers={**IP, "Accept": "text/html"})
    code = control.status()["stream"]["devices"][0]["code"]
    assert waiting.status_code == 403 and waiting.mimetype == "text/html"
    assert f'class="code">{code}<' in waiting.text and 'http-equiv="refresh"' in waiting.text
    assert "X-Vpstream-File" not in waiting.headers
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
    enabled = dash.post("/api/v1/devices", json={"device": "192.0.2.4", "allowed": True}).get_json()
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
        ("devices", {"device": "192.0.2.1"}, "needs a device"),
    ],
)
def test_refusals_are_json_with_the_reason(
    dash: FlaskClient, route: str, body: dict[str, Any], message: str
) -> None:
    answer = dash.post(f"/api/v1/{route}", json=body)
    assert answer.status_code == 400 and message in answer.get_json()["error"]


def test_bad_device_address_is_a_refusal(control: Control, dash: FlaskClient, show: Path) -> None:
    control.start({"dir": str(show)})
    answer = dash.post("/api/v1/devices", json={"device": "example.com", "allowed": True})
    assert (
        answer.status_code == 400 and "not a device or an IP address" in answer.get_json()["error"]
    )


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
    def full(ip: object, allowed: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(control, "set_device", full)
    answer = dash.post("/api/v1/devices", json={"device": "192.0.2.1", "allowed": True})
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


# --- found in the review of milestone 2


def test_a_file_name_that_is_not_utf8_is_listed_and_served(
    control: Control, viewer: FlaskClient, tmp_path: Path
) -> None:
    import os

    folder = tmp_path / "odd"
    folder.mkdir()
    (folder / "ok.mkv").write_bytes(b"x")
    try:
        with open(os.fsencode(folder) + b"/caf\xe9.mkv", "wb"):
            pass
    except OSError:
        pytest.skip("this file system refuses names that are not UTF-8")
    info = control.start({"dir": str(folder), "alias": "aaa"})
    assert [v["name"] for v in info["stream"]["videos"]] == ["caf\ufffd.mkv", "ok.mkv"]
    page = viewer.get("/aaa", headers={**IP, "Accept": "text/html"})
    assert page.status_code == 200 and "caf\ufffd.mkv" in page.get_data(as_text=True)
    assert viewer.get("/aaa/playlist.m3u", headers=IP).status_code == 200
    video = viewer.get("/aaa/1", headers=IP)
    assert video.status_code == 200 and video.headers["X-Vpstream-File"] == "1"


def test_a_video_swapped_for_a_link_or_a_folder_after_the_start_is_not_served(
    control: Control, viewer: FlaskClient, show: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.mkv"
    secret.write_text("private")
    control.start({"dir": str(show), "alias": "aaa"})
    first = show / "e1 100%?.mp4"
    assert viewer.get("/aaa/1", headers=IP).status_code == 200
    first.unlink()
    first.symlink_to(secret)
    swapped = viewer.get("/aaa/1", headers=IP)
    assert swapped.status_code == 404 and "X-Vpstream-File" not in swapped.headers
    first.unlink()
    first.mkdir()
    (first / "index.html").write_text("a page")
    assert viewer.get("/aaa/1", headers=IP).status_code == 404
    first.joinpath("index.html").unlink()
    first.rmdir()
    assert viewer.get("/aaa/1", headers=IP).status_code == 404  # gone
    assert viewer.get("/aaa/2", headers=IP).status_code == 200


def test_the_status_of_an_answer_is_what_the_viewer_gets(
    control: Control, viewer: FlaskClient, show: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vpstream.stream import Answer

    for answer in (Answer(410, "missing"), Answer(451, "denied")):
        monkeypatch.setattr(control, "visit", lambda *a, answer=answer: answer)
        assert viewer.get("/aaa", headers=IP).status_code == answer.status


def test_a_guesser_is_shut_out_and_can_be_let_back_in(
    control: Control, viewer: FlaskClient, dash: FlaskClient, show: Path
) -> None:
    from vpstream import gate

    control.start({"dir": str(show), "alias": "aaa"})
    other = {"X-Vpstream-Ip": "192.0.2.50"}
    for n in range(gate.MAX_MISSES):
        assert viewer.get(f"/guess{n}", headers=IP).status_code == 404
    assert viewer.get("/aaa", headers=IP).status_code == 404  # the right one comes too late
    assert viewer.get("/aaa", headers=other).status_code == 200  # others are not touched
    info = dash.get("/api/v1/status").get_json()
    assert info["stream"]["guessers"] == ["192.0.2.1"]
    assert [d["ip"] for d in info["stream"]["devices"]] == ["192.0.2.50"]
    dash.post("/api/v1/devices", json={"device": "192.0.2.1", "allowed": True})
    assert viewer.get("/aaa", headers=IP).status_code == 200


def test_a_device_that_gets_in_is_never_a_guesser(
    control: Control, viewer: FlaskClient, show: Path
) -> None:
    from vpstream import gate

    control.start({"dir": str(show), "alias": "aaa"})
    assert viewer.get("/aaa", headers=IP).status_code == 200
    for _ in range(gate.MAX_MISSES * 2):  # a browser asks for its icon on every page
        assert viewer.get("/favicon.ico", headers=IP).status_code == 404
        assert viewer.get("/aaa/99", headers=IP).status_code == 404
    assert viewer.get("/aaa/1", headers=IP).status_code == 200


def test_a_waiting_device_is_not_counted_as_a_guesser_for_asking_again(
    control: Control, viewer: FlaskClient, show: Path
) -> None:
    from vpstream import gate

    control.start({"dir": str(show), "alias": "aaa", "policy": "approval"})
    for _ in range(gate.MAX_MISSES * 2):
        assert viewer.get("/aaa/1", headers=IP).status_code == 403
    control.set_device("192.0.2.1", True)
    assert viewer.get("/aaa/1", headers=IP).status_code == 200


def test_only_videos_can_be_started_from_the_dashboard_port(
    dash: FlaskClient, tmp_path: Path
) -> None:
    secret = tmp_path / "id_rsa"
    secret.write_text("key")
    hidden = tmp_path / "link.mkv"
    hidden.symlink_to(secret)  # a video name that leads to something else
    for path in (secret, hidden):
        answer = dash.post("/api/v1/stream/start", json={"file": str(path)})
        assert answer.status_code == 400 and "not a video file" in answer.get_json()["error"]
    assert dash.get("/api/v1/status").get_json()["stream"] is None


def test_the_dashboard_sends_no_time_limit_of_its_own_for_an_empty_field(
    dash: FlaskClient,
) -> None:
    script = dash.get("/assets/dashboard.js").get_data(as_text=True)
    assert 'ttl === "" ? null : Number(ttl)' in script
    assert b'value="180" required' in dash.get("/").data


def test_a_video_being_prepared_says_to_try_again(control: Control, tmp_path: Path) -> None:
    from vpstream.tests import mkvdata
    from vpstream.tests.fakes import FakeMkvmerge

    film = mkvdata.write(tmp_path / "film.mkv", indexed=[1])
    control.runner = FakeMkvmerge(tmp_path, "slow")
    control.start({"file": str(film), "alias": "aaa"})
    try:
        answer = public.create_app(control).test_client().get("/aaa", headers=IP)
        assert answer.status_code == 503 and answer.headers["Retry-After"] == "30"
        assert "being prepared" in answer.get_data(as_text=True)
        assert "X-Vpstream-File" not in answer.headers
    finally:
        control.stop()


# --- the dashboard keeps doing what it did (found while redesigning it)


def test_the_dashboard_uses_every_route_the_api_offers(dash: FlaskClient) -> None:
    import re

    script = dash.get("/assets/dashboard.js").get_data(as_text=True)
    rules = [rule.rule for rule in dash.application.url_map.iter_rules()]
    routes = {rule.removeprefix(api.API + "/") for rule in rules if rule.startswith(api.API)}
    used = set(re.findall(r'(?:change|request)\((?:"(?:GET|POST)", )?"([a-z/]+)"', script))
    assert routes and routes <= used, routes - used


def test_every_element_the_dashboard_script_looks_up_is_on_the_page(dash: FlaskClient) -> None:
    import re

    script = dash.get("/assets/dashboard.js").get_data(as_text=True)
    page = dash.get("/").get_data(as_text=True)
    ids = set(re.findall(r'\$\("([A-Za-z]+)"\)', script))
    missing = [i for i in sorted(ids) if f'id="{i}"' not in page]
    assert len(ids) > 30 and missing == []


def test_the_dashboard_behaves() -> None:
    """The real page and script in node with a small DOM of their own (dashboard_test.js):
    what they show for a status, and what each button and form sends."""
    harness.run("dashboard_test.js", "dashboard.html", "dashboard.js", at_least=15)


def test_the_player_behaves() -> None:
    """The real player page and scripts in node (player_test.js): where it starts, what it
    reports, and how the owner's commands and the viewer's own buttons meet."""
    harness.run("player_test.js", "player.html", "vtt.js", "player.js", at_least=10)
