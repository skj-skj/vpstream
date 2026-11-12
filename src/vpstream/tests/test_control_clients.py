"""Client control in browser mode: the approval cookie, the players' reports and the owner's
commands, through the daemon's control and its public app, with a stand-in ffmpeg."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from vpstream import api, control, gate, player, public, state
from vpstream.stream import Answer, StreamError
from vpstream.tests.test_control_browser import Rig

TV, PHONE = "192.0.2.10", "192.0.2.20"
PLAYER = {player.REPORT_HEADER: "1"}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Viewers(Rig):
    """A rig with a clock the test moves, and browsers that keep their cookies."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.clock = Clock()
        self.control.clock = self.clock
        self.cookies: dict[str, str] = {}

    def again(self) -> control.Control:
        made = super().again()
        if hasattr(self, "clock"):
            made.clock = self.clock
        return made

    def start_browser(self, **options: Any) -> None:
        self.start(policy="approval", **options)
        assert self.preparer.wait()

    def visit(
        self, who: str, path: str = "aaa", ip: str | None = None, html: bool = True, **query: Any
    ) -> Answer:
        """A request of browser `who`, which sends the cookie it was given and keeps a new one."""
        answer = self.control.visit(
            path,
            ip or {"tv": TV, "phone": PHONE}.get(who, TV),
            who,
            html,
            self.cookies.get(who),
            {k: str(v) for k, v in query.items()},
        )
        if answer.cookie:
            self.cookies[who] = answer.cookie
        return answer

    def device(self, who: str) -> gate.Device:
        assert self.control.stream is not None
        found = self.control.stream.devices.browser(self.cookies.get(who))
        assert found is not None, who
        return found

    def let_in(self, *who: str) -> None:
        for name in who:
            if name not in self.cookies:
                self.visit(name)
            self.control.set_device(self.device(name).id, True)

    def report(self, who: str, number: int = 1, **query: Any) -> list[dict[str, Any]]:
        answer = self.visit(who, f"aaa/{number}/client", html=False, **query)
        assert answer.kind == "client", answer
        return list(answer.data["commands"])

    def ask(self, *who: str, **change: Any) -> dict[str, Any]:
        return self.control.set_clients({"devices": [self.device(w).id for w in who], **change})


@pytest.fixture
def rig(tmp_path: Path) -> Viewers:
    return Viewers(tmp_path)


def test_a_tv_waits_is_let_in_by_its_id_and_plays(rig: Viewers) -> None:
    rig.start_browser()
    first = rig.visit("tv")
    assert (first.kind, first.status) == ("waiting", 403) and first.cookie
    device = rig.device("tv")
    assert first.data["code"] == device.code
    rig.control.set_device(device.id, True)
    page = rig.visit("tv")
    assert page.kind == "player" and page.cookie == ""
    assert page.data["report"] == "/aaa/1/client" and page.data["start"] == 0
    # Its segments carry the cookie and are no page: they are let in all the same.
    assert rig.visit("tv", "aaa/1/hls/base/master.m3u8", html=False).kind == "file"


def test_a_changed_address_does_not_send_it_back_to_the_waiting_list(rig: Viewers) -> None:
    rig.start_browser()
    rig.let_in("tv")
    assert rig.visit("tv", ip="198.51.100.40").kind == "player"
    assert rig.device("tv").ip == "198.51.100.40"


def test_a_stranger_on_the_tvs_address_waits_and_sees_no_commands(rig: Viewers) -> None:
    rig.start_browser()
    rig.let_in("tv")
    stranger = rig.visit("stranger", ip=TV)
    assert stranger.kind == "waiting" and rig.cookies["stranger"] != rig.cookies["tv"]
    rig.ask("tv", audio="2")
    # Neither a report without the cookie, nor one with the stranger's, reaches the TV's.
    assert rig.visit("stranger", "aaa/1/client", html=False).kind == "denied"
    anonymous = rig.control.visit("aaa/1/client", TV, "curl", False, None, {})
    assert anonymous.kind == "denied"
    forged = rig.control.visit("aaa/1/client", TV, "curl", False, "A" * 43, {})
    assert forged.kind == "denied"
    assert rig.report("tv") == [{"seq": 1, "audio": 2}]


