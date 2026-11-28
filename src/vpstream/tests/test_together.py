"""The group of watch together: its windows, cooldowns and what it keeps across a restart."""

from __future__ import annotations

from typing import Any

import pytest

from vpstream import together
from vpstream.together import (
    APPLIED,
    COOLDOWN,
    COOLDOWN_SECONDS,
    QUEUED,
    REFUSED,
    WINDOW_SECONDS,
    Room,
    TogetherError,
)

T0 = 1000.0


def joined(room: Room, *pages: str, now: float = T0, duration: float = 600.0) -> Room:
    """The pages are in, loaded at the group's place and past their cooldown."""
    for page in pages:
        room.report(page, f"b-{page}", now - COOLDOWN_SECONDS, room.video, 0.0, duration, True)
    return room


def press(
    room: Room, page: str, kind: str, value: Any = None, now: float = T0, act: str = ""
) -> str:
    if kind in ("play", "pause"):
        kind, value = together.PLAY, kind == "play"
    return room.propose(kind, value, page, f"b-{page}", act, now)


def test_the_group_starts_paused_at_the_beginning() -> None:
    state = Room(videos=3).state(T0)
    assert (state["video"], state["position"], state["paused"], state["rev"]) == (1, 0.0, True, 1)
    assert (state["audio"], state["sub"], state["window"]) == (None, None, 0.0)


def test_the_first_press_applies_at_once_and_opens_a_window() -> None:
    room = joined(Room(), "a", "b")
    assert press(room, "a", "play") == APPLIED
    state = room.state(T0)
    assert state["paused"] is False and state["rev"] == 2
    assert (state["changed_by"], state["window"]) == ("b-a", WINDOW_SECONDS)
    assert room.state(T0 + 5)["position"] == 5.0  # it plays on with the clock


def test_the_last_press_of_a_kind_in_the_window_wins_when_it_closes() -> None:
    room = joined(Room(), "a", "b", "c")
    press(room, "a", "play")
    assert press(room, "b", "pause", now=T0 + 0.5) == QUEUED
    assert press(room, "c", "play", now=T0 + 1.0) == QUEUED
    assert press(room, "b", "seek", 100, now=T0 + 1.5) == QUEUED
    assert room.state(T0 + 1.9)["paused"] is False and room.rev == 2  # nothing yet
    assert not room.tick(T0 + 1.99)
    assert room.tick(T0 + WINDOW_SECONDS)
    state = room.state(T0 + WINDOW_SECONDS)
    # Play was last of its kind, and the group plays already: only the seek changes it.
    assert (state["paused"], state["position"], state["rev"]) == (False, 100.0, 3)
    assert state["changed_by"] == "b-b" and state["window"] == 0.0


def test_different_kinds_in_one_window_all_apply() -> None:
    room = joined(Room(), "a", "b")
    press(room, "a", "play")
    press(room, "b", "pause", now=T0 + 1)
    press(room, "a", "seek", 300, now=T0 + 1.2)
    press(room, "b", "audio", "2", now=T0 + 1.4)
    press(room, "b", "sub", "1-soft", now=T0 + 1.6)
    room.tick(T0 + 2)
    state = room.state(T0 + 10)
    assert (state["paused"], state["position"], state["audio"], state["sub"]) == (
        True,
        300.0,
        2,
        "1-soft",
    )
    assert state["rev"] == 3  # one change for the whole window


def test_a_window_that_changes_nothing_bumps_no_rev() -> None:
    room = joined(Room(), "a", "b")
    press(room, "a", "play")
    press(room, "b", "pause", now=T0 + 1)
    press(room, "b", "play", now=T0 + 1.5)  # back to what it is: the window changes nothing
    assert not room.tick(T0 + 2) and room.rev == 2
    assert press(room, "b", "pause", now=T0 + 3) == APPLIED  # no window open: at once
    assert room.rev == 3


