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
        "15.5 MB",
        "10 s ago",
    )
    assert rows[1].what == "Browser" and rows[1].code == "K7" and rows[1].name == "(no name)"
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
