"""What the vpstream window says and sends (`platform.windows.gui.model`), the way it starts
(`gui.main`). The Qt window itself is tried in test_windows_qt, where PySide6 is installed
(Windows)."""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from vpstream import cli, gate
from vpstream.platform import windows
from vpstream.platform.windows import commands, gui, processes
from vpstream.platform.windows.gui import model
from vpstream.platform.windows.network import Address

WIFI = Address("192.168.1.73", "Wi-Fi", 16, public=True, network="Home")
WIRED = Address("172.16.0.184", "Ethernet", 5, public=False, network="")


def test_the_address_list_starts_with_automatic() -> None:
    choices = model.address_choices([WIFI, WIRED], "192.168.1.73")
    assert [c.label for c in choices] == [
        "Automatic (192.168.1.73)",
        "192.168.1.73 (Wi-Fi)",
        "172.16.0.184 (Ethernet)",
    ]
    assert model.chosen_label(choices, "") == "Automatic (192.168.1.73)"
    assert model.chosen_label(choices, "172.16.0.184") == "172.16.0.184 (Ethernet)"
    assert model.chosen_label(choices, "10.9.9.9") == "10.9.9.9 (not on this PC now)"
    assert model.address_of(choices, "172.16.0.184 (Ethernet)") == "172.16.0.184"
    assert model.address_of(choices, "nothing") is None
    assert model.address_choices([], "")[0].label == "Automatic (no network now)"


def test_a_start_names_the_file_as_windows_does() -> None:
    options = model.start_options(
        "C:/Films/Été/a.mkv", "file", " film ", model.DIRECT, model.POLICIES[gate.LOCKED]
    )
    assert options == {
        "file": "C:\\Films\\Été\\a.mkv",
        "mode": "direct",
        "policy": "locked",
        "alias": "film",
    }
    assert "alias" not in model.start_options("C:/a", "dir", "  ", model.DIRECT, "?")
    assert model.start_options("C:/a", "dir", "", model.DIRECT, "?")["policy"] == gate.APPROVAL
    with pytest.raises(ValueError, match="Pick a video file or a folder"):
        model.start_options("", "file", "", model.DIRECT, "")


def test_every_policy_has_words_and_each_mode_its_default() -> None:
    assert set(model.POLICIES) == set(gate.POLICIES)
    for key, label in model.POLICIES.items():
        assert model.policy_of(label) == key
    assert model.default_policy(model.DIRECT) == model.POLICIES[gate.OPEN]
    assert model.default_policy(model.BROWSER) == model.POLICIES[gate.APPROVAL]


def device(**data: Any) -> dict[str, Any]:
    return {
        "id": "192.168.1.50",
        "ip": "192.168.1.50",
        "kind": gate.PLAYER,
        "allowed": None,
        "passes": False,
        "user_agent": "VLC/3.0.20 LibVLC/3.0.20",
        "bytes": 0,
        "last_seen": 1000.0,
        "name": "",
        "code": "",
        **data,
    }


@pytest.mark.parametrize(
    ("data", "policy", "said"),
    [
        ({"allowed": True, "passes": True}, gate.LOCKED, "Let in"),
        ({"allowed": False}, gate.OPEN, "Kept out"),
        ({"passes": True}, gate.OPEN, "Watching (anyone may)"),
        ({}, gate.APPROVAL, "Waiting for you"),
        ({}, gate.LOCKED, "Kept out (not let in)"),
    ],
)
def test_the_state_of_a_device_in_words(data: dict[str, Any], policy: str, said: str) -> None:
    assert model.device_state(device(**data), policy) == said


def test_the_device_table() -> None:
    running = {
        "policy": gate.APPROVAL,
        "devices": [
            device(bytes=15_492_232, last_seen=990.0, name="TV room"),
            device(id="b-1", ip="192.168.1.9", kind=gate.BROWSER, user_agent="Mozilla", code="K7"),
            device(id="192.168.1.8", user_agent="Lavf/60"),
        ],
    }
    rows = model.device_rows(running, now=1000.0)
    assert rows[0].cells == (
        "TV room",
        "192.168.1.50",
        "VLC",
        "Waiting for you",
        "",
        "15.5 MB",
        "10 s ago",
    )
    assert rows[1].what == "Browser" and rows[1].code == "K7" and rows[1].name == "(no name)"
    assert rows[1].cells[2:5] == ("Browser K7", "Waiting for you", "not playing yet")
    assert rows[2].what == "Player"
    assert [model.ago(s) for s in (1, 30, 600, 7200)] == [
        "now",
        "30 s ago",
        "10 min ago",
        "2 h ago",
    ]
    assert [model.size(n) for n in (5, 1500, 2_500_000_000)] == ["5 B", "1.5 kB", "2.5 GB"]