def test_a_press_that_changes_nothing_opens_no_window() -> None:
    room = joined(Room(), "a", "b")
    assert press(room, "a", "pause") == APPLIED  # paused already
    assert room.rev == 1 and room.window_until is None
    room.propose(together.SEEK, 0.2, "a", "b-a", "", T0)  # where it is, near enough
    room.propose(together.AUDIO, 0, "", together.OWNER, "", T0)
    room.propose(together.AUDIO, 0, "", together.OWNER, "", T0)  # the same audio again
    assert room.window_until == T0 + WINDOW_SECONDS and room.rev == 2
    assert press(room, "b", "play", now=T0 + 3) == APPLIED and room.paused is False


def test_steps_stack_on_the_pressers_own_pending_seek() -> None:
    room = joined(Room(), "a", "b")
    room.propose(together.SEEK, 600, "", together.OWNER, "", T0 - 50)
    room.tick(T0)
    assert press(room, "a", together.SKIP, -10, now=T0) == APPLIED  # 600 -> 590 at once
    assert room.live(T0) == 590.0
    press(room, "a", together.SKIP, -10, now=T0 + 0.3)  # waits: 580
    press(room, "a", together.SKIP, -10, now=T0 + 0.6)  # on its own pending seek: 570
    room.tick(T0 + 2)
    assert room.live(T0 + 2) == 570.0  # three quick taps moved 30 s


def test_a_step_does_not_build_on_another_viewers_seek() -> None:
    room = joined(Room(), "a", "b")
    press(room, "a", "seek", 100)
    press(room, "b", "seek", 400, now=T0 + 0.5)
    press(room, "a", together.SKIP, 10, now=T0 + 1)  # from where the group is: 110
    room.tick(T0 + 2)
    assert room.live(T0 + 2) == 110.0


def test_a_retried_action_is_done_once() -> None:
    room = joined(Room(), "a")
    assert press(room, "a", together.SKIP, 10, act="a-1") == APPLIED
    room.tick(T0 + 2)
    assert press(room, "a", together.SKIP, 10, now=T0 + 3, act="a-1") == APPLIED  # as then
    assert room.live(T0 + 3) == 10.0 and room.window_until is None
    assert press(room, "a", together.SKIP, 10, now=T0 + 3, act="a-2") == APPLIED
    assert room.live(T0 + 3) == 20.0


def test_a_page_that_just_joined_is_in_cooldown() -> None:
    room = Room()
    assert room.cooldown("new", T0) == COOLDOWN_SECONDS
    assert press(room, "new", "play") == COOLDOWN  # not even known yet
    room.report("new", "b-new", T0, 1, 0.0, 600.0)  # loading: not at the group's place yet
    assert press(room, "new", "play", now=T0 + 30) == COOLDOWN  # its cooldown has not started
    room.report("new", "b-new", T0 + 30, 1, 0.0, 600.0, ready=True)
    assert room.cooldown("new", T0 + 33) == pytest.approx(COOLDOWN_SECONDS - 3)
    assert press(room, "new", "play", now=T0 + 39.9) == COOLDOWN
    assert room.rev == 1 and room.window_until is None  # nothing was done, no window
    assert press(room, "new", "play", now=T0 + 40) == APPLIED


def test_the_owner_never_waits_for_a_cooldown() -> None:
    room = Room()
    assert room.propose(together.PLAY, True, "", together.OWNER, "", T0) == APPLIED
    assert room.changed_by == together.OWNER


def test_a_joiner_starts_where_the_group_is_now() -> None:
    room = joined(Room(), "a", duration=5400.0)
    press(room, "a", "seek", 1200 - 1, now=T0)
    room.tick(T0 + 2)
    press(room, "a", "play", now=T0 + 3)
    late = room.state(T0 + 63)
    assert late["position"] == pytest.approx(1199 + 60)
    page = room.report("late", "b-late", T0 + 63, 1, 1259.4, 5400.0, ready=True)
    assert page.drift == pytest.approx(0.4)