def test_a_player_let_in_by_address_has_no_record_to_report_to(rig: Viewers) -> None:
    rig.start_browser(alias="aaa")
    rig.control.set_device("192.0.2.77", True)  # VLC
    vlc = rig.control.visit("aaa", "192.0.2.77", "VLC/3", False, None, {})
    assert vlc.kind == "redirect"
    report = rig.control.visit("aaa/1/client", "192.0.2.77", "VLC/3", False, None, {"t": "5"})
    assert report.kind == "missing"
    assert rig.control.stream is not None
    assert not rig.control.stream.devices.guesser("192.0.2.77")


def test_reports_and_commands_go_round_and_the_record_follows_the_player(rig: Viewers) -> None:
    rig.start_browser()
    rig.let_in("tv")
    rig.visit("tv")
    assert rig.report("tv", t=600, d=5400, p=0, a=1, s="off", ack=0) == []
    info = rig.ask("tv", audio="2", sub="1-soft", seek="10:10")
    client = rig.device("tv").client
    assert client is not None and client.audio == 1, "not changed until the player says so"
    shown = next(d for d in info["stream"]["devices"] if d["kind"] == "browser")
    assert shown["client"]["pending"] == {"audio": 2, "sub": "1-soft", "seek": 610.0}
    commands = rig.report("tv", t=602, ack=0)
    assert commands == [
        {"seq": 1, "audio": 2},
        {"seq": 3, "seek": 610.0},
        {"seq": 2, "sub": "1-soft"},
    ]
    assert rig.report("tv", t=610, a=2, s="1-soft", ack=3) == []
    assert (client.audio, client.sub, client.position) == (2, "1-soft", 610.0)


def test_several_tvs_each_get_their_own_commands(rig: Viewers) -> None:
    rig.start_browser()
    rig.let_in("tv", "phone")
    rig.ask("tv", "phone", seek=60)
    rig.ask("phone", audio="off")
    assert rig.report("tv") == [{"seq": 1, "seek": 60.0}]
    assert rig.report("phone") == [{"seq": 2, "audio": 0}, {"seq": 1, "seek": 60.0}]
    rig.control.set_clients({"all": True, "name": "Same name"})
    assert {rig.device("tv").name, rig.device("phone").name} == {"Same name"}


def test_resume_and_a_start_position_the_owner_sets(rig: Viewers) -> None:
    rig.start_browser()
    rig.let_in("tv")
    rig.visit("tv")
    rig.report("tv", t=1234.5, d=5400)
    assert rig.visit("tv").data["start"] == 1234.5, "reopening continues where it left off"
    rig.ask("tv", start_at="10:10")
    assert rig.visit("tv").data["start"] == 610
    assert rig.visit("tv").data["start"] == 1234.5, "used once"


def test_a_page_loaded_while_commands_wait_starts_with_them(rig: Viewers) -> None:
    rig.start_browser()
    rig.let_in("tv")
    rig.visit("tv")
    rig.report("tv", t=100, d=5400, a=1, s="off")
    rig.ask("tv", audio="2", sub="1-soft", seek=300)
    page = rig.visit("tv").data
    assert (page["start"], page["start_audio"], page["ack"]) == (300, 1, 3)
    assert page["subs"][page["start_sub"]]["id"] == "1-soft"
    # Its first report says it has seen them, so they are not done twice.
    assert rig.report("tv", ack=page["ack"]) == []


def test_the_dashboard_is_offered_the_tracks_of_each_video(rig: Viewers) -> None:
    rig.start_browser()
    (video,) = rig.control.status()["stream"]["videos"]
    assert video["menu"]["audio"] == [
        {"value": "1", "label": "English"},
        {"value": "2", "label": "Japanese"},
    ]
    assert {"value": "1-soft", "label": "English"} in video["menu"]["subs"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({}, "say what to change"),
        ({"audio": "eng"}, "not an audio track"),
        ({"sub": "2"}, "not a subtitle choice"),
        ({"seek": "soon"}, "not a time"),
        ({"start_at": 30, "video": 2}, "not a video of this stream"),
        ({"start_at": 30, "video": True}, "not a video of this stream"),
        ({"name": 5}, "not a name"),
        ({"volume": 11}, "unknown option"),
    ],
)
def test_client_changes_that_are_refused(
    rig: Viewers, change: dict[str, Any], message: str
) -> None:
    rig.start_browser()
    rig.let_in("tv")
    with pytest.raises(StreamError, match=message):
        rig.ask("tv", **change)
    assert rig.device("tv").client is None, "nothing was changed"


