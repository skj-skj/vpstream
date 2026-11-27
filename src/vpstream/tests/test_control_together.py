"""Watch together through the daemon's control: the group's pages, the players' presses and
the waits they hold for the group to change, with a stand-in ffmpeg."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from vpstream import api, control, gate, player, together
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