def test_a_page_that_follows_the_group_to_another_video_does_not_wait_again() -> None:
    room = joined(Room(videos=3), "a", "b")
    room.report("a", "b-a", T0, 1, 0.0, 600.0)
    room.report("b", "b-b", T0, 1, 0.0, 600.0)
    assert press(room, "a", together.VIDEO, 2, now=T0 + 1) == APPLIED
    assert (room.video, room.position, room.paused) == (2, 0.0, True)
    # Each browser loads the new video in a new page.
    room.report("a2", "b-a", T0 + 4, 2, 0.0, 700.0, ready=True)
    assert room.cooldown("a2", T0 + 4) == 0.0
    # A browser that was not watching is a joiner.
    room.report("x", "b-x", T0 + 4, 2, 0.0, 700.0, ready=True)
    assert room.cooldown("x", T0 + 4) == COOLDOWN_SECONDS
    # Neither is one that comes long after the change.
    room.report("b2", "b-b", T0 + 60, 2, 0.0, 700.0, ready=True)
    assert room.cooldown("b2", T0 + 60) == COOLDOWN_SECONDS


def test_a_page_on_another_video_cannot_press() -> None:
    room = joined(Room(videos=2), "a", "b")
    press(room, "a", together.VIDEO, 2)
    assert press(room, "b", "play", now=T0 + 3) == REFUSED  # still on video 1


def test_a_seek_for_a_video_the_window_left_is_dropped() -> None:
    room = joined(Room(videos=2), "a", "b")
    press(room, "a", "play")
    press(room, "b", "seek", 300, now=T0 + 0.5)
    room.pending[together.VIDEO] = together.Proposal(2, "a", "b-a", 1)
    room.tick(T0 + 2)
    assert (room.video, room.live(T0 + 2)) == (2, 0.0)


def test_the_group_pauses_at_the_end_of_its_video() -> None:
    room = joined(Room(), "a", duration=100.0)
    press(room, "a", "seek", 90)
    room.tick(T0 + 2)
    press(room, "a", "play", now=T0 + 3)
    assert not room.tick(T0 + 12)
    assert room.tick(T0 + 13)  # 90 + 10 s
    state = room.state(T0 + 20)
    assert (state["paused"], state["position"], state["changed_by"]) == (True, 100.0, "")
    # Play there starts it again from the beginning.
    press(room, "a", "play", now=T0 + 30)
    assert room.live(T0 + 31) == 1.0


def test_a_seek_past_the_end_stops_at_it() -> None:
    room = joined(Room(), "a", duration=100.0)
    press(room, "a", "seek", 5000)
    assert room.live(T0) == 100.0


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        (together.SEEK, "ten"),
        (together.SEEK, -1),
        (together.SKIP, "x"),
        (together.SKIP, True),
        (together.AUDIO, "eng"),
        (together.SUB, "2-hard"),
        (together.VIDEO, 4),
        (together.VIDEO, "0"),
        (together.PLAY, "yes"),
        ("dance", 1),
    ],
)
def test_values_that_do_not_read_are_refused(kind: str, value: Any) -> None:
    room = Room(videos=3)
    with pytest.raises(TogetherError):
        room.propose(kind, value, "", together.OWNER, "", T0)
    assert room.rev == 1 and room.window_until is None


def test_a_restart_brings_the_group_back_paused_where_it_was() -> None:
    room = joined(Room(videos=3), "a")
    press(room, "a", together.VIDEO, 2)
    room.tick(T0 + 2)
    room.report("a2", "b-a", T0 + 3, 2, 0.0, 700.0, ready=True)
    room.propose(together.AUDIO, 2, "", together.OWNER, "", T0 + 4)
    room.propose(together.PLAY, True, "", together.OWNER, "", T0 + 10)  # in its own window
    room.tick(T0 + 12)
    data = room.to_dict(T0 + 70)
    again = Room.from_dict(data, videos=3)
    assert (again.video, again.paused, again.audio) == (2, True, 2)
    assert again.live(T0 + 500) == pytest.approx(60.0)  # paused at where it was saved
    assert again.rev == room.rev + 1  # new to every page that comes back
    assert again.durations == {1: 600.0, 2: 700.0}
    # A page that was in keeps its place in the group.
    assert again.cooldown("a2", T0 + 80) == 0.0
    assert again.window_until is None and not again.pending


