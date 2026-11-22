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
    assert time.monotonic() - began < 0.5 and third.data["waited"] < 0.1
    again = hold("t1")  # the same page again: the older wait ends
    first.join(2)
    assert "t1" in got and got["t1"].data["waited"] < 1
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