def test_client_changes_name_browsers_of_this_stream_only(rig: Viewers) -> None:
    rig.start_browser()
    with pytest.raises(StreamError, match="no browser watches"):
        rig.control.set_clients({"all": True, "seek": 1})
    rig.let_in("tv")
    rig.control.set_device("192.0.2.77", True)
    for ids in (["192.0.2.77"], ["b-000000"], [], "b-1", [5]):
        with pytest.raises(StreamError):
            rig.control.set_clients({"devices": ids, "seek": 1})
    # One bad id among good ones changes none of them.
    with pytest.raises(StreamError):
        rig.control.set_clients({"devices": [rig.device("tv").id, "b-000000"], "seek": 1})
    assert rig.device("tv").client is None


def test_all_means_the_browsers_that_get_in(rig: Viewers) -> None:
    rig.start_browser()
    rig.let_in("tv")
    rig.visit("stranger", ip=PHONE)  # waits
    rig.control.set_clients({"all": True, "seek": 5})
    assert rig.device("tv").client is not None
    assert rig.device("stranger").client is None


def test_direct_mode_has_no_client_control(rig: Viewers) -> None:
    rig.start(mode="direct", policy="approval")
    answer = rig.visit("tv")
    assert answer.cookie == "" and answer.kind == "waiting", "waits by its address, no cookie"
    rig.control.set_device(TV, True)
    assert rig.visit("tv").kind == "video"
    assert rig.visit("tv", "aaa/1/client", html=False, t=5).kind == "missing"
    with pytest.raises(StreamError, match="browser mode only"):
        rig.control.set_clients({"devices": [TV], "seek": 5})


def test_a_daemon_restart_keeps_cookies_records_and_positions(rig: Viewers) -> None:
    rig.start_browser()
    rig.let_in("tv")
    rig.visit("tv")
    rig.report("tv", t=900, d=5400, a=2)
    rig.ask("tv", sub="1-soft")
    rig.report("tv", t=950)
    rig.clock.now += control.SAVE_SECONDS
    rig.control.tick()  # writes down what the player said
    rig.again().reconcile()
    assert rig.control.stream is not None
    page = rig.visit("tv")
    assert page.kind == "player" and page.data["start"] == 950
    assert rig.report("tv") == [{"seq": 1, "sub": "1-soft"}]
    rig.ask("tv", audio="1")
    assert rig.device("tv").client.commands["audio"].seq == 2  # type: ignore[union-attr]


def test_what_viewers_change_is_written_down_now_and_then_not_each_time(rig: Viewers) -> None:
    rig.start_browser()
    rig.let_in("tv")
    saved = rig.paths.state_dir / state.FILE_NAME
    before = saved.stat().st_mtime_ns
    for n in range(20):
        rig.report("tv", t=100 + n)
        rig.control.tick()
    assert saved.stat().st_mtime_ns == before
    rig.clock.now += control.SAVE_SECONDS
    rig.control.tick()
    recorded = rig.recorded_stream()
    (tv,) = [d for d in recorded["devices"] if d["kind"] == "browser"]
    assert tv["client"]["position"] == 119
    assert "token" in tv and rig.cookies["tv"] not in saved.read_text()


def test_a_save_that_fails_is_not_tried_on_every_tick(rig: Viewers, monkeypatch: Any) -> None:
    rig.start_browser()
    rig.let_in("tv")
    rig.report("tv", t=5)
    calls: list[int] = []

    def broken(*_: Any) -> None:
        calls.append(1)
        raise OSError("disk full")

    monkeypatch.setattr(state, "save", broken)
    rig.clock.now += control.SAVE_SECONDS
    rig.control.tick()
    rig.control.tick()
    assert len(calls) == 1 and any("disk full" in line for line in rig.log)


def test_stop_forgets_every_cookie(rig: Viewers) -> None:
    rig.start_browser()
    rig.let_in("tv")
    rig.control.stop()
    rig.start_browser()
    again = rig.visit("tv")
    assert again.kind == "waiting" and again.cookie, "a new stream knows no old cookie"