@pytest.mark.parametrize(
    "data",
    [
        None,
        "x",
        {"video": 9, "position": -5, "rev": "x", "audio": "eng", "sub": 3, "pages": {"a": 1}},
        {"video": True, "position": float("nan"), "durations": {"x": 1, "1": -2}, "pages": []},
    ],
)
def test_a_damaged_record_gives_a_fresh_paused_group(data: Any) -> None:
    room = Room.from_dict(data, videos=3)
    assert (room.video, room.position, room.paused, room.rev) == (1, 0.0, True, 1)
    assert room.pages == {} and room.durations == {} and room.audio is None


def test_pages_that_went_quiet_are_forgotten() -> None:
    room = Room()
    room.report("old", "b-1", T0, 1, 0.0, 600.0, ready=True)
    room.report("new", "b-2", T0 + together.PAGE_SECONDS + 1, 1, 0.0, 600.0)
    assert set(room.pages) == {"new"}
    assert [p["page"] for p in room.describe(T0 + together.PAGE_SECONDS + 1)["pages"]] == ["new"]


def test_a_page_that_was_quiet_long_joins_again_with_a_cooldown() -> None:
    room = joined(Room(), "a", "b")
    assert room.cooldown("a", T0) == 0
    later = T0 + together.PAGE_SECONDS + 1  # its tab was put away; nothing new joined since
    room.report("a", "b-a", later, 1, 0.0, 600.0, False)
    assert room.cooldown("a", later) == COOLDOWN_SECONDS
    assert press(room, "a", "play", now=later) == COOLDOWN
    room.report("a", "b-a", later + 1, 1, 0.0, 600.0, True)  # at the group's place again
    assert press(room, "a", "play", now=later + 1 + COOLDOWN_SECONDS) == APPLIED


def test_a_page_that_reports_often_keeps_its_place() -> None:
    room = joined(Room(), "a")
    for n in range(1, 40):
        room.report("a", "b-a", T0 + 2 * n, 1, 0.0, 600.0, True)
    assert room.cooldown("a", T0 + 80) == 0


def playing(*pages: str) -> Room:
    """A group that plays from 100 s at T0, with the pages joined and quiet since."""
    room = joined(Room(), *pages)
    room._set(T0, 100.0, paused=False)
    return room


def test_a_page_alone_takes_the_group_where_it_is() -> None:
    room = playing("a")
    assert room.alone("a", T0)
    # Its video waited 5 s for data: alone, it is not out of sync with nobody.
    room.report("a", "b-a", T0 + 10, 1, 105.0, 600.0, True, offset=-5.0, rev=room.rev)
    assert room.live(T0 + 10) == pytest.approx(105.0) and room.pages["a"].drift == 0.0
    rev = room.rev
    room.report("a", "b-a", T0 + 12, 1, 107.0, 600.0, True, offset=0.2, rev=room.rev)
    assert room.live(T0 + 12) == pytest.approx(107.0), "a little off is left to it"
    assert room.rev == rev, "no change for the pages to follow: the answers say where it is"


@pytest.mark.parametrize(
    "case", ["another watches", "an older rev", "not joined", "paused", "a window is open"]
)
def test_only_a_page_alone_in_the_groups_place_takes_it_along(case: str) -> None:
    room = playing("a", "b")
    # b reported a moment ago, or long enough ago that it has gone.
    room.pages["b"].seen = T0 + 9 if case == "another watches" else T0 - 1
    rev = room.rev - 1 if case == "an older rev" else room.rev
    if case == "not joined":
        room.pages["a"].joined = None
    if case == "paused":
        room._set(T0, 100.0, paused=True)
    if case == "a window is open":
        room.window_until = T0 + 11
    room.report("a", "b-a", T0 + 10, 1, 105.0, 600.0, True, offset=-5.0, rev=rev)
    expected = 100.0 if case == "paused" else 110.0
    assert room.live(T0 + 10) == pytest.approx(expected)
    assert room.pages["a"].drift == pytest.approx(-5.0)


