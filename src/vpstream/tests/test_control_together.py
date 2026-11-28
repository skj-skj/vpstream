"""Watch together through the daemon's control: the group's pages, the players' presses and
the waits they hold for the group to change, with a stand-in ffmpeg."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from vpstream import api, clients, control, gate, player, public, together
from vpstream.stream import Answer, StreamError
from vpstream.tests.test_control_clients import Viewers


class Group(Viewers):
    def start_together(self, folder: bool = False, **options: Any) -> None:
        source = {"file": str(self.film)}
        if folder:
            for name in ("e1.mkv", "e2.mkv"):
                (self.media / name).write_bytes(name.encode())
            self.film.unlink()
            source = {"dir": str(self.media)}
        start = {**source, "alias": "aaa", "mode": "browser", "together": True, **options}
        self.control.start(start)
        assert self.preparer.wait()

    def page(self, who: str, number: int = 1) -> dict[str, Any]:
        answer = self.visit(who, f"aaa/{number}")
        assert answer.kind == "player", answer
        return dict(answer.data)

    def say(self, who: str, pg: str, number: int = 1, **query: Any) -> dict[str, Any]:
        """A report of page `pg` of browser `who`."""
        answer = self.visit(who, f"aaa/{number}/client", html=False, pg=pg, **query)
        assert answer.kind == "client", answer
        return dict(answer.data)

    def join(self, who: str, pg: str, number: int = 1) -> None:
        """Browser `who` is let in, loads its page and is past its cooldown."""
        self.let_in(who)
        self.page(who, number)
        self.say(who, pg, number, t=0, d=600, **{"in": 1})
        self.clock.now += together.COOLDOWN_SECONDS

    def wait(self, who: str, pg: str, rev: int, number: int = 1) -> Answer:
        return self.visit(who, f"aaa/{number}/sync", html=False, pg=pg, rev=rev)

    def room(self) -> together.Room:
        assert self.control.stream is not None and self.control.stream.together is not None
        return self.control.stream.together


@pytest.fixture
def rig(tmp_path: Path) -> Group:
    return Group(tmp_path)


def test_together_lets_in_only_approved_devices(rig: Group) -> None:
    rig.start_together()
    info = rig.control.status()
    assert info["stream"]["policy"] == gate.APPROVAL
    assert info["stream"]["together"]["paused"] is True
    with pytest.raises(StreamError, match="cannot be open"):
        rig.control.set_policy("open")
    rig.control.set_policy("locked")  # stricter still: allowed
    rig.control.stop()
    with pytest.raises(StreamError, match="only the devices you approve"):
        rig.start(together=True)  # the rig's policy is open
    with pytest.raises(StreamError, match="browser mode"):
        rig.control.start({"file": str(rig.film), "together": True})
    with pytest.raises(StreamError, match="true or false"):
        rig.control.start({"file": str(rig.film), "mode": "browser", "together": "yes"})


def test_a_page_starts_where_the_group_is_with_its_tracks(rig: Group) -> None:
    rig.start_together()
    rig.join("tv", "t1")
    rig.say("tv", "t1", act="seek", v="300", id="t1-1")
    rig.say("tv", "t1", act="audio", v="2", id="t1-2")  # in the window: waits
    rig.clock.now += 2
    rig.control.tick_together()
    rig.say("tv", "t1", act="play", id="t1-3")
    rig.clock.now += 30
    rig.let_in("phone")
    rig.say("phone", "p0", t=4000, d=5400)  # where the phone itself left off: not used
    page = rig.page("phone")
    assert page["start"] == pytest.approx(330.0)
    assert page["audio"][page["start_audio"]]["number"] == 2
    group = page["together"]
    assert group["sync"] == "/aaa/1/sync" and "cooldown" not in group
    assert group["tolerance"] == together.SYNC_SECONDS, "out of sync past it, as the daemon says"
    assert group["state"]["paused"] is False and group["state"]["audio"] == 2


def test_a_page_asked_for_another_video_gets_the_groups(rig: Group) -> None:
    rig.start_together(folder=True)
    rig.join("tv", "t1")
    assert rig.visit("tv", "aaa").kind == "player"  # not the list: the group's video
    rig.say("tv", "t1", act="video", v="2", id="t1-1")
    answer = rig.visit("tv", "aaa/1")
    assert (answer.kind, answer.location) == ("redirect", "/aaa/2")
    assert rig.visit("tv", "aaa").data["number"] == 2
    assert rig.page("tv", 2)["together"]["state"]["video"] == 2


def test_a_video_that_is_not_ready_cannot_be_chosen(rig: Group) -> None:
    rig.start_together(folder=True)
    rig.join("tv", "t1")
    assert rig.say("tv", "t1", act="video", v="3", id="t1-1")["outcome"] == "refused"
    assert rig.say("tv", "t1", act="video", v="x", id="t1-2")["outcome"] == "refused"
    assert rig.room().video == 1 and rig.room().window_until is None


def test_a_press_applies_to_the_group_at_once_and_the_answer_says_so(rig: Group) -> None:
    rig.start_together()
    rig.join("tv", "t1")
    rig.join("phone", "p1")
    answer = rig.say("tv", "t1", act="play", id="t1-1", t=0)
    assert answer["outcome"] == "applied" and answer["together"]["paused"] is False
    assert answer["together"]["changed_by"] == rig.device("tv").id
    answer = rig.say("phone", "p1", act="pause", id="p1-1")
    assert answer["outcome"] == "queued" and answer["together"]["paused"] is False
    assert rig.say("tv", "t1")["outcome"] is None  # a report only says where it is
    rig.clock.now += 2
    rig.control.tick_together()
    assert rig.say("tv", "t1")["together"]["paused"] is True


def test_the_daemon_refuses_a_press_during_the_cooldown(rig: Group) -> None:
    rig.start_together()
    rig.let_in("tv")
    rig.page("tv")
    assert rig.say("tv", "t1", act="play", id="t1-1")["outcome"] == "cooldown"  # loading
    answer = rig.say("tv", "t1", t=0, d=600, act="play", id="t1-2", **{"in": 1})
    assert answer["outcome"] == "cooldown" and answer["cooldown"] == together.COOLDOWN_SECONDS
    rig.clock.now += together.COOLDOWN_SECONDS - 1
    assert rig.say("tv", "t1", act="play", id="t1-3")["cooldown"] == 1.0
    assert rig.room().paused
    rig.clock.now += 1
    assert rig.say("tv", "t1", act="play", id="t1-4")["outcome"] == "applied"


def test_per_device_commands_are_refused_but_a_name_is_not(rig: Group) -> None:
    rig.start_together()
    rig.let_in("tv")
    for change in ({"audio": "2"}, {"sub": "off"}, {"seek": 10}, {"start_at": 10}):
        with pytest.raises(StreamError, match="every player does what the group does"):
            rig.ask("tv", **change)
    rig.ask("tv", name="Living room")
    assert rig.device("tv").name == "Living room"


def test_the_subtitle_size_is_each_devices_own(rig: Group) -> None:
    rig.start_together()
    rig.join("tv", "t1")
    rig.join("phone", "p1")
    assert rig.page("tv")["start_sub_size"] == "m"
    info = rig.ask("phone", sub_size="s")
    phone = next(d for d in info["stream"]["devices"] if d["id"] == rig.device("phone").id)
    assert phone["client"]["pending"] == {"sub_size": "s"}
    assert rig.say("tv", "t1")["commands"] == [], "not the other devices"
    answer = rig.say("phone", "p1", z="m")
    assert answer["commands"] == [{"seq": 1, "sub_size": "s"}] and "together" in answer
    assert rig.page("phone")["start_sub_size"] == "s", "a page loaded now starts with it"
    rig.say("phone", "p1", z="s", ack=1)
    assert rig.say("phone", "p1", ack=1)["commands"] == []
    assert rig.page("tv")["start_sub_size"] == "m"
    rig.say("tv", "t1", z="xl")  # the TV's own button
    assert rig.page("tv")["start_sub_size"] == "xl"


def test_the_owner_changes_it_for_everyone(rig: Group) -> None:
    rig.start_together(folder=True)
    info = rig.control.together({"action": "play"})
    assert info["outcome"] == "applied" and info["stream"]["together"]["paused"] is False
    assert info["stream"]["together"]["changed_by"] == together.OWNER
    assert rig.control.together({"action": "seek", "value": "12:30"})["outcome"] == "queued"
    rig.clock.now += 2
    rig.control.tick_together()
    status = rig.control.together({"action": "status"})
    assert status["outcome"] is None
    assert status["stream"]["together"]["position"] == pytest.approx(750.0)
    with pytest.raises(StreamError, match="not a video of this stream"):
        rig.control.together({"action": "video", "value": "3"})
    with pytest.raises(StreamError, match="not a time"):
        rig.control.together({"action": "seek", "value": "soon"})
    with pytest.raises(StreamError, match="needs a value"):
        rig.control.together({"action": "audio"})
    with pytest.raises(StreamError, match="not a together action"):
        rig.control.together({"action": "dance"})


def test_together_needs_a_stream_watched_together(rig: Group) -> None:
    with pytest.raises(StreamError):
        rig.control.together({"action": "status"})
    rig.start_browser()
    with pytest.raises(StreamError, match="--together"):
        rig.control.together({"action": "play"})


def test_a_wait_on_an_old_rev_is_answered_at_once(rig: Group) -> None:
    rig.start_together()
    rig.join("tv", "t1")
    answer = rig.wait("tv", "t1", rev=0)
    assert answer.kind == "client" and answer.data["together"]["rev"] == 1
    assert "waited" not in answer.data


def test_a_wait_is_held_until_the_group_changes(
    rig: Group, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(control, "HOLD_SECONDS", 5.0)
    rig.start_together()
    rig.join("tv", "t1")
    rig.join("phone", "p1")
    got: list[Answer] = []
    held = threading.Thread(target=lambda: got.append(rig.wait("phone", "p1", rev=1)))
    held.start()
    time.sleep(0.3)
    assert not got, "held while nothing changes"
    rig.say("tv", "t1", act="play", id="t1-1")
    held.join(2)
    assert got and got[0].data["together"]["rev"] == 2
    assert got[0].data["together"]["paused"] is False
    assert 0.2 < got[0].data["waited"] < 2


def test_a_closing_window_wakes_the_waits(rig: Group, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(control, "HOLD_SECONDS", 5.0)
    rig.start_together()
    rig.join("tv", "t1")
    rig.say("tv", "t1", act="play", id="t1-1")
    rig.say("tv", "t1", act="pause", id="t1-2")
    got: list[Answer] = []
    held = threading.Thread(target=lambda: got.append(rig.wait("tv", "t1", rev=2)))
    held.start()
    time.sleep(0.2)
    rig.clock.now += 2
    rig.control.tick_together()
    held.join(2)
    assert got and got[0].data["together"]["paused"] is True


def test_a_wait_that_nothing_changes_ends_after_a_while(
    rig: Group, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(control, "HOLD_SECONDS", 0.3)
    rig.start_together()
    rig.join("tv", "t1")
    answer = rig.wait("tv", "t1", rev=1)
    assert answer.data["together"]["rev"] == 1 and answer.data["waited"] >= 0.3


def test_waits_are_bounded_per_page_per_device_and_in_all(
    rig: Group, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(control, "HOLD_SECONDS", 5.0)
    monkeypatch.setattr(control, "HOLDS_PER_DEVICE", 2)
    rig.start_together()
    rig.join("tv", "t1")
    got: dict[str, Answer] = {}

    def hold(pg: str) -> threading.Thread:
        thread = threading.Thread(target=lambda: got.update({pg: rig.wait("tv", pg, rev=1)}))
        thread.start()
        time.sleep(0.15)
        return thread

    first = hold("t1")
    second = hold("t2")
    began = time.monotonic()
    third = rig.wait("tv", "t3", rev=1)  # past the bound: answered at once
    # Times well short of a held wait's (5 s), also on a loaded machine.
    assert time.monotonic() - began < 2 and third.data["waited"] < 1
    again = hold("t1")  # the same page again: the older wait ends
    first.join(2)
    assert "t1" in got and got["t1"].data["waited"] < 3
    del got["t1"]
    rig.control.together({"action": "play"})
    again.join(2)
    second.join(2)  # woken too: each thread answers once it has the lock
    assert got["t1"].data["together"]["rev"] == 2 and got["t2"].data["together"]["rev"] == 2
    assert not rig.control._holds


def test_a_device_denied_while_it_waits_gets_no_answer(
    rig: Group, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(control, "HOLD_SECONDS", 5.0)
    rig.start_together()
    rig.join("tv", "t1")
    got: list[Answer] = []
    held = threading.Thread(target=lambda: got.append(rig.wait("tv", "t1", rev=1)))
    held.start()
    time.sleep(0.2)
    rig.control.set_device(rig.device("tv").id, False)
    rig.control.together({"action": "play"})
    held.join(2)
    assert got and got[0].kind == "denied"


def test_the_group_is_kept_across_a_restart_paused(rig: Group) -> None:
    rig.start_together()
    rig.join("tv", "t1")
    rig.say("tv", "t1", act="play", id="t1-1")
    rig.clock.now += 42
    rig.control._save()
    rig.again().reconcile()
    room = rig.room()
    assert room.paused and room.live(rig.clock.now + 100) == pytest.approx(42.0)
    assert room.cooldown("t1", rig.clock.now) == 0.0  # the page is in the group still


def test_a_group_that_plays_is_written_down_now_and_then(rig: Group) -> None:
    rig.start_together()
    rig.control.together({"action": "play"})
    rig.clock.now += 30
    for _ in range(3):  # the daemon's ticks, and its tick that writes what is due
        rig.control.tick_together()
        rig.clock.now += control.SAVE_SECONDS / 2
        rig.control.tick()
    recorded = rig.recorded_stream()
    # Written at the last tick (45 s in), not only once after the press (35 s in).
    assert recorded["together"]["position"] == pytest.approx(30 + 1.5 * control.SAVE_SECONDS)


def test_the_dashboard_sees_each_pages_drift_and_cooldown(rig: Group) -> None:
    rig.start_together()
    rig.join("tv", "t1")
    rig.say("tv", "t1", act="play", id="t1-1", t=0)
    rig.clock.now += 5
    rig.say("tv", "t1", t=4.2)
    rig.let_in("phone")
    rig.page("phone")
    rig.say("phone", "p1", t=5.0, d=600, **{"in": 1})
    pages = {p["page"]: p for p in rig.control.status()["stream"]["together"]["pages"]}
    assert pages["t1"]["drift"] == pytest.approx(-0.8) and pages["t1"]["cooldown"] == 0.0
    assert pages["p1"]["cooldown"] == together.COOLDOWN_SECONDS
    assert pages["p1"]["device"] == rig.device("phone").id


def test_the_drift_shown_is_the_one_the_page_measured(rig: Group) -> None:
    """Over the internet a report is on its way for a while: a page in step with the group
    said where it was when the report left, and the group has moved on since. The page's
    own measure (o=) is shown; a report without one, or one that does not read, falls back
    to its position."""
    rig.start_together()
    rig.join("tv", "t1")
    rig.say("tv", "t1", act="play", id="t1-1", t=0)
    rig.clock.now += 5
    # It left 0.25 s ago, at 4.8 s, as the page knew the group to be: in step.
    rig.clock.now += 0.25
    rig.say("tv", "t1", t=4.8, o="0.03")
    pages = {p["page"]: p for p in rig.control.status()["stream"]["together"]["pages"]}
    assert pages["t1"]["drift"] == pytest.approx(0.03)
    for bad in ("x", "nan", "1e9", "9" * 30):
        rig.say("tv", "t1", t=4.8, o=bad)
        pages = {p["page"]: p for p in rig.control.status()["stream"]["together"]["pages"]}
        assert pages["t1"]["drift"] == pytest.approx(-0.45), bad
    rig.say("tv", "t1", t=4.8, o="-1.5")
    pages = {p["page"]: p for p in rig.control.status()["stream"]["together"]["pages"]}
    assert pages["t1"]["drift"] == pytest.approx(-1.5), "behind is below 0"
    assert pages["t1"]["out_of_sync"] is False


def test_a_page_alone_takes_the_group_where_it_is(rig: Group) -> None:
    """Watching alone, a video that waited for data is not out of sync with nobody: the
    group goes where the page is. With another page watching, it stays."""
    rig.start_together()
    rig.join("tv", "t1")
    answer = rig.say("tv", "t1", act="play", id="t1-1", t=0)
    assert answer["alone"] is True
    rev = rig.room().rev
    rig.clock.now += 10
    answer = rig.say("tv", "t1", t=6, o="-4.00", rev=rev)
    assert answer["together"]["position"] == pytest.approx(6.0) and answer["together"]["rev"] == rev
    rig.join("phone", "p1")  # joins where the tv is
    assert rig.say("phone", "p1", t=16, o="0", rev=rev)["alone"] is False
    answer = rig.say("tv", "t1", t=12, o="-4.00", rev=rev)
    assert answer["alone"] is False and answer["together"]["position"] == pytest.approx(16.0)


def test_a_page_further_off_than_the_tolerance_is_shown_out_of_sync(rig: Group) -> None:
    rig.start_together()
    rig.join("tv", "t1")
    rig.say("tv", "t1", act="play", id="t1-1", t=0)
    rig.clock.now += 5
    shown = {}
    for o in ("2.0", "-2.0", "2.01", "-6.93"):
        rig.say("tv", "t1", t=5, o=o)
        (page,) = rig.control.status()["stream"]["together"]["pages"]
        shown[o] = page["out_of_sync"]
    assert shown == {"2.0": False, "-2.0": False, "2.01": True, "-6.93": True}


def test_the_api_and_the_public_app_carry_it(rig: Group) -> None:
    rig.start_together()
    client = api.create_app(lambda: rig.control.status(), rig.control).test_client()
    answer = client.post("/api/v1/together", json={"action": "play"})
    assert answer.status_code == 200 and answer.get_json()["outcome"] == "applied"
    bad = client.post("/api/v1/together", json={"action": "seek", "value": "x"})
    assert bad.status_code == 400
    from vpstream import caddy, public

    app = public.create_app(rig.control).test_client()
    rig.let_in("tv")
    cookie = rig.cookies["tv"]
    app.set_cookie(gate.cookie_name(False), cookie)
    headers = {caddy.HEADER_IP: "192.0.2.10", player.REPORT_HEADER: "1"}
    got = app.get("/aaa/1/sync?pg=t1&rev=0", headers=headers)
    assert got.status_code == 200 and got.get_json()["together"]["rev"] == 2
    assert got.headers["Cache-Control"] == "no-store"
    plain = app.get("/aaa/1/sync?pg=t1&rev=0", headers={caddy.HEADER_IP: "192.0.2.10"})
    assert plain.status_code == 404  # not the player's: not a wait


def test_the_owner_changes_the_video_for_everyone(rig: Group) -> None:
    rig.start_together(folder=True)
    rig.join("tv", "t1")
    info = rig.control.together({"action": "video", "value": "2"})
    assert info["outcome"] == "applied" and info["stream"]["together"]["video"] == 2
    assert rig.visit("tv", "aaa/1").location == "/aaa/2"


def test_a_page_that_comes_back_after_a_while_waits_again(rig: Group) -> None:
    rig.start_together()
    rig.join("tv", "t1")
    rig.clock.now += together.PAGE_SECONDS + 1  # the tab was put away
    answer = rig.say("tv", "t1", t=0, d=600, act="play", id="t1-1", **{"in": 1})
    assert answer["outcome"] == "cooldown" and answer["cooldown"] == together.COOLDOWN_SECONDS
    assert rig.room().paused


def test_a_window_closed_by_the_dashboards_look_wakes_the_waits(
    rig: Group, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(control, "HOLD_SECONDS", 5.0)
    rig.start_together()
    rig.join("tv", "t1")
    rig.say("tv", "t1", act="play", id="t1-1")
    rig.say("tv", "t1", act="pause", id="t1-2")
    got: list[Answer] = []
    held = threading.Thread(target=lambda: got.append(rig.wait("tv", "t1", rev=2)))
    held.start()
    time.sleep(0.2)
    rig.clock.now += 2
    rig.control.status()  # closes the window, before the daemon's own tick does
    held.join(0.5)
    assert got and got[0].data["together"]["paused"] is True


def test_on_the_public_dashboard_only_the_logged_in_owner_reaches_the_group(rig: Group) -> None:
    """Linux's public dashboard: the group's card and its changes work through the login and
    its token; a viewer, with the stream's cookie or through the stream's port, reaches none
    of it."""
    from vpstream import caddy, login, public
    from vpstream.tests.test_dashboard_login import IP, get, log_in, post
    from vpstream.tests.test_login import verifier

    rig.start_together()
    rig.let_in("tv")
    auth = login.Login(verifier(), login.Limiter(None, log=lambda m: None))
    web = api.create_app(rig.control.status, rig.control, auth).test_client()
    play = {"action": "play"}
    # A viewer's browser sends its stream cookie to the dashboard too: it counts for nothing.
    web.set_cookie(gate.cookie_name(False), rig.cookies["tv"], domain="203.0.113.5")
    assert post(web, "/api/v1/together", play).status_code == 401
    assert get(web, "/api/v1/status").status_code == 401
    viewer = public.create_app(rig.control).test_client()
    viewer.set_cookie(gate.cookie_name(False), rig.cookies["tv"])
    through = {caddy.HEADER_IP: IP["X-Vpstream-Ip"], player.REPORT_HEADER: "1"}
    for path in ("/api/v1/together", "/api/v1/status"):
        assert viewer.post(path, json=play, headers=through).status_code == 405  # GET only
        assert viewer.get(path, headers=through).status_code == 404
    assert rig.room().paused, "nothing a viewer sent reached the group"

    _, csrf = log_in(web)
    assert post(web, "/api/v1/together", play).status_code == 403, "not without the token"
    done = post(web, "/api/v1/together", play, headers={login.CSRF_HEADER: csrf})
    assert done.status_code == 200 and done.get_json()["outcome"] == "applied"
    shown = get(web, "/api/v1/status").get_json()["stream"]["together"]
    assert shown["paused"] is False and shown["changed_by"] == together.OWNER


def test_a_switch_of_the_look_brings_the_pages_back_with_no_cooldown(rig: Group) -> None:
    rig.start_together()
    rig.join("tv", "abc")
    rig.say("tv", "abc", t=0, d=600, **{"in": 1})
    rig.control.set_ui("old")
    assert rig.say("tv", "abc", t=0, d=600)["ui"] == "old"  # the page loads again
    rig.clock.now += 2
    assert rig.page("tv")["ui"] == "old"
    rig.say("tv", "new1", t=0, d=600, **{"in": 1})
    assert rig.room().cooldown("new1", rig.clock.now) == 0.0
    rig.control.set_ui("old")  # the look it has: nothing to load again
    assert rig.room().reloaded < rig.clock.now


def test_a_viewers_whole_name_comes_through_http(rig: Group) -> None:
    rig.start_together()
    web = public.create_app(rig.control).test_client()
    ip = {"X-Vpstream-Ip": "192.0.2.9", "User-Agent": "phone"}
    page = web.get("/aaa", headers={**ip, "Accept": "text/html"})
    cookie = page.headers["Set-Cookie"].split(";")[0]
    assert rig.control.stream is not None
    device = rig.control.stream.devices.browser(cookie.split("=", 1)[1])
    assert device is not None
    rig.control.set_device(device.id, True)
    name = "Bartholomew Montgomery-Fitzgerald X"  # 35 characters: more than any other value
    assert 32 < len(name) <= clients.MAX_NAME
    answer = web.get(
        "/aaa/1/client",
        query_string={"pg": "abc", "t": "0", "n": name},
        headers={**ip, player.REPORT_HEADER: "1", "Cookie": cookie},
    )
    assert answer.status_code == 200
    assert rig.room().pages["abc"].name == name


# --- the call


def post(rig: Group, who: str, pg: str, number: int = 1, **body: Any) -> Answer:
    """What page `pg` of browser `who` posts to its call."""
    return rig.control.visit(
        f"aaa/{number}/call", TV_IP, who, False, rig.cookies.get(who), None, {"pg": pg, **body}
    )


TV_IP = "192.0.2.10"


def calling(rig: Group) -> Group:
    """Two browsers in the group over HTTPS: tv (page t1) and phone (page p1)."""
    rig.start_together()
    assert rig.control.stream is not None
    rig.control.stream.https = True
    rig.join("tv", "t1")
    rig.join("phone", "p1")
    return rig


def test_a_call_is_only_for_a_stream_over_https(rig: Group) -> None:
    rig.start_together()
    rig.join("tv", "t1")
    assert rig.page("tv")["together"]["call"] is None
    assert "call" not in rig.say("tv", "t1")
    assert post(rig, "tv", "t1", do="start").kind == "missing"
    assert rig.control.stream is not None
    rig.control.stream.https = True
    assert rig.page("tv")["together"]["call"] == "/aaa/1/call"
    assert rig.say("tv", "t1")["call"]["id"] == ""


def test_a_call_rings_the_others_and_passes_their_messages(rig: Group) -> None:
    calling(rig)
    started = post(rig, "tv", "t1", do="start")
    assert started.kind == "client" and started.data["outcome"] == together.STARTED
    call = started.data["call"]
    assert [m["page"] for m in call["members"]] == ["t1"] and not call["ring"]
    rev, version = rig.room().rev, rig.room().call.version
    # The phone's wait answers at once: it has not seen this call.
    rung = rig.visit("phone", "aaa/1/sync", html=False, pg="p1", rev=rev, cv=version - 1)
    assert rung.kind == "client" and rung.data["call"]["ring"] is True
    assert post(rig, "phone", "p1", do="start").data["outcome"] == together.JOINED
    sent = post(rig, "tv", "t1", do="send", to="p1", kind="offer", data="v=0 sdp")
    assert sent.data["outcome"] == together.SENT
    version = rig.room().call.version
    got = rig.visit("phone", "aaa/1/sync", html=False, pg="p1", rev=rev, cv=version, cm=0)
    (mail,) = got.data["call"]["mail"]
    assert (mail["from"], mail["kind"], mail["data"]) == ("t1", "offer", "v=0 sdp")
    # Taken: the next wait is held, as any.
    held = rig.room().call.news("p1", version, mail["seq"])
    assert held is False
    assert post(rig, "phone", "p1", do="state", mic=True, sees=["t1"]).data["outcome"] == "applied"
    assert rig.say("tv", "t1")["call"]["members"][1] == {
        "page": "p1", "name": "", "mic": True, "cam": True, "sees": ["t1"]
    }  # fmt: skip
    assert post(rig, "phone", "p1", do="end").data["outcome"] == together.ENDED
    assert rig.say("tv", "t1")["call"]["id"] == "", "two in the call: it ended for both"
    assert rig.control.status()["stream"]["together"]["call"] == {"id": "", "members": []}


def test_a_held_wait_ends_when_a_message_comes_for_its_page(
    rig: Group, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(control, "HOLD_SECONDS", 5.0)
    calling(rig)
    post(rig, "tv", "t1", do="start")
    post(rig, "phone", "p1", do="start")
    room = rig.room()
    got: dict[str, Answer] = {}
    query = {"pg": "p1", "rev": room.rev, "cv": room.call.version, "cm": room.call.seq}
    thread = threading.Thread(
        target=lambda: got.update(p1=rig.visit("phone", "aaa/1/sync", html=False, **query))
    )
    thread.start()
    time.sleep(0.2)
    assert "p1" not in got, "nothing new: held"
    began = time.monotonic()
    post(rig, "tv", "t1", do="send", to="p1", kind="answer", data="x")
    thread.join(3)
    assert time.monotonic() - began < 2
    assert [m["data"] for m in got["p1"].data["call"]["mail"]] == ["x"]


def test_only_pages_of_the_group_call_and_not_too_often(rig: Group) -> None:
    calling(rig)
    assert post(rig, "tv", "stranger", do="start").kind == "missing"
    assert post(rig, "tv", "Bad!", do="start").kind == "missing"
    rig.visit("other")  # a browser that waits for the owner
    assert post(rig, "other", "o1", do="start").kind == "denied"
    assert post(rig, "tv", "t1", do="dance").data["outcome"] == together.REFUSED
    assert rig.control.visit(
        "aaa/1/client", TV_IP, "tv", False, rig.cookies["tv"], None, {}
    ).kind == ("missing"), "only the call is posted to"
    answers = [post(rig, "tv", "t1", do="state").kind for _ in range(together.POSTS + 1)]
    assert answers[0] == "client" and answers[-1] == "busy"


def test_the_call_goes_through_the_public_app(rig: Group) -> None:
    from vpstream import caddy

    calling(rig)
    app = public.create_app(rig.control).test_client()
    app.set_cookie(gate.cookie_name(True), rig.cookies["tv"])
    headers = {caddy.HEADER_IP: TV_IP, caddy.HEADER_SCHEME: "https", player.REPORT_HEADER: "1"}
    got = app.post("/aaa/1/call", json={"pg": "t1", "do": "start"}, headers=headers)
    assert got.status_code == 200 and got.get_json()["outcome"] == together.STARTED
    assert got.headers["Cache-Control"] == "no-store"
    plain = {k: v for k, v in headers.items() if k != player.REPORT_HEADER}
    assert app.post("/aaa/1/call", json={"pg": "t1"}, headers=plain).status_code == 400
    form = app.post("/aaa/1/call", data="pg=t1", headers=headers)
    assert form.status_code == 400, "only JSON: a page of another site must ask first"
    big = {"pg": "t1", "do": "send", "data": "x" * caddy.CALL_BYTES}
    assert app.post("/aaa/1/call", json=big, headers=headers).status_code == 400
    deep = "[" * 5000 + "]" * 5000
    sent = app.post("/aaa/1/call", data=deep, headers=headers, content_type="application/json")
    assert sent.status_code == 400
    assert app.get("/aaa/1/call", headers=headers).status_code == 404
    page = app.get("/aaa/1", headers={**headers, "Accept": "text/html"})
    assert page.status_code == 200
    assert page.headers["Permissions-Policy"] == "camera=(self), microphone=(self)"
