"""What the vpstream window says and sends (`platform.windows.gui.model`), the way it starts
(`gui.main`), and the closing of the Worker's answers; the Tk widgets themselves are tried
on Windows."""

from __future__ import annotations

import queue
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
    assert 'network "Home" on Wi-Fi as Public' in public
    assert "block Caddy" in blocked
    assert "runs as Admin" in other


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


def test_an_answer_of_the_worker_runs_on_the_window_thread() -> None:
    pytest.importorskip("tkinter", reason="this Python has no tkinter")
    from vpstream.platform.windows.gui.app import Worker

    answers: queue.Queue[Any] = queue.Queue()
    lines: list[str] = []
    worker = Worker(answers, lines.append)
    got: list[Any] = []
    worker.do(lambda: 41 + 1, got.append)
    worker.do(lambda: 1 / 0, None, lambda exc: got.append(type(exc).__name__))
    for _ in range(2):
        answers.get(timeout=5)()
    assert got == [42, "ZeroDivisionError"] and worker.busy == 0
    assert "ZeroDivisionError" in lines[0]


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


def test_the_jobs_of_the_chosen_videos() -> None:
    jobs = [
        {"name": "film.mkv", "task": "base version (copy)", "state": "failed", "error": "x"},
        {"name": "film.mkv", "task": "base version (copy)", "state": "running",
         "progress": 0.42, "seconds_left": 30.0},
        {"name": "other.mkv", "task": "base version (copy)", "state": "queued"},
        {"name": "film.mkv", "task": "subtitle 5 (burned in)", "state": "queued"},
    ]  # fmt: skip
    mine = model.jobs_of(jobs, ["film.mkv"])
    assert [model.job_line(j) for j in mine] == [
        "film.mkv: base version (copy), 42%, about 30s left",
        "film.mkv: subtitle 5 (burned in): waits for the one before",
    ]
    assert model.busy(mine) and not model.busy([{"state": "done"}])
    starting = {"name": "a", "task": "t", "state": "running", "progress": 0.0}
    assert model.job_line(starting) == "a: t: starting..."
    failed = {"name": "a", "task": "t", "state": "failed", "error": "ffmpeg failed"}
    assert model.job_line(failed) == "a: t: failed: ffmpeg failed"


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


def test_the_queue_shows_what_runs_then_the_last_that_ended() -> None:
    jobs = [{"id": n, "state": "done", "finished": float(n)} for n in range(15)]
    jobs += [{"id": 99, "state": "queued"}, {"id": 98, "state": "running"}]
    shown = model.all_jobs(jobs)
    assert [j["id"] for j in shown] == [99, 98, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5]