def test_the_longest_duration_any_page_reports_is_the_videos() -> None:
    room = joined(Room(), "a", "b", duration=600.0)
    room.report("b", "b-b", T0, 1, 0.0, 599.4, True)  # a version that ends a little sooner
    room.report("b", "b-b", T0, 1, 0.0, 0.5, True)  # or a page that says something odd
    assert room.duration == 600.0
    press(room, "a", "play")
    assert not room.tick(T0 + 1) and room.paused is False


@pytest.mark.parametrize(
    ("action", "value", "pressed"),
    [
        ("play", None, (together.PLAY, True)),
        ("pause", "x", (together.PLAY, False)),
        ("seek", "12:30", (together.SEEK, "12:30")),
        ("video", 2, (together.VIDEO, 2)),
    ],
)
def test_an_action_is_a_kind_of_press(action: str, value: Any, pressed: Any) -> None:
    assert together.as_press(action, value) == pressed


def test_an_action_that_is_not_one_does_not_read() -> None:
    with pytest.raises(TogetherError, match="not an action: 'jump'"):
        together.as_press("jump", 1)


def test_a_restart_drops_pages_whose_ids_do_not_read() -> None:
    data = joined(Room(), "a").to_dict(T0)
    data["pages"]["Bad id!"] = data["pages"]["a"]
    assert set(Room.from_dict(data, 1).pages) == {"a"}


def test_each_change_says_what_it_was_and_whose_press_it_was() -> None:
    room = joined(Room(), "a", "b")
    room.report("a", "b-a", T0, room.video, 0.0, 600.0, True, name="Asha")
    assert press(room, "a", "play") == APPLIED
    state = room.state(T0)
    assert state["changes"] == [{"what": "play", "by": "b-a", "page": "a", "name": "Asha"}]
    # In one window, each kind with its own presser: b has no name.
    press(room, "b", "pause", now=T0 + 0.5)
    press(room, "a", "seek", 100, now=T0 + 1.0)
    room.tick(T0 + WINDOW_SECONDS)
    assert room.state(T0 + WINDOW_SECONDS)["changes"] == [
        {"what": "pause", "by": "b-b", "page": "b", "name": ""},
        {"what": "seek", "by": "b-a", "page": "a", "name": "Asha"},
    ]
    room.propose(together.AUDIO, 2, "", together.OWNER, "", T0 + 10)
    assert room.state(T0 + 10)["changes"] == [
        {"what": "audio", "by": together.OWNER, "page": "", "name": ""}
    ]


def test_the_end_of_a_video_is_no_ones_change() -> None:
    room = joined(Room(), "a")
    press(room, "a", "play")
    room.tick(T0 + 601)
    assert room.state(T0 + 601)["paused"] is True
    assert room.state(T0 + 601)["changes"] == []


def test_a_page_says_its_viewers_name_with_each_report() -> None:
    room = joined(Room(), "a")
    room.report("a", "b-a", T0, room.video, name="Asha")
    assert room.describe(T0)["pages"][0]["name"] == "Asha"
    room.report("a", "b-a", T0 + 1, room.video)
    assert room.describe(T0 + 1)["pages"][0]["name"] == "", "a page that stops saying it"


def test_a_page_that_loads_again_with_the_other_look_does_not_wait_again() -> None:
    room = joined(Room(), "a")
    room.report("a", "b-a", T0, 1, 0.0, 600.0)
    room.reload(T0 + 1)  # the owner switched the player's look
    room.report("a2", "b-a", T0 + 3, 1, 0.0, 600.0, ready=True)
    assert room.cooldown("a2", T0 + 3) == 0.0
    # A browser that was not watching is a joiner all the same.
    room.report("x", "b-x", T0 + 3, 1, 0.0, 600.0, ready=True)
    assert room.cooldown("x", T0 + 3) == COOLDOWN_SECONDS


