"""The Qt window (`gui.app`), headless (Qt's offscreen platform): answers come back on the
window's thread, rows change where they are and keep their selection, one bar for each
video, keep-awake on its own thread, and closing asks first and closes the session. Runs
where PySide6 is installed (Windows); elsewhere it is skipped."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is installed on Windows")

from vpstream import gate  # noqa: E402
from vpstream.platform.windows import processes  # noqa: E402
from vpstream.platform.windows.gui import app, widgets  # noqa: E402


@pytest.fixture(scope="module")
def qt() -> Any:
    return app.application()


def wait(qt: Any, until: Callable[[], bool], seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qt.processEvents()
        if until():
            return True
        time.sleep(0.01)
    return False


def test_an_answer_of_the_worker_runs_on_the_window_thread(qt: Any) -> None:
    lines: list[str] = []
    bridge = app.Bridge(lines.append)
    worker = app.Worker(bridge.post, lines.append)
    got: list[Any] = []
    main = threading.get_ident()
    worker.do(lambda: threading.get_ident() != main, got.append)
    worker.do(lambda: 1 / 0, None, lambda exc: got.append(threading.get_ident() == main))
    assert wait(qt, lambda: worker.busy == 0)
    assert got == [True, True]  # the work ran elsewhere; both answers here
    assert "ZeroDivisionError" in lines[0]
    worker.stop()


def test_rows_change_where_they_are_and_keep_their_selection(qt: Any) -> None:
    view, rows = widgets.table(("Name", "State"), 1)
    widgets.fill(view, rows, [("a", ("TV", "Waiting")), ("b", ("Phone", "Let in"))])
    view.selectRow(1)
    item = rows.item(1, 1)
    widgets.fill(view, rows, [("a", ("TV", "Let in")), ("b", ("Phone", "Watching"))])
    assert rows.item(1, 1) is item and item.text() == "Watching"
    assert widgets.selected(view) == ["b"]
    widgets.fill(view, rows, [("c", ("New", "")), ("b", ("Phone", "Watching"))])
    assert widgets.keys_of(rows) == ["c", "b"] and widgets.selected(view) == ["b"]
    widgets.fill(view, rows, [("b", ("Phone", "Watching")), ("c", ("New", ""))])
    assert widgets.keys_of(rows) == ["b", "c"] and widgets.selected(view) == ["b"]


def test_a_bar_glides_and_stops_its_timer(qt: Any) -> None:
    bar = widgets.Bar()
    bar.set(0.5)
    assert bar.timer.isActive()
    assert wait(qt, lambda: bar.value == 0.5 and not bar.timer.isActive(), 3)
    bar.set(None)  # busy: it keeps moving until it gets a value
    assert wait(qt, lambda: False, 0.2) is False and bar.timer.isActive()
    bar.set(1.0)
    assert wait(qt, lambda: not bar.timer.isActive(), 3) and bar.value == 1.0


class FakeControl:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def host(self) -> str:
        return "192.168.1.73"

    def prepared(self) -> list[dict[str, Any]]:
        return []

    def prepare(self, options: dict[str, Any]) -> dict[str, Any]:
        return {"plans": []}

    def set_device(self, device: str, allowed: bool) -> None:
        self.calls.append(("set_device", (device, allowed)))


class FakeConfig:
    local_address = ""


class FakeSession:
    def __init__(self) -> None:
        self.control = FakeControl()
        self.config = FakeConfig()
        self.notice = ""
        self.closed = threading.Event()
        self.lines: list[str] = []
        self.order: list[str] = []

    def log(self, line: str) -> None:
        self.lines.append(line)

    def stop_reason(self) -> str:
        return "Caddy ended"

    def close(self) -> None:
        time.sleep(0.2)  # as the real one, it takes a moment: the window does not wait
        self.order.append("close")
        self.closed.set()

    def start(self, options: dict[str, Any]) -> dict[str, Any]:
        time.sleep(0.3)  # a start takes a moment
        if options.get("file") == "C:\\gone.mkv":
            raise FileNotFoundError("C:\\gone.mkv")
        self.order.append(f"start {options.get('file')}")
        return status([], [])

    def stop(self) -> dict[str, Any]:
        self.order.append("stop")
        return {"now": 1000.0, "prepare": [], "stream": None}


class FakeRunner:
    def which(self, name: str) -> str | None:
        return None


def device(**over: Any) -> dict[str, Any]:
    found = {"id": "192.168.1.50", "ip": "192.168.1.50", "kind": gate.PLAYER, "bytes": 0}
    return {**found, **over}


def status(devices: list[dict[str, Any]], prepare: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "now": 1000.0,
        "prepare": prepare,
        "stream": {
            "kind": "file",
            "mode": "direct",
            "source": "C:\\Films\\a.mkv",
            "alias": "a",
            "url": "http://192.168.1.73:1337/a",
            "policy": gate.APPROVAL,
            "videos": [{"number": 1, "name": "a.mkv"}],
            "devices": devices,
        },
    }


def job(id: int, source: str, state: str, progress: float = 0.0) -> dict[str, Any]:
    name = Path(source).name
    return {"id": id, "source": source, "name": name, "task": "base", "state": state,
            "progress": progress, "estimate": 10}  # fmt: skip


def test_the_window_shows_a_stream_and_closes_after_asking(
    qt: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    awake: list[tuple[bool, str]] = []
    monkeypatch.setattr(
        processes, "keep_awake", lambda on: awake.append((on, threading.current_thread().name))
    )
    asked: list[str] = []
    answers = [False, True]
    session = FakeSession()
    window = app.Window(
        session, FakeRunner(), tmp_path / "vpstream.log", "",  # type: ignore[arg-type]
        answer=lambda text, asking: asked.append(text) or answers.pop(0),
    )  # fmt: skip
    window.show()
    queue = [
        job(1, "C:\\a.mkv", "done"),
        job(2, "C:\\a.mkv", "running", 0.5),
        job(3, "C:\\b.mkv", "queued"),
        job(4, "C:\\b.mkv", "queued"),
    ]
    window.show_status(status([device(), device(id="b", ip="192.168.1.9")], queue))
    assert window.pages.currentIndex() == 1
    assert window.url.text() == "http://192.168.1.73:1337/a"
    assert widgets.keys_of(window.device_rows) == ["192.168.1.50", "b"]
    assert len(window.prepare_tab.job_rows) == 2  # one row and one bar for each video
    window.devices.selectRow(1)
    window.show_status(status([device(), device(id="b", ip="192.168.1.9", bytes=5000)], queue))
    assert widgets.selected(window.devices) == ["b"]
    assert window.device_rows.item(1, 5).text() == "5.0 kB"
    assert wait(qt, lambda: awake == [(True, "awake")])

    window.close()  # No: it stays
    assert asked == ["A stream is running. Stop it and close vpstream?"]
    assert wait(qt, lambda: False, 0.1) is False and window.isVisible() and not window.closing
    window.close()  # Yes: the answer comes as a real one does, through the event loop
    assert wait(qt, lambda: window.closing) and not session.closed.is_set()  # no waiting
    assert wait(qt, lambda: window.closed, 5) and session.closed.is_set()
    assert not window.isVisible()
    assert awake == [(True, "awake"), (False, "awake")]


def window_for(session: FakeSession, tmp_path: Path, **over: Any) -> Any:
    return app.Window(session, FakeRunner(), tmp_path / "vpstream.log", "", **over)  # type: ignore[arg-type]


def test_a_question_box_does_not_wait_and_its_button_answers(qt: Any, tmp_path: Path) -> None:
    window = window_for(FakeSession(), tmp_path)
    window.show()
    said: list[str] = []
    Box = QtWidgets.QMessageBox

    def shown() -> Any:
        return next((b for b in window.findChildren(Box) if b.isVisible()), None)

    window.ask("Remove a?", lambda: said.append("a yes"), lambda: said.append("a no"))
    first = shown()
    assert first is not None and said == []  # open, and nothing waited
    first.button(Box.StandardButton.Yes).click()
    assert wait(qt, lambda: said == ["a yes"] and shown() is None)
    window.ask("Remove b?", lambda: said.append("b yes"), lambda: said.append("b no"))
    second = shown()
    assert second is not None and second is not first and second.text() == "Remove b?"
    second.button(Box.StandardButton.No).click()
    assert wait(qt, lambda: said == ["a yes", "b no"])


def test_notes_do_not_take_a_drives_answers(qt: Any, tmp_path: Path) -> None:
    heard: list[tuple[str, bool]] = []
    window = window_for(FakeSession(), tmp_path, answer=lambda t, q: heard.append((t, q)) or True)
    window.tell("Select a device in the list first.")
    window.ask("Remove it?", lambda: heard.append(("yes", True)))
    assert wait(qt, lambda: len(heard) == 3)
    assert heard == [
        ("Select a device in the list first.", False),
        ("Remove it?", True),
        ("yes", True),
    ]


def test_one_start_at_a_time_and_the_close_waits_for_it(qt: Any, tmp_path: Path) -> None:
    session = FakeSession()
    asked: list[str] = []
    window = window_for(session, tmp_path, answer=lambda t, q: asked.append(t) or True)
    window.show()
    window.stream_prepared("C:\\a.mkv")
    window.stream_prepared("C:\\a.mkv")  # a double click: one start
    assert window.starting and window.said.text() == "A stream is starting; try again in a moment."
    window.close()  # while it starts: asked, then closed once the start is done
    assert asked == ["A stream is starting. Stop it and close vpstream?"]
    assert wait(qt, lambda: window.closed, 5)
    assert session.order == ["start C:\\a.mkv", "close"]


def test_the_close_does_not_wait_for_a_job_that_hangs(
    qt: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app, "CLOSE_WAIT", 0.3)
    session = FakeSession()
    window = window_for(session, tmp_path)
    window.show()
    ran: list[str] = []
    under_way = threading.Event()
    window.worker.do(lambda: under_way.set() or time.sleep(3))  # a download, say
    window.worker.do(lambda: ran.append("queued"))  # dropped: the window closes
    assert under_way.wait(2)
    started = time.monotonic()
    window.close()
    assert wait(qt, lambda: window.closed, 2) and time.monotonic() - started < 1.5
    assert session.order == ["close"] and ran == [] and window.worker.busy == 1


def test_a_switch_to_a_browser_stream_shows_the_player_panel(qt: Any, tmp_path: Path) -> None:
    window = window_for(FakeSession(), tmp_path)
    window.show()
    window.show_status(status([], []))  # a direct stream
    assert window.pages.currentIndex() == 1 and window.players.isHidden()
    switched = status([], [])
    switched["stream"] |= {"mode": "browser", "url": "http://192.168.1.73:1337/a", "alias": "a"}
    window.show_status(switched)  # the same address, now for browsers: no poll between
    assert not window.players.isHidden()
    window.starting = True
    window.update_start()
    assert not window.live.isEnabled()  # no Stop while one starts


def test_a_switch_whose_start_fails_says_only_that(qt: Any, tmp_path: Path) -> None:
    session = FakeSession()
    told: list[str] = []
    window = window_for(session, tmp_path, answer=lambda t, q: told.append(t) or True)
    window.show()
    window.show_status(status([], []))
    window.stream_prepared("C:\\gone.mkv")  # asked, then the old one stops, the new one fails
    assert wait(qt, lambda: window.starting)  # once the question is answered
    gap = {"now": 1000.0, "prepare": [], "stream": None}
    window.show_status(gap)  # a poll between its stop and its start: the page stays
    assert window.pages.currentIndex() == 1
    assert wait(qt, lambda: not window.starting, 3)
    assert session.order == ["stop"] and "gone.mkv" in told[-1]
    window.show_status(gap)  # the next poll: the stop was the switch's, not Caddy's
    assert window.pages.currentIndex() == 0 and "Caddy ended" not in window.said.text()
    window.show_status(status([], []))
    window.show_status(gap)  # a stream that stops by itself says why
    assert window.said.text() == "The stream stopped: Caddy ended."


def test_the_close_does_not_wait_for_an_open_that_never_began(qt: Any, tmp_path: Path) -> None:
    session = FakeSession()
    session.open = lambda: None  # type: ignore[method-assign]
    window = window_for(session, tmp_path)
    window.show()
    window.worker.do(lambda: time.sleep(0.3))  # ahead of the open, which is then dropped
    window.begin()
    window.polling.set()
    window.close()
    assert wait(qt, lambda: window.closed, 3) and not window.open_begun.is_set()
    window.worker.do(lambda: None)  # after the close: not taken
    assert window.worker.busy == 0


def test_a_session_close_that_hangs_does_not_keep_the_window(
    qt: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app, "SESSION_CLOSE_WAIT", 0.3)
    session = FakeSession()
    hung = threading.Event()
    session.close = lambda: hung.wait(30)  # type: ignore[method-assign]
    window = window_for(session, tmp_path)
    window.show()
    window.close()
    try:
        assert wait(qt, lambda: window.closed, 3)
        assert any("did not close in time" in line for line in session.lines)
    finally:
        hung.set()  # the stand-in's thread ends with the test


def test_remove_takes_every_selected_prepared_video(qt: Any, tmp_path: Path) -> None:
    session = FakeSession()
    rows = [{"key": k, "source": f"C:\\{k}.mkv", "base": True, "bytes": 1_000_000} for k in "abc"]
    removed: list[str] = []

    def remove_prepared(options: dict[str, Any]) -> dict[str, Any]:
        removed.append(options["path"])
        return {"removed": 1, "kept": [options["path"]] if "b" in options["path"] else []}

    session.control.prepared = lambda: [r for r in rows if r["source"] not in removed]  # type: ignore[method-assign]
    session.control.remove_prepared = remove_prepared  # type: ignore[attr-defined]
    asked: list[str] = []
    window = window_for(session, tmp_path, answer=lambda t, q: asked.append(t) or True)
    tab = window.prepare_tab
    tab._library(rows)
    tab.table.selectAll()
    tab.stream_it()  # several selected: one is asked for
    assert asked == ["Select one video to stream."]
    asked.clear()
    tab.remove()
    assert wait(qt, lambda: len(removed) == 3 and window.worker.busy == 0)
    assert asked == ["Remove what was prepared of 3 videos (3.0 MB)? The videos themselves stay."]
    assert removed == ["C:\\a.mkv", "C:\\b.mkv", "C:\\c.mkv"]
    assert window.said.text() == "b.mkv is streaming or being prepared, so it was kept."


def test_the_tools_tab_offers_a_copy_an_update_and_a_removal(
    qt: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vpstream.platform.windows import install

    own = {"caddy": "2.11.8"}
    done: list[str] = []

    def found(runner: Any, tools: Path) -> list[install.Found]:
        rows = []
        for tool in install.TOOLS.values():
            if tool.name in own:
                path, version = f"C:\\v\\{tool.name}.exe", own[tool.name]
                rows.append(install.Found(tool, path, True, version, version))
            else:
                rows.append(install.Found(tool, f"C:\\sys\\{tool.name}.exe", False, "1.0", None))
        return rows

    def latest(tool: install.Tool, http: Any) -> install.Build:
        version = {"ffmpeg": "9.0.2", "caddy": "2.11.9", "mkvtoolnix": "103.0"}[tool.name]
        return install.Build(tool, version, "https://example.com/x.zip", "a" * 64, 5_000_000)

    def install_build(build: install.Build, *a: Any) -> str:
        own[build.name] = build.version
        done.append(f"{build.name} {build.version}")
        return f"{build.tool.title} {build.version} is installed"

    def remove_own(tools: Path, tool: install.Tool) -> bool:
        done.append(f"removed {tool.name}")
        return own.pop(tool.name, None) is not None

    for name, fake in (("found", found), ("latest", latest), ("install_build", install_build),
                       ("remove_own", remove_own)):  # fmt: skip
        monkeypatch.setattr(install, name, fake)
    asked: list[str] = []
    window = window_for(FakeSession(), tmp_path, answer=lambda t, q: asked.append(t) or True)
    window.show()
    tab = window.tools_tab
    window.tabs.setCurrentWidget(window.tools_page)  # shown: it looks, and for new releases
    assert wait(qt, lambda: tab.found and tab.latest and not tab.checking, 5)
    caddy, ffmpeg = tab.lines["caddy"], tab.lines["ffmpeg"]
    assert caddy.latest.text() == "2.11.9 is out" and caddy.action.text() == "Update"
    assert not caddy.action.isHidden() and not caddy.remove.isHidden()
    assert ffmpeg.action.text() == "Install vpstream's copy" and ffmpeg.remove.isHidden()
    assert ffmpeg.where.text() == "C:\\sys\\ffmpeg.exe (installed for the whole system)"

    caddy.action.click()
    assert wait(qt, lambda: done == ["caddy 2.11.9"] and not tab.busy and window.worker.busy == 0)
    assert asked[-1] == "Download Caddy 2.11.9 (5 MB) and use it in place of vpstream's copy?"
    assert wait(qt, lambda: tab.lines["caddy"].latest.text() == "2.11.9 (up to date)")
    assert caddy.action.isHidden() and window.said.text() == "Caddy 2.11.9 is installed"

    caddy.remove.click()
    assert wait(qt, lambda: "removed caddy" in done and window.worker.busy == 0)
    assert asked[-1].startswith("Remove vpstream's copy of Caddy?")
    assert wait(
        qt, lambda: caddy.remove.isHidden() and caddy.action.text() == "Install vpstream's copy"
    )