def test_what_the_window_says_about_a_stream() -> None:
    status = {
        "stream": {
            "kind": "dir",
            "source": "C:\\Films\\Show",
            "url": "http://192.168.1.73:1337/show",
            "policy": gate.APPROVAL,
            "seconds_left": 3600.0,
            "videos": [
                {"number": 1, "name": "e1.mkv", "note": None, "preparing": True},
                {
                    "number": 2,
                    "name": "e2.mkv",
                    "note": "VLC resumes it slowly",
                    "preparing": False,
                },
            ],
            "devices": [device()],
        }
    }
    lines = model.stream_lines(status)
    assert lines[0] == "Streaming 2 videos from C:\\Films\\Show"
    assert lines[1] == "It stops by itself in 1h 00m."
    assert "http://192.168.1.73:1337/show/1, /2" in lines[2]
    assert "Video 2: VLC resumes it slowly" in lines
    assert any("being made" in line for line in lines)
    assert lines[-1].startswith("A device is waiting")


def test_the_warnings() -> None:
    assert model.warnings(WIRED, []) == []
    public, blocked, other = model.warnings(WIFI, ["caddy.exe"], "Admin")
    assert 'network "Home" on Wi-Fi as Public' in public.text
    assert public.button == model.MAKE_PRIVATE
    assert "block Caddy" in blocked.text and blocked.button == model.UNBLOCK
    assert "runs as Admin" in other.text and other.button == ""


# --- starting