# --- the call


def in_call(room: Room, *pages: str, now: float = T0) -> Room:
    """The pages are in the group, and in the call, the first one having started it."""
    joined(room, *pages, now=now)
    for page in pages:
        room.start_call(page, now)
    return room


def test_call_rings_every_other_page_and_anyone_joins() -> None:
    room = joined(Room(), "a", "b", "c")
    assert not room.call_state("b", T0)["ring"], "no call, no ring"
    assert room.start_call("a", T0) == together.STARTED
    a, b = room.call_state("a", T0), room.call_state("b", T0)
    assert a["id"] and a["id"] == b["id"]
    assert [m["page"] for m in a["members"]] == ["a"]
    assert (a["ring"], b["ring"], room.call_state("c", T0)["ring"]) == (False, True, True)
    assert room.start_call("b", T0 + 1) == together.JOINED
    assert [m["page"] for m in room.call_state("c", T0 + 1)["members"]] == ["a", "b"]
    assert room.start_call("b", T0 + 2) == together.JOINED, "pressed again: still in, once"
    assert len(room.call.members) == 2


def test_call_a_page_that_comes_while_it_runs_is_rung_too() -> None:
    room = in_call(Room(), "a", "b")
    joined(room, "late")
    assert room.call_state("late", T0)["ring"]


def test_call_a_page_the_room_does_not_know_cannot_start_or_join() -> None:
    room = joined(Room(), "a")
    assert room.start_call("stranger", T0) == REFUSED
    assert not room.call.running


def test_call_with_two_end_ends_it_for_both() -> None:
    room = in_call(Room(), "a", "b")
    version = room.call.version
    assert room.call.leave("b") == together.ENDED
    assert not room.call.running and room.call.members == {}
    assert room.call.version > version, "the pages see it"
    state = room.call_state("a", T0)
    assert (state["id"], state["members"], state["ring"]) == ("", [], False)


def test_call_with_three_end_takes_out_only_the_one_who_pressed_it() -> None:
    room = in_call(Room(), "a", "b", "c")
    assert room.call.leave("c") == together.LEFT
    assert list(room.call.members) == ["a", "b"]
    assert not room.call_state("c", T0)["ring"], "who left is not rung again"
    assert room.start_call("c", T0 + 5) == together.JOINED, "but can join again"
    assert room.call.leave("x") == together.NOT_IN


def test_call_alone_at_its_start_end_ends_it() -> None:
    room = in_call(Room(), "a")
    assert room.call.leave("a") == together.ENDED and not room.call.running


def test_call_every_new_call_rings_everyone_again() -> None:
    room = in_call(Room(), "a", "b", "c")
    room.call.leave("c")
    room.call.leave("b")  # two left: it ends for both
    assert not room.call.running
    first = room.call.calls
    assert room.start_call("b", T0 + 10) == together.STARTED
    assert room.call.calls == first + 1
    assert room.call_state("c", T0 + 10)["ring"], "c left the last call, not this one"
    assert room.call_state("a", T0 + 10)["ring"]


def test_call_a_page_that_goes_quiet_leaves() -> None:
    room = in_call(Room(), "a", "b", "c")
    for page in ("a", "b"):
        room.call.touch(page, T0 + together.CALL_QUIET_SECONDS)
    room.tick(T0 + together.CALL_QUIET_SECONDS + 1)
    assert list(room.call.members) == ["a", "b"]
    room.call.touch("a", T0 + 100)
    room.tick(T0 + 100)  # b quiet too: two in the call, so it ends for a as well
    assert not room.call.running


def test_call_has_at_most_twenty() -> None:
    pages = [f"p{n}" for n in range(together.CALL_SIZE + 1)]
    room = joined(Room(), *pages)
    outcomes = [room.start_call(page, T0) for page in pages]
    assert outcomes[-1] == together.FULL
    assert len(room.call.members) == together.CALL_SIZE
    assert room.call_state(pages[-1], T0)["ring"], "still asked: someone may leave"