def test_a_seek_asked_for_on_one_episode_is_not_done_on_the_next(tmp_path: Path) -> None:
    rig = Viewers(tmp_path)
    (rig.media / "e2.mkv").write_bytes(b"video")
    rig.control.start({"dir": str(rig.media), "alias": "aaa", "mode": "browser"})
    assert rig.preparer.wait()
    rig.let_in("tv")
    rig.report("tv", number=1, t=50)
    rig.ask("tv", seek=40, start_at=20, video=2)
    assert rig.report("tv", number=2) == []
    assert rig.visit("tv", "aaa/2").data["start"] == 20


# --- through the public app, as Caddy passes requests on


def test_the_cookie_is_sent_as_a_browser_needs_it(rig: Viewers) -> None:
    rig.start_browser()
    web = public.create_app(rig.control).test_client()
    headers = {"X-Vpstream-Ip": TV, "Accept": "text/html"}
    first = web.get("/aaa", headers=headers)
    assert first.status_code == 403
    cookie = first.headers["Set-Cookie"]
    assert cookie.startswith(f"{gate.COOKIE}=")
    for part in ("HttpOnly", "SameSite=Lax", "Path=/", f"Max-Age={public.COOKIE_SECONDS}"):
        assert part in cookie
    assert "Secure" not in cookie, "the stream is plain HTTP"
    value = cookie.split(";")[0].split("=", 1)[1]
    assert rig.control.stream is not None
    device = rig.control.stream.devices.browser(value)
    assert device is not None
    rig.control.set_device(device.id, True)
    web.set_cookie(gate.COOKIE, value)
    page = web.get("/aaa", headers=headers)
    assert page.status_code == 200 and "Set-Cookie" not in page.headers
    report = web.get("/aaa/1/client?t=12.5&d=60&p=0&ack=0", headers={"X-Vpstream-Ip": TV, **PLAYER})
    assert report.status_code == 200 and report.get_json() == {"commands": []}
    assert report.headers["Cache-Control"] == "no-store"
    assert device.client is not None and device.client.position == 12.5