@pytest.fixture
def boxes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What would be in a message box (on Windows a real one would wait for a click)."""
    said: list[str] = []
    monkeypatch.setattr(gui, "message", lambda text, error=True: said.append(text))
    return said


def test_vpstream_alone_asks_for_administrator_rights_first(
    monkeypatch: pytest.MonkeyPatch, boxes: list[str]
) -> None:
    asked: list[str] = []
    monkeypatch.setattr(processes, "is_admin", lambda: False)
    monkeypatch.setattr(processes, "relaunch_elevated", lambda user: asked.append(user) or False)
    monkeypatch.setattr(processes, "user", lambda: "ana")
    assert gui.main() == 1
    assert asked == ["ana"] and "needs administrator rights" in boxes[0]
    monkeypatch.setattr(processes, "relaunch_elevated", lambda user: True)
    assert gui.main() == 0  # the window is the one Windows starts


def test_a_second_window_is_not_opened(monkeypatch: pytest.MonkeyPatch, boxes: list[str]) -> None:
    class Taken:
        first = False

    monkeypatch.setattr(processes, "is_admin", lambda: True)
    monkeypatch.setattr(processes, "SingleInstance", Taken)
    assert gui.main() == 0 and boxes == ["vpstream is already open."]


def test_the_window_started_as_administrator_says_who_asked(
    on_windows: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(windows, "prepare_process", lambda: None)
    opened: list[str | None] = []
    monkeypatch.setattr(commands, "open_window", lambda who=None: opened.append(who) or 0)
    assert cli.main(["--elevated-for", "ana"]) == 0
    assert opened == ["ana"]


# --- browser mode

PLAN = {
    "name": "film.mkv",
    "tasks": [
        {"task": "base version (copy)", "kind": "base", "sub": 0, "seconds": 2.7},
        {"task": "subtitle 5 (burned in)", "kind": "burn", "sub": 5, "seconds": 46.9},
    ],
    "notes": [],
    "tracks": [
        {"kind": "audio", "number": 1, "label": "English (AAC 2.0)", "treatment": "AAC"},
        {"kind": "subtitle", "number": 2, "label": "Hindi (SRT)", "treatment": "soft"},
        {"kind": "subtitle", "number": 5, "label": "Japanese (ASS)", "treatment": "burned in"},
        {"kind": "subtitle", "number": 9, "label": "German (DVB)", "treatment": "unsupported"},
    ],
}


def test_the_tracks_to_offer_and_what_is_left() -> None:
    audio, subs = model.plan_tracks([{"name": "x", "tracks": [], "notes": []}, PLAN])
    assert [t.text for t in audio] == ["1: English (AAC 2.0)"]
    assert [t.text for t in subs] == [
        "2: Hindi (SRT) (shown by the player)",
        "5: Japanese (ASS) (drawn into the picture: a slower prepare)",
        "9: German (DVB) (cannot be shown)",
    ]
    assert model.plan_tracks([]) == ([], [])
    assert model.sub_spec([5, 2, 5]) == "2,5" and model.sub_spec([]) == "none"
    assert model.what_is_left([PLAN]) == [
        "film.mkv: base version (copy), subtitle 5 (burned in) (about 49s)"
    ]
    done = {**PLAN, "tasks": []}
    assert model.what_is_left([done]) == []
    unreadable = {"name": "bad.mkv", "tasks": [], "tracks": [], "notes": ["ffprobe cannot read it"]}
    assert model.what_is_left([unreadable]) == ["bad.mkv: ffprobe cannot read it"]


def test_a_browser_start_names_what_each_browser_begins_with() -> None:
    options = {"file": "C:\\a.mkv", "mode": "direct", "policy": "approval"}
    assert model.browser_start(options, 2, 5) == {
        "file": "C:\\a.mkv", "mode": "browser", "policy": "approval", "audio": "2", "sub": "5",
    }  # fmt: skip
    assert model.browser_start(options, None, None)["sub"] == "off"


def test_times_to_go_to() -> None:
    assert [model.seconds_of(t) for t in ("90", "12:30", "1:02:03", " 0:05 ")] == [
        90.0, 750.0, 3723.0, 5.0,
    ]  # fmt: skip
    for bad in ("", "1:2:3:4", "ten", "-5", "1.5"):
        with pytest.raises(ValueError, match="not a time"):
            model.seconds_of(bad)
    assert model.clock(3723) == "1:02:03" and model.clock(75) == "1:15"


def test_where_a_browser_is_and_its_choices() -> None:
    playing = {"client": {"video": 2, "position": 75, "duration": 1800, "paused": False,
                          "active": True}}  # fmt: skip
    assert model.where_it_is(playing) == "video 2, 1:15 of 30:00, playing"
    assert model.where_it_is({"client": None}) == "not playing yet"
    quiet = {"client": {**playing["client"], "active": False}}
    assert model.where_it_is(quiet) == "video 2, gone quiet"
    menu = {
        "audio": [{"value": "1", "label": "English"}],
        "subs": [{"value": "2-soft", "label": "Hindi"}],
    }
    status = {"stream": {"videos": [{"menu": None}, {"menu": menu}]}}
    found = model.menu_for(status, playing)
    assert found["audio"] == menu["audio"]
    assert found["subs"] == [{"value": "off", "label": "Off"}, *menu["subs"]]
    assert model.menu_for(status, None) == {"audio": [], "subs": [{"value": "off", "label": "Off"}]}


# --- the prepared videos

ROW = {
    "key": "k1", "source": "C:\\Films\\B film.mkv", "stale": False, "unchecked": False,
    "base": "copy", "soft": [2], "burn": [5], "bytes": 40_000_000, "in_use": False, "busy": False,
}  # fmt: skip


def test_the_prepared_videos_ready_ones_first() -> None:
    rows = [
        {**ROW, "key": "k2", "source": "C:\\Films\\a gone.mkv", "stale": True},
        ROW,
        {**ROW, "key": "k3", "source": "D:\\x\\c.mkv", "busy": True, "soft": [], "burn": []},
        {**ROW, "key": "k4", "source": "D:\\x\\d.mkv", "in_use": True},
        {**ROW, "key": "k5", "source": "D:\\x\\e.mkv", "base": ""},
    ]
    found = model.library_rows(rows)
    assert [r.key for r in found] == ["k1", "k4", "k2", "k3", "k5"]
    assert found[0].cells == ("B film.mkv", "2, 5 (drawn in)", "40.0 MB", "ready")
    assert found[1].state == "streaming now" and found[1].ready
    assert found[2].state == "the file changed or is gone" and not found[2].ready
    assert found[3].state == "being prepared" and found[3].subtitles == "none"
    assert found[4].state == "not ready (no base version)" and not found[4].ready


def test_what_a_prepared_file_starts_with() -> None:
    assert model.prepared_subs([ROW], "c:\\films\\B FILM.mkv") == [2, 5]  # either case
    assert model.prepared_subs([ROW], "C:\\other.mkv") == []


def test_the_next_video_takes_the_running_streams_address() -> None:
    running = {"alias": "tv", "policy": gate.LOCKED}
    assert model.switch_options(running, "C:\\b.mkv") == {
        "file": "C:\\b.mkv", "mode": "browser", "sub": "off", "alias": "tv", "policy": "locked",
    }  # fmt: skip
    assert "alias" not in model.switch_options(None, "C:\\b.mkv")


def job(id: int, state: str, task: str, source: str = "C:\\a.mkv", **more: Any) -> dict[str, Any]:
    name = source.rsplit("\\", 1)[-1]
    found = {"id": id, "source": source, "name": name, "task": task, "state": state}
    return {**found, "estimate": 10.0, "progress": 0.0, "seconds_left": None, **more}


def test_the_queue_has_one_row_and_one_bar_for_each_video() -> None:
    jobs = [
        job(1, "done", "base version (copy)"),
        job(2, "running", "subtitle 1 (soft)", progress=0.5, seconds_left=5.0),
        job(3, "queued", "subtitle 4 (burned in)", estimate=30.0, seconds_left=30.0),
        job(4, "queued", "base version (copy)", source="C:\\b.mkv"),
        job(5, "done", "base version (copy)", source="C:\\c.mkv"),
    ]
    first, second = model.video_jobs(jobs)  # c.mkv is prepared: it left the queue
    assert (first.key, first.state, second.state) == (1, "running", "queued")
    assert first.progress == pytest.approx((10 * 1 + 10 * 0.5) / 50)  # by expected time
    assert first.line == "a.mkv: part 2 of 3: subtitle 1 (soft), 30%, about 35s left"
    assert second.line == "b.mkv: waits for the one before (1 part)" and second.active


def test_a_video_prepared_again_is_a_row_of_its_own() -> None:
    jobs = [job(1, "done", "base version (copy)"), job(2, "queued", "x", source="C:\\b.mkv"),
            job(3, "running", "subtitle 2 (soft)")]  # fmt: skip
    rows = model.video_jobs(jobs)
    assert [(r.key, r.state) for r in rows] == [(2, "queued"), (3, "running")]
    assert rows[1].line.endswith("starting...")


def test_a_video_that_failed_or_was_cancelled_says_so() -> None:
    failed = model.video_jobs(
        [job(1, "done", "base"), job(2, "failed", "subtitle 4", error="ffmpeg")]
    )
    assert failed[0].line == "a.mkv: subtitle 4 failed: ffmpeg" and failed[0].state == "failed"
    cancelled = model.video_jobs([job(1, "cancelled", "base"), job(2, "cancelled", "sub")])
    assert cancelled[0].line == "a.mkv: cancelled"
    many = model.video_jobs([job(n, "failed", "base", source=f"C:\\v{n}.mkv") for n in range(15)])
    assert [r.key for r in many] == list(range(14, 9, -1))  # the last 5 that failed
    again = model.video_jobs([job(1, "failed", "base"), job(5, "done", "base")])
    assert again == []  # failed, then prepared after all: nothing left to say
    done = model.video_jobs([job(n, "done", "base", source=f"C:\\v{n}.mkv") for n in range(3)])
    assert done == []  # prepared: in the list of prepared videos, not in the queue


# --- the bars


def test_a_bar_is_busy_only_while_a_job_runs_at_nothing() -> None:
    assert model.bar_target("running", 0.0) is None
    assert model.bar_target("running", 0.4) == 0.4
    assert model.bar_target("queued", 0.0) == 0.0
    assert model.bar_target("done", 0.3) == 1.0
    assert model.bar_target("failed", 1.7) == 1.0  # never past the end
    assert model.bar_target("running", float("nan")) == 0.0
    assert model.setup_target("downloading", 0, 0) is None
    assert model.setup_target("downloading", 5, 10) == 0.5
    assert model.setup_target(model.INSTALLED, 0, 0) == 1.0
    assert model.setup_target(model.failed_line(OSError("no")), 3, 10) == 0.0


def test_a_bar_glides_to_its_value_and_stops_there() -> None:
    def frames(start: float, target: float) -> int:
        value = start
        for frame in range(1, 100):
            value = model.glide(value, target)
            if value == target:
                return frame
        raise AssertionError(f"{start} never arrived at {target}")

    assert 10 < frames(0.0, 0.5) < 40  # about a second at 25 frames a second
    assert 10 < frames(0.9, 0.2) < 40  # back too (a job started again)


def test_the_busy_bar_slides_across_calmly() -> None:
    start, end = model.busy_span(0.0)
    assert start == 0.0 and end == 0.0  # it comes in from the left
    assert model.busy_span(model.BUSY_SECONDS / 2) == pytest.approx((0.375, 0.625))
    assert model.busy_span(model.BUSY_SECONDS * 0.999)[1] == 1.0
    assert model.busy_span(model.BUSY_SECONDS * 3.5) == pytest.approx((0.375, 0.625))
    spans = [model.busy_span(i * model.FRAME_SECONDS) for i in range(200)]
    assert all(0.0 <= s <= e <= 1.0 for s, e in spans)
    steps = [abs(b[0] - a[0]) for a, b in itertools.pairwise(spans) if b[0] >= a[0]]
    assert max(steps) < 0.03  # each frame moves it a little: calm, not jumpy


# --- the words


def test_what_the_player_panel_sends() -> None:
    assert model.send_options(["a"], "1", None, " 1:30 ") == {
        "devices": ["a"], "audio": "1", "seek": 90.0,
    }  # fmt: skip
    assert model.send_options(None, None, "off", "") == {"all": True, "sub": "off"}
    with pytest.raises(ValueError, match="Select a browser"):
        model.send_options([], "1", None, "")
    with pytest.raises(ValueError, match="Choose audio"):
        model.send_options(None, None, None, "")
    with pytest.raises(ValueError, match="not a time"):
        model.send_options(None, None, None, "soon")


def test_what_the_start_panel_and_the_prepare_tab_say() -> None:
    assert model.start_state([]) == "Prepared: it can start."
    six = [f"{n}.mkv: base" for n in range(6)]
    said = model.start_state(six)
    assert said.startswith("Not prepared yet:\n0.mkv") and said.endswith("\n...")
    assert "5.mkv" not in said
    assert model.left_text([]) == "All of it is prepared."
    assert model.left_text(["a.mkv: base"]) == "To prepare:\na.mkv: base"


def test_the_questions() -> None:
    file = {"kind": "file", "source": "C:\\F\\a.mkv", "videos": [{"name": "a.mkv"}]}
    folder = {"kind": "dir", "source": "C:\\Show", "videos": [{"name": "1.mkv"}]}
    assert model.streaming_what(file) == "a.mkv" and model.streaming_what(folder) == "C:\\Show"
    assert model.ask_switch("a.mkv", "C:\\F\\Fïlm test\\épisode 1.mkv") == (
        "Stop streaming a.mkv and stream épisode 1.mkv at the same address?"
    )
    row = model.library_rows([{"key": "k", "source": "C:\\b.mkv", "bytes": 2_000_000}])[0]
    assert model.ask_remove([row]) == (
        "Remove what was prepared of b.mkv (2.0 MB)? The video itself stays."
    )
    other = model.library_rows([{"key": "j", "source": "C:\\c.mkv", "bytes": 500_000_000}])[0]
    assert model.ask_remove([row, other]) == (
        "Remove what was prepared of 2 videos (502.0 MB)? The videos themselves stay."
    )
    assert model.kept_said([]) == ""
    assert model.kept_said(["C:\\b.mkv", "C:\\b.mkv"]).startswith("b.mkv is streaming")
    assert model.kept_said(["C:\\F\\b.mkv"]).startswith("b.mkv is streaming")
    assert model.kept_said(["a", "b"]) == (
        "2 videos are streaming or being prepared, so they were kept."
    )
    assert model.not_ready(row) == "b.mkv is not ready (no base version)."
    assert model.ask_long("It takes 2h") == "It takes 2h.\n\nPrepare it all the same?"
    assert model.prepared_said(0) == "Prepared." and model.prepared_said(2).startswith("2 videos")
    assert model.prepared_said(1) == "1 video still to prepare."
    assert model.queued_said(1).startswith("Queued: 1 part.")
    assert model.close_question(False, False) is None
    assert "is running" in (model.close_question(True, True) or "")
    assert "is starting" in (model.close_question(True, False) or "")
    assert "being downloaded" in (model.close_question(False, False, downloading=True) or "")
    assert "Stop both" in (model.close_question(False, True, downloading=True) or "")
    assert model.cancelled_said(3) == "Cancelled 3 parts."


# --- the Tools tab


def test_which_release_is_newer() -> None:
    assert model.newer("103.0", "102.0") and model.newer("2.11.10", "2.11.9")
    assert model.newer("9.1", "9.0.2") and not model.newer("9.0", "9.0.0")
    assert not model.newer("2.11.9", "2.11.9") and not model.newer("7.1", "9.0.2")
    assert model.newer("1.0", "")  # anything is newer than an unknown version


def row(**over: Any) -> model.ToolRow:
    given: dict[str, Any] = {
        "name": "caddy", "title": "Caddy", "path": "C:\\t\\caddy\\caddy.exe", "own": True,
        "version": "2.11.9", "own_version": "2.11.9", "latest": None,
    }  # fmt: skip
    return model.tool_row(**{**given, **over})


def test_update_is_offered_only_for_vpstreams_own_copy_when_a_newer_one_is_out() -> None:
    assert row().latest == "not looked up yet" and row().action == ""
    assert row().where == "C:\\t\\caddy\\caddy.exe (vpstream's copy)" and row().can_remove
    assert (
        row(latest="2.11.9").latest == "2.11.9 (up to date)" and row(latest="2.11.9").action == ""
    )
    out = row(latest="2.12.0")
    assert (out.latest, out.action) == ("2.12.0 is out", model.UPDATE)
    system = row(path="C:\\Program Files\\caddy.exe", own=False, own_version=None, latest="2.12.0")
    assert system.where.endswith("(installed for the whole system)")
    assert (system.latest, system.action, system.can_remove) == ("2.12.0", model.INSTALL_OWN, False)
    missing = row(path=None, own=False, version="", own_version=None)
    assert (missing.where, missing.version, missing.action) == (
        "not found",
        "unknown",
        model.INSTALL_OWN,
    )
    unmarked = row(own_version=None)  # a folder copied there by hand: not vpstream's to remove
    assert not unmarked.can_remove and unmarked.version == "2.11.9"
    assert model.tool_removed("Caddy", True) == "vpstream's copy of Caddy is removed."
    assert model.tool_removed("Caddy", False) == "vpstream has no copy of Caddy to remove."
    assert row(checking=True).latest == "looking..."
    assert row(problem="HTTP 403").latest == "cannot look it up: HTTP 403"


def test_the_tools_questions() -> None:
    assert model.ask_install("Caddy", "2.12.0", 18_384_117, "C:\\caddy.exe", own=False) == (
        "Download Caddy 2.12.0 (18 MB) into vpstream's folder? vpstream uses it in place of "
        "C:\\caddy.exe."
    )
    assert model.ask_install("MKVToolNix", "103.0", 0, None, own=False).endswith(
        "? vpstream uses it."
    )
    assert "in place of vpstream's copy" in model.ask_install("Caddy", "2.12.0", 0, "x", own=True)
    assert model.ask_remove_tool("Caddy").startswith("Remove vpstream's copy of Caddy?")


def test_a_downloads_speed_is_over_the_last_seconds() -> None:
    samples, rate = model.speed([], 100.0, 0)
    assert rate is None  # one sample: no speed yet
    samples, rate = model.speed(samples, 101.0, 4_000_000)
    assert rate == 4_000_000.0
    samples, rate = model.speed(samples, 105.0, 12_000_000)  # a slow line: 4 s apart
    assert samples == [(101.0, 4_000_000), (105.0, 12_000_000)] and rate == 2_000_000.0
    samples, rate = model.speed(samples, 106.0, 22_000_000)
    assert rate == 10_000_000.0  # it follows the line, not the average since the start
    samples, rate = model.speed(samples, 120.0, 22_000_000)
    assert samples[0][0] == 106.0 and rate == 0.0  # stalled: 0, which is not shown
    samples, rate = model.speed(samples, 106.5, 1_000)  # a new download: from its start
    assert samples == [(106.5, 1_000)] and rate is None


def test_what_a_download_says() -> None:
    assert model.progress_said("unpacking ffmpeg", 0, 0, None) == "unpacking ffmpeg"
    assert model.progress_said("downloading Caddy 2.11.9", 6_100_000, 18_400_000, 4_200_000) == (
        "downloading Caddy 2.11.9: 6.1 MB of 18.4 MB, 4.2 MB/s"
    )
    assert model.progress_said("downloading x", 5, 10, None) == "downloading x: 5 B of 10 B"
    assert model.progress_said("downloading x", 0, 10, 0.0) == "downloading x: 0 B of 10 B"
    assert model.progress_said("downloading x", 10, 10, 9.0) == "downloading x: 10 B of 10 B"