def test_call_keeps_each_ones_mic_and_camera() -> None:
    room = in_call(Room(), "a", "b", "c")
    a = room.call_state("a", T0)["members"][0]
    assert (a["mic"], a["cam"], a["sees"]) == (False, True, []), "in with the camera, muted"
    version = room.call.version
    assert room.call.set_state("a", True, False, ["b", "c", "a", "b", "nobody", 3])
    a = room.call_state("b", T0)["members"][0]
    assert (a["mic"], a["cam"], a["sees"]) == (True, False, ["b", "c"])
    assert room.call.version == version + 1
    room.call.set_state("a", "yes", None, "b")  # nothing that reads: nothing changes
    assert room.call.version == version + 1
    assert not room.call.set_state("x", True, True, [])


def test_call_passes_messages_to_a_page_until_it_has_them() -> None:
    room = in_call(Room(), "a", "b")
    call = room.call
    assert call.send("a", "b", together.OFFER, "sdp-1", T0)
    assert not call.send("a", "b", "shout", "x", T0), "not a kind of message"
    assert not call.send("a", "a", together.OFFER, "x", T0), "not to itself"
    assert not call.send("a", "x", together.OFFER, "x", T0), "not to a page out of the call"
    assert not call.send("a", "b", together.OFFER, "x" * (together.MAX_MAIL_DATA + 1), T0)
    assert not call.send("a", "b", together.OFFER, {"sdp": 1}, T0)
    assert call.news("b", call.version, 0) and not call.news("a", call.version, 0)
    mail = room.call_state("b", T0)["mail"]
    assert [(m["from"], m["kind"], m["data"]) for m in mail] == [("a", "offer", "sdp-1")]
    again = room.call_state("b", T0 + 1)["mail"]
    assert again == mail, "lost on the way: it comes again"
    seq = mail[0]["seq"]
    assert room.call_state("b", T0 + 1, after=seq)["mail"] == []
    assert not call.news("b", call.version, seq)
    assert call.news("b", call.version - 1, seq), "a change of the call is news"


def test_call_a_mailbox_keeps_the_newest_and_not_for_long() -> None:
    room = in_call(Room(), "a", "b")
    for n in range(together.MAIL_SIZE + 5):
        room.call.send("a", "b", together.OFFER, str(n), T0)
    mail = room.call_state("b", T0)["mail"]
    assert len(mail) == together.MAIL_SIZE and mail[0]["data"] == "5"
    later = T0 + together.MAIL_SECONDS + 1
    room.call.touch("a", later)
    room.call.touch("b", later)
    assert room.call_state("b", later)["mail"] == []
    room.tick(later)
    assert "b" not in room.call.mail


def test_call_a_page_sends_only_so_often() -> None:
    call = Room().call
    assert all(call.allow("a", T0) for _ in range(together.POSTS))
    assert not call.allow("a", T0 + 1)
    assert call.allow("b", T0 + 1), "each page its own"
    assert call.allow("a", T0 + together.POST_SECONDS + 1)


def test_call_ending_drops_its_mail() -> None:
    room = in_call(Room(), "a", "b")
    room.call.send("a", "b", together.OFFER, "x", T0)
    room.call.leave("a")
    assert room.call.mail == {}


def test_call_shows_on_the_dashboard_and_its_count_survives_a_restart() -> None:
    room = in_call(Room(), "a", "b")
    room.pages["a"].name = "Asha"
    room.call.set_state("b", True, None, None)
    call = room.describe(T0)["call"]
    assert call["id"] == room.call.id
    assert call["members"] == [
        {"page": "a", "device": "b-a", "name": "Asha", "mic": False, "cam": True},
        {"page": "b", "device": "b-b", "name": "", "mic": True, "cam": True},
    ]
    back = Room.from_dict(room.to_dict(T0), 1)
    assert not back.call.running, "the connections went with the daemon"
    assert back.call.version > room.call.version and back.call.calls == room.call.calls
    assert Room.from_dict({"call": {"version": "x", "calls": -1}}, 1).call.version == 1