def test_only_a_bit_of_a_report_is_read(rig: Viewers, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.start_browser()
    rig.let_in("tv")
    seen: list[dict[str, str] | None] = []
    real = rig.control.visit
    monkeypatch.setattr(rig.control, "visit", lambda *a: seen.append(a[5]) or real(*a))
    web = public.create_app(rig.control).test_client()
    web.set_cookie(gate.COOKIE, rig.cookies["tv"])
    junk = "&".join(f"k{n}={'9' * 500}" for n in range(200))
    answer = web.get(f"/aaa/1/client?t={'1' * 400}&{junk}", headers={"X-Vpstream-Ip": TV, **PLAYER})
    assert answer.status_code == 200
    (query,) = seen
    assert query is not None and len(query) == public.MAX_VALUES
    assert {len(v) for v in query.values()} == {public.MAX_VALUE}
    client = rig.device("tv").client
    assert client is not None and client.position == 0  # 32 digits are no time in a video


def test_a_report_without_the_players_header_is_not_one(rig: Viewers) -> None:
    """A page on another site can make the browser fetch the address with its cookie, but
    not add the header (that needs a preflight, which Caddy refuses)."""
    rig.start_browser()
    rig.let_in("tv")
    rig.ask("tv", seek=60)
    web = public.create_app(rig.control).test_client()
    web.set_cookie(gate.COOKIE, rig.cookies["tv"])
    forged = web.get("/aaa/1/client?ack=99&t=5000", headers={"X-Vpstream-Ip": TV})
    assert forged.status_code == 404
    assert rig.device("tv").client.commands, "nothing was acked"  # type: ignore[union-attr]
    wrong = web.get(
        "/aaa/1/client?ack=99", headers={"X-Vpstream-Ip": TV, player.REPORT_HEADER: "0"}
    )
    assert wrong.status_code == 404


def test_odd_digits_in_a_report_or_a_change_are_no_failure(rig: Viewers) -> None:
    rig.start_browser()
    rig.let_in("tv")
    web = public.create_app(rig.control).test_client()
    web.set_cookie(gate.COOKIE, rig.cookies["tv"])
    answer = web.get(
        "/aaa/1/client?ack=%C2%B2&a=%C2%B2&t=7", headers={"X-Vpstream-Ip": TV, **PLAYER}
    )
    assert answer.status_code == 200
    dash = api.create_app(rig.control.status, rig.control).test_client()
    refused = dash.post(
        "/api/v1/clients", json={"devices": [rig.device("tv").id], "audio": "\u00b2"}
    )
    assert refused.status_code == 400


def test_a_change_must_change_something(rig: Viewers) -> None:
    rig.start_browser()
    rig.let_in("tv")
    with pytest.raises(StreamError, match="say what to change"):
        rig.ask("tv", audio=None)
    with pytest.raises(StreamError, match="goes with start_at"):
        rig.ask("tv", seek=5, video=1)
    assert rig.device("tv").client is None


def test_a_used_start_position_and_a_new_cookie_are_written_down_at_the_next_tick(
    rig: Viewers,
) -> None:
    rig.start_browser()
    rig.let_in("tv")
    rig.ask("tv", start_at=610)
    assert rig.visit("tv").data["start"] == 610
    rig.visit("phone")  # a new browser, with a new cookie, within the same second
    rig.control.tick()  # written down on the next tick
    rig.again().reconcile()  # the daemon crashes and comes back
    assert rig.visit("tv").data["start"] == 0, "not used a second time"
    assert rig.visit("phone").cookie == "", "its cookie is known after the restart"


def test_the_dashboard_gets_the_tracks_of_the_videos_in_use_only(tmp_path: Path) -> None:
    rig = Viewers(tmp_path)
    for n in range(2, 6):
        (rig.media / f"e{n}.mkv").write_bytes(b"video")
    rig.control.start({"dir": str(rig.media), "alias": "aaa", "mode": "browser"})
    assert rig.preparer.wait()
    rig.let_in("tv")
    rig.report("tv", number=3)
    rig.ask("tv", start_at=10, video=5)
    videos = rig.control.status()["stream"]["videos"]
    assert [v["number"] for v in videos if v["menu"] is not None] == [1, 3, 5]


def test_enabling_an_address_lets_the_browser_waiting_there_in(rig: Viewers) -> None:
    rig.start_browser()
    assert rig.visit("tv").kind == "waiting"
    rig.control.set_device(TV, True)  # vpstream devices enable 192.0.2.10
    assert rig.visit("tv").kind == "player"
    assert rig.visit("stranger", ip=TV).kind == "waiting", "one that comes later still waits"


def test_the_dashboard_api_changes_clients(rig: Viewers) -> None:
    rig.start_browser()
    rig.let_in("tv")
    dash = api.create_app(rig.control.status, rig.control).test_client()
    device = rig.device("tv").id
    answer = dash.post("/api/v1/clients", json={"devices": [device], "seek": "1:00"})
    assert answer.status_code == 200
    assert rig.report("tv") == [{"seq": 1, "seek": 60.0}]
    refused = dash.post("/api/v1/clients", json={"devices": [device], "audio": "loud"})
    assert refused.status_code == 400 and "not an audio track" in refused.get_json()["error"]
    renamed = dash.post("/api/v1/devices", json={"device": device, "allowed": False})
    assert renamed.status_code == 200 and rig.visit("tv").kind == "denied"


def test_what_must_be_written_at_once_is_and_at_most_once_a_second(rig: Viewers) -> None:
    rig.start_browser()
    rig.let_in("tv")
    rig.ask("tv", start_at=610)
    rig.clock.now += control.NOW_SECONDS
    assert rig.visit("tv").data["start"] == 610
    rig.again().reconcile()  # a crash right after, with no tick between
    assert rig.visit("tv").data["start"] == 0

    def recorded() -> int:
        return len(rig.recorded_stream()["devices"])

    rig.clock.now += control.NOW_SECONDS
    before = recorded()
    for n in range(5):  # a flood of new browsers within one second
        rig.visit(f"b{n}", ip=f"198.51.100.{n}")
    assert recorded() == before + 1, "the first is written at once, the rest wait"
    rig.control.tick()
    assert recorded() == before + 5
