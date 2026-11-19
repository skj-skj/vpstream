"""The vpstream window (Qt). One window: what is missing and installing it, what to stream
and how, and while it streams its address, the devices and Stop.

The window never waits. Whatever asks Windows, Caddy or the disk runs on another thread:
the worker (one job after another), the poll (the status once a second, on its own so a
long job never holds it up), the keep-awake thread while a stream runs, and the close. Their
answers come back through `Bridge`, a queued signal that Qt delivers on the window's thread;
only that thread touches a widget. What the window says and decides is in `model`.
"""

from __future__ import annotations

import contextlib
import functools
import os
import queue
import sys
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QCloseEvent, QFont, QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from vpstream import __version__, local
from vpstream.platform.common import UrlHttp
from vpstream.platform.windows import firewall, install, network, processes, shortcut, tools_dir
from vpstream.platform.windows.gui import browser, model, widgets
from vpstream.platform.windows.gui.browser import note, path_line, set_choices
from vpstream.platform.windows.session import Session
from vpstream.tools import Runner

POLL_SECONDS = 1.0
# How long the close waits for the worker's job under way (a download, say) before the
# session closes; Control's own work (a start) holds the session's close in turn.
CLOSE_WAIT = 15.0
OPEN_WAIT = 60.0  # and for the session's open, which starts the gate (a stream taken up)
# And for the session's close: a slow one (the indexer stopping, netsh, Caddy) ends well
# within this; one that hangs (a gate that never served cannot be shut down) must not keep
# the window open. Caddy goes with the window all the same.
SESSION_CLOSE_WAIT = 60.0
APP_ID = "vpstream"  # the taskbar's name for the window, not pythonw's
SIZE = (1100, 760)  # at 125% on a 1920x1080 screen: 1375x950
SMALLEST = (900, 640)

# A drive's stand-in for the person: (the text, whether it is a question) -> yes or no; a
# note's answer is not used.
Answer = Callable[[str, bool], bool]


class Bridge(QObject):
    """The one way back to the window's thread: `post(fn)` from any thread, and `fn` runs
    there, queued like any other event."""

    call = Signal(object)

    def __init__(self, log: Callable[[str], None]) -> None:
        super().__init__()
        self.log = log
        self.call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    def post(self, fn: Callable[[], None]) -> None:
        self.call.emit(fn)

    def _run(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as exc:  # a bug in the window must not end it
            self.log(f"WARNING: the window: {exc!r}\n{traceback.format_exc()}")


class Worker:
    """One thread that runs what the window asks, one thing after another."""

    def __init__(self, post: Callable[[Callable[[], None]], None], log: Callable[[str], None]):
        self.jobs: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self.post, self.log = post, log
        self.busy = 0  # jobs asked for and not answered (drives and tests wait on it)
        self.ended = False
        self.thread = threading.Thread(target=self._run, name="window-worker", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while (job := self.jobs.get()) is not None:
            job()

    def do(
        self,
        work: Callable[[], Any],
        done: Callable[[Any], None] | None = None,
        failed: Callable[[Exception], None] | None = None,
    ) -> None:
        """Run `work` here; `done` (or `failed`) then runs on the window's thread. Once
        stopped, nothing more is run."""
        if self.ended:
            return
        self.busy += 1

        def job() -> None:
            try:
                found = work()
            except Exception as exc:
                self.log(f"WARNING: {exc!r}\n{traceback.format_exc()}")
                answer = functools.partial(self._end, failed, exc)
            else:
                answer = functools.partial(self._end, done, found)
            with contextlib.suppress(RuntimeError):  # the window is gone (it outlived the close)
                self.post(answer)

        self.jobs.put(job)

    def _end(self, then: Callable[[Any], None] | None, value: Any) -> None:
        self.busy -= 1
        if then is not None and not self.ended:  # after stop() no answer is shown
            then(value)

    def stop(self) -> None:
        """No more jobs: the one under way ends, those waiting are dropped, and no answer
        is shown any more."""
        self.ended = True
        while True:
            try:
                self.jobs.get_nowait()
            except queue.Empty:
                break
            self.busy -= 1
        self.jobs.put(None)


def scrolled(inner: QWidget) -> QScrollArea:
    """A tab's page: what does not fit scrolls, the window keeps its size."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    area.setWidget(inner)
    return area


class Window(QMainWindow):
    def __init__(
        self,
        session: Session,
        runner: Runner,
        log_path: Path,
        other_admin: str = "",
        answer: Answer | None = None,
    ) -> None:
        super().__init__()
        self.session, self.runner = session, runner
        self.log_path, self.other_admin, self.answer = log_path, other_admin, answer
        self.bridge = Bridge(session.log)
        self.worker = Worker(self.bridge.post, session.log)
        self.status: dict[str, Any] | None = None
        self.addresses: list[network.Address] = []
        self.choices: list[model.Choice] = []
        self.blocking: list[str] = []
        self.awake: threading.Event | None = None  # set to end the keep-awake thread
        self.polling = threading.Event()  # set to end the poll
        self.open_begun = threading.Event()  # the worker took up the session's open
        self.opened = threading.Event()  # and it ended
        self.shown: tuple[str, str] | None = None  # the stream the live page is set up for
        self.was_streaming = False
        self.stopping_on_purpose = False
        self.closing = False
        self.asking_close = False
        self.closed = False
        self.starting = False
        self.setup_missing = False
        self.kind = "file"
        self.setup_rows: dict[str, tuple[QLabel, widgets.Bar]] = {}
        self.setWindowTitle("vpstream")
        self.setWindowIcon(QIcon(str(shortcut.ICON)))
        self.resize(*SIZE)
        self.setMinimumSize(*SMALLEST)
        self._build()
        self.update_start()

    # --- the layout

    def _build(self) -> None:
        central = QWidget()
        top = QVBoxLayout(central)
        self.notice = note()
        self.notice.setObjectName("notice")
        self.notice.hide()
        top.addWidget(self.notice)
        self.warn_box = QVBoxLayout()
        top.addLayout(self.warn_box)
        self.tabs = QTabWidget()
        top.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self.central = central

        # The Stream tab: two pages in one place, choosing and streaming.
        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_choose())
        self.pages.addWidget(self._build_live())
        self.stream_page = scrolled(self.pages)
        self.tabs.addTab(self.stream_page, "Stream")
        self.prepare_tab = browser.PrepareTab(self)
        self.prepare_page = scrolled(self.prepare_tab)
        self.tabs.addTab(self.prepare_page, "Prepare for browsers")

        # The bottom line, always there.
        bar = self.statusBar()
        bar.setSizeGripEnabled(False)
        self.said = QLabel(model.STARTING)
        self.said.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        bar.addWidget(self.said, 1)
        bar.addPermanentWidget(QLabel(f"vpstream {__version__}"))
        where = QLabel(f"Log: {self.log_path}")
        where.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bar.addPermanentWidget(where)
        open_log = QPushButton("Open log")
        open_log.clicked.connect(self._open_log)
        bar.addPermanentWidget(open_log)

    def _build_choose(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        self.setup = QGroupBox("Programs vpstream needs")
        setup = QVBoxLayout(self.setup)
        intro = note()
        intro.setText(model.SETUP_INTRO)
        setup.addWidget(intro)
        self.setup_list = QFormLayout()
        setup.addLayout(self.setup_list)
        row = QHBoxLayout()
        row.addStretch(1)
        self.install_button = QPushButton("Install")
        self.install_button.clicked.connect(self._install)
        row.addWidget(self.install_button)
        setup.addLayout(row)
        self.setup.hide()
        box.addWidget(self.setup)

        pick = QGroupBox("What to stream")
        form_box = QVBoxLayout(pick)
        row = QHBoxLayout()
        pick_file = QPushButton("Video file...")
        pick_file.clicked.connect(lambda: self._pick(folder=False))
        pick_folder = QPushButton("Folder...")
        pick_folder.clicked.connect(lambda: self._pick(folder=True))
        self.path = path_line()
        row.addWidget(pick_file)
        row.addWidget(pick_folder)
        row.addWidget(self.path, 1)
        form_box.addLayout(row)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        self.alias = QLineEdit()
        self.alias.setMinimumWidth(240)
        form.addRow("Name in the address (optional)", self.alias)
        self.mode = QComboBox()
        set_choices(self.mode, [(text, key) for key, text in model.MODES.items()])
        self.mode.activated.connect(lambda _at: self._mode_changed())
        form.addRow("Watch it with", self.mode)
        self.policy = QComboBox()
        set_choices(self.policy, [(text, text) for text in model.POLICIES.values()])
        self.policy.setCurrentText(model.default_policy(model.DIRECT))
        form.addRow("Who may watch", self.policy)
        address = QHBoxLayout()
        self.address = QComboBox()
        self.address.setMinimumContentsLength(40)
        self.address.addItem(model.LOOKING)
        self.address.activated.connect(lambda _at: self._address_chosen())
        look = QPushButton("Look again")
        look.clicked.connect(self._look)
        address.addWidget(self.address)
        address.addWidget(look)
        address.addStretch(1)
        form.addRow("This PC's address", address)
        form_box.addLayout(form)
        box.addWidget(pick)

        self.start_panel = browser.StartPanel(self)
        self.start_panel.hide()
        box.addWidget(self.start_panel)
        row = QHBoxLayout()
        row.addStretch(1)
        self.start_button = QPushButton("Start")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self._start)
        row.addWidget(self.start_button)
        box.addLayout(row)
        box.addStretch(1)
        return page

    def _build_live(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        live = self.live = QGroupBox("Streaming")
        outer.addWidget(live)
        box = QVBoxLayout(live)
        row = QHBoxLayout()
        self.url = QLineEdit()
        self.url.setReadOnly(True)
        big = QFont(self.url.font())
        big.setPointSizeF(big.pointSizeF() * 1.6)
        self.url.setFont(big)
        copy = QPushButton("Copy")
        copy.clicked.connect(self._copy)
        row.addWidget(self.url, 1)
        row.addWidget(copy)
        box.addLayout(row)
        self.lines = note()
        box.addWidget(self.lines)
        heads = ("Device", "Address", "What", "State", "Playing", "Sent", "Last seen")
        self.devices, self.device_rows = widgets.table(heads, 4)
        for column, width in enumerate((150, 110, 120, 160, 220, 80, 90)):
            self.devices.setColumnWidth(column, width)
        self.devices.setMinimumHeight(200)
        self.devices.selectionModel().selectionChanged.connect(lambda *_: self._selection())
        box.addWidget(self.devices, 1)
        row = QHBoxLayout()
        for text, then in (
            ("Let in", lambda: self._device(True)),
            ("Keep out", lambda: self._device(False)),
            ("Rename...", self._rename),
        ):
            button = QPushButton(text)
            button.clicked.connect(then)
            row.addWidget(button)
        row.addStretch(1)
        self.new_alias = QLineEdit()
        self.new_alias.setMaximumWidth(180)
        row.addWidget(self.new_alias)
        change = QPushButton("Change name")
        change.clicked.connect(self._change_alias)
        row.addWidget(change)
        stop = QPushButton("Stop")
        stop.clicked.connect(self._stop)
        row.addWidget(stop)
        box.addLayout(row)
        self.players = browser.PlayerPanel(self)
        self.players.hide()
        box.addWidget(self.players)
        return page

    # --- talking to the person

    def say(self, text: str) -> None:
        self.said.setText(text)
        self.said.setToolTip(text)

    def ask(self, text: str, yes: Callable[[], None], no: Callable[[], None] | None = None) -> None:
        """A question with Yes and No that does not wait: `yes` or `no` runs once it is
        answered. A drive answers it through `answer` instead."""
        if self.answer is not None:
            then = yes if self.answer(text, True) else no
            if then is not None:
                self.bridge.post(then)
            return
        box = QMessageBox(QMessageBox.Icon.Question, "vpstream", text, parent=self)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        def answered(button: int) -> None:
            then = yes if button == QMessageBox.StandardButton.Yes.value else no
            if then is not None:
                then()

        box.finished.connect(answered)
        box.open()

    def tell(self, text: str, error: bool = False) -> None:
        """A note or an error, in a box that does not wait."""
        if self.answer is not None:
            self.answer(text, False)
            return
        icon = QMessageBox.Icon.Warning if error else QMessageBox.Icon.Information
        box = QMessageBox(icon, "vpstream", text, parent=self)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.open()

    def failed(self, exc: Exception) -> None:
        self.say("")
        self.tell(str(exc), error=True)

    # --- what is there

    def begin(self) -> None:
        """Once the window shows: the session (which may take up a stream a crash left),
        the poll, what is missing, the networks, the firewall."""
        self.say(model.STARTING)
        threading.Thread(target=self._poll, name="poll", daemon=True).start()

        def work() -> None:
            self.open_begun.set()
            try:
                self.session.open()
            finally:
                self.opened.set()

        self.worker.do(work, lambda _: self._opened(), self.failed)

    def _poll(self) -> None:
        while not self.polling.is_set():
            try:
                found = self.session.control.status()
            except Exception as exc:
                self.session.log(f"WARNING: status: {exc!r}")
            else:
                self.bridge.post(functools.partial(self.show_status, found))
            self.polling.wait(POLL_SECONDS)

    def _opened(self) -> None:
        if self.session.notice:
            self.notice.setText(self.session.notice)
            self.notice.show()
        self.worker.do(lambda: install.missing(self.runner), self._offer_setup)
        self._look()
        self._check_firewall()
        self.say(model.READY)

    def _check_firewall(self) -> None:
        def work() -> list[str]:
            caddy = self.runner.which("caddy")
            return firewall.blocking_rules(self.runner, caddy) if caddy else []

        self.worker.do(work, self._blocking)

    def _offer_setup(self, missing: list[str]) -> None:
        if not missing:
            return
        while self.setup_list.rowCount():
            self.setup_list.removeRow(0)
        self.setup_rows = {}
        for name in missing:
            label = QLabel(model.setup_line(name))
            label.setMinimumWidth(360)
            bar = widgets.Bar()
            self.setup_list.addRow(label, bar)
            self.setup_rows[name] = (label, bar)
        self.install_button.setEnabled(True)
        self.setup.show()
        self.setup_missing = True
        self.update_start()

    def _install(self) -> None:
        self.install_button.setEnabled(False)
        names = list(self.setup_rows)

        def progress(name: str) -> Callable[[str, int, int], None]:
            def show(what: str, done: int, total: int) -> None:
                self.bridge.post(functools.partial(self._progress, name, what, done, total))

            return show

        def work() -> tuple[list[str], list[str]]:
            problems = []
            for name in names:
                try:
                    if name == install.MKVTOOLNIX:
                        done = install.install_mkvtoolnix(
                            self.runner, UrlHttp(), tools_dir(), progress(name)
                        )
                    else:
                        build = install.BUILDS[name]
                        done = install.install_build(
                            build, tools_dir(), self.runner, UrlHttp(), progress(name)
                        )
                    self.session.log(done)
                    progress(name)(model.INSTALLED, 1, 1)
                except Exception as exc:
                    problems.append(f"{name}: {exc}")
                    progress(name)(model.failed_line(exc), 0, 1)
            return problems, install.missing(self.runner)

        self.worker.do(work, self._installed, self.failed)

    def _progress(self, name: str, what: str, done: int, total: int) -> None:
        label, bar = self.setup_rows[name]
        label.setText(model.setup_line(name, what))
        bar.set(model.setup_target(what, done, total))

    def _installed(self, found: tuple[list[str], list[str]]) -> None:
        problems, missing = found
        if problems:
            self.tell("\n\n".join(problems), error=True)
        if [n for n in missing if n != install.MKVTOOLNIX]:
            self._offer_setup(missing)
            return
        self.setup.hide()
        self.setup_missing = False
        self.update_start()
        self._check_firewall()
        self.say(model.ALL_INSTALLED)

    def _look(self) -> None:
        def work() -> tuple[list[network.Address], str]:
            try:
                automatic = local.find("")
            except local.LocalError:
                automatic = ""
            if automatic and not network.in_use(automatic):
                automatic = ""  # a turned-off Wi-Fi's
            return network.addresses(self.runner), automatic

        self.worker.do(work, self._networks, self.failed)

    def _networks(self, found: tuple[list[network.Address], str]) -> None:
        self.addresses, automatic = found
        self.choices = model.address_choices(self.addresses, automatic)
        labels = [c.label for c in self.choices]
        shown = model.chosen_label(self.choices, self.session.config.local_address)
        if shown not in labels:
            labels.append(shown)
        set_choices(self.address, [(label, label) for label in labels], keep=False)
        self.address.setCurrentText(shown)
        self._warnings()

    def _address_chosen(self) -> None:
        value = model.address_of(self.choices, self.address.currentText())
        if value is None or value == self.session.config.local_address:
            return
        self.worker.do(lambda: self.session.choose_address(value), lambda _: self._warnings())

    def _blocking(self, names: list[str]) -> None:
        self.blocking = names
        self._warnings()

    def _warnings(self) -> None:
        """The warnings, once the worker has looked which address streams (in local mode
        that reads the network adapters, which can take a while)."""
        self.worker.do(self.session.control.host, self._show_warnings)

    def _show_warnings(self, host: str) -> None:
        while self.warn_box.count():
            item = self.warn_box.takeAt(0)
            gone = item.widget() if item is not None else None
            if gone is not None:
                gone.deleteLater()
        address = model.network_of(self.addresses, host)
        for found in model.warnings(address, self.blocking, self.other_admin):
            row = QWidget()
            row.setObjectName("warning")
            line = QHBoxLayout(row)
            text = note()
            text.setText(found.text)
            line.addWidget(text, 1)
            if found.button == model.MAKE_PRIVATE and address is not None:
                button = QPushButton(found.button)
                button.clicked.connect(functools.partial(self._make_private, address.index))
                line.addWidget(button)
            elif found.button == model.UNBLOCK:
                button = QPushButton(found.button)
                button.clicked.connect(self._unblock)
                line.addWidget(button)
            self.warn_box.addWidget(row)

    def _make_private(self, index: int) -> None:
        self.worker.do(
            lambda: network.make_private(self.runner, index), lambda _: self._look(), self.failed
        )

    def _unblock(self) -> None:
        def work() -> None:
            firewall.remove_blocking(self.runner, self.runner.which("caddy") or "")

        self.worker.do(work, lambda _: self._blocking([]), self.failed)

    # --- starting and stopping

    def _pick(self, folder: bool) -> None:
        title = model.PICK_FOLDER if folder else model.PICK_VIDEO
        kind = "dir" if folder else "file"
        widgets.pick(self, title, folder, lambda path: self.chosen(path, kind))

    def chosen(self, path: str, kind: str) -> None:
        self.path.setText(os.path.normpath(path))
        self.path.setCursorPosition(0)
        self.kind = kind
        if self.browser_mode():
            self.start_panel.check()
        self.update_start()

    def browser_mode(self) -> bool:
        return bool(self.mode.currentData() == model.BROWSER)

    def _mode_changed(self) -> None:
        mode = model.BROWSER if self.browser_mode() else model.DIRECT
        self.policy.setCurrentText(model.default_policy(mode))
        self.start_panel.setVisible(mode == model.BROWSER)
        if mode == model.BROWSER:
            self.start_panel.check()
        self.update_start()

    def update_start(self) -> None:
        """Start can go when a video is chosen, the programs are there, nothing is starting,
        and in browser mode once everything it plays is prepared."""
        ready = bool(self.path.text()) and not self.setup_missing and not self.starting
        if self.browser_mode():
            ready = ready and self.start_panel.ready
        self.start_button.setEnabled(ready)
        self.live.setEnabled(not self.starting)  # no Stop or Let in while one starts

    def _start(self) -> None:
        mode = model.BROWSER if self.browser_mode() else model.DIRECT
        try:
            options = model.start_options(
                self.path.text(), self.kind, self.alias.text(), mode, self.policy.currentText()
            )
        except ValueError as exc:
            self.tell(str(exc))
            return
        if mode == model.BROWSER:
            options = model.browser_start(
                options, self.start_panel.audio_number(), self.start_panel.sub_number()
            )
        self.starting = True
        self.update_start()
        self.say(model.STARTING_STREAM)

        def started(found: dict[str, Any]) -> None:
            self.starting = False
            self.update_start()
            self.say(model.STREAM_ON)
            self.show_status(found)

        def failed(exc: Exception) -> None:
            self.starting = False
            self.update_start()
            self.failed(exc)

        self.worker.do(lambda: self.session.start(options), started, failed)

    def stream_prepared(self, source: str) -> None:
        """From the Prepare tab: stream a prepared file in browser mode. In place of a
        running stream it keeps its address and policy, so a TV only reloads the page."""
        if self.starting:
            self.say(model.ONE_AT_A_TIME)
            return
        running = (self.status or {}).get("stream")
        if running:
            question = model.ask_switch(model.streaming_what(running), source)
            self.ask(question, functools.partial(self._switch, source))
        else:
            self._switch(source)

    def _switch(self, source: str) -> None:
        """The stream as it is now (it may have stopped while the question was open)."""
        if self.starting:
            self.say(model.ONE_AT_A_TIME)
            return
        running = (self.status or {}).get("stream")
        options = model.switch_options(running, source)
        self.tabs.setCurrentWidget(self.stream_page)
        self.say(model.STARTING_STREAM)
        self.starting = True
        self.stopping_on_purpose = bool(running)
        self.update_start()

        stopped: list[bool] = []  # written there before the answer is posted, read here

        def work() -> dict[str, Any]:
            if running:
                self.session.stop()
                stopped.append(True)
            return self.session.start(options)

        def started(found: dict[str, Any]) -> None:
            self.starting = self.stopping_on_purpose = False
            self.update_start()
            self.say(model.STREAM_ON)
            self.show_status(found)

        def failed(exc: Exception) -> None:
            self.starting = False
            if not stopped:  # the old stream still runs; else the next status takes it
                self.stopping_on_purpose = False
            self.update_start()
            self.failed(exc)

        self.worker.do(work, started, failed)

    def _stop(self) -> None:
        self.stopping_on_purpose = True
        self.say(model.STOPPING)

        def stopped(found: dict[str, Any]) -> None:
            self.say(model.STOPPED)
            self.show_status(found)

        def failed(exc: Exception) -> None:
            self.stopping_on_purpose = False  # it still streams
            self.failed(exc)

        self.worker.do(self.session.stop, stopped, failed)

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.url.text())
        self.say(model.COPIED)

    def _selected(self) -> list[str]:
        chosen = widgets.selected(self.devices)
        if not chosen:
            self.tell(model.PICK_DEVICE)
        return chosen

    def _device(self, allowed: bool) -> None:
        chosen = self._selected()
        if chosen:
            device = chosen[0]
            self.worker.do(
                lambda: self.session.control.set_device(device, allowed),
                self.show_status,
                self.failed,
            )

    def _rename(self) -> None:
        chosen = self._selected()
        if not chosen:
            return
        dialog = QInputDialog(self)
        dialog.setWindowTitle("vpstream")
        dialog.setLabelText(model.RENAME)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.textValueSelected.connect(functools.partial(self.rename, chosen[0]))
        dialog.open()

    def rename(self, device: str, name: str) -> None:
        options = {"devices": [device], "name": name.strip()}
        self.worker.do(
            lambda: self.session.control.set_clients(options), self.show_status, self.failed
        )

    def _change_alias(self) -> None:
        alias = self.new_alias.text().strip()
        if alias:
            self.worker.do(
                lambda: self.session.control.set_alias(alias), self.show_status, self.failed
            )

    # --- what the stream is doing

    def show_status(self, status: dict[str, Any] | None) -> None:
        if status is None or self.closing:
            return
        self.status = status
        self.prepare_tab.show_status(status)
        running = status.get("stream")
        if running is None:
            if self.starting and self.stopping_on_purpose:
                return  # a switch, between its stop and its start: the page stays
            if self.was_streaming:
                self.pages.setCurrentIndex(0)
                why = self.session.stop_reason()
                if not self.stopping_on_purpose and why:
                    self.say(model.stopped_said(why))
                self._awake(False)
            self.was_streaming = self.stopping_on_purpose = False
            self.shown = None
            return
        if not self.was_streaming:
            self.pages.setCurrentIndex(1)
            self._awake(True)
        self.was_streaming = True
        if self.shown != (running["url"], running["mode"]):  # a new stream (a switch too)
            self.shown = (running["url"], running["mode"])
            self.new_alias.setText(running["alias"])
            self.players.setVisible(running["mode"] == model.BROWSER)
        if self.url.text() != running["url"]:
            self.url.setText(running["url"])
        lines = "\n".join(model.stream_lines(status))
        if self.lines.text() != lines:
            self.lines.setText(lines)
        rows = model.device_rows(running, status.get("now"))
        widgets.fill(self.devices, self.device_rows, [(row.id, row.cells) for row in rows])
        if running["mode"] == model.BROWSER:
            self.players.show_status(status, widgets.selected(self.devices))

    def _selection(self) -> None:
        running = (self.status or {}).get("stream")
        if self.status is not None and running and running["mode"] == model.BROWSER:
            self.players.show_status(self.status, widgets.selected(self.devices))

    def _awake(self, on: bool) -> None:
        """While a stream runs, a thread of its own asks Windows not to sleep: the request
        belongs to the thread that made it, and ends when that thread lets it go."""
        if on and self.awake is None:
            self.awake = threading.Event()
            threading.Thread(
                target=keep_awake, args=(self.awake,), name="awake", daemon=True
            ).start()
        elif not on and self.awake is not None:
            self.awake.set()
            self.awake = None

    # --- the end

    def _open_log(self) -> None:
        """The log in the program Windows opens .log files with: the one program the window
        starts itself, on purpose with a window of its own. On a thread of its own, since
        ShellExecute can take a moment, and not behind the worker's jobs."""
        path = self.log_path

        def work() -> None:
            try:
                os.startfile(path)  # type: ignore[attr-defined,unused-ignore]
            except OSError as exc:
                self.bridge.post(functools.partial(self.tell, f"{path}: {exc}", True))

        threading.Thread(target=work, name="open-log", daemon=True).start()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Closing asks first while a stream runs or starts; the session then closes off
        this thread, and the window goes once it has."""
        if self.closed:
            event.accept()
            return
        event.ignore()
        if self.closing or self.asking_close:
            return
        streaming = bool(self.status and self.status.get("stream"))
        question = model.close_question(self.starting, streaming)
        if question is None:
            self._close()
            return
        self.asking_close = True

        def no() -> None:
            self.asking_close = False

        self.ask(question, self._close, no)

    def _close(self) -> None:
        self.closing = True
        self.say(model.CLOSING)
        self.central.setEnabled(False)
        self.polling.set()
        self._awake(False)

        self.worker.stop()

        def end() -> None:
            # The job under way (a start, say) ends first, unless it takes long (a
            # download); the session's open, if it began, too (it starts the gate).
            self.worker.thread.join(CLOSE_WAIT)
            if self.open_begun.is_set() and not self.opened.wait(OPEN_WAIT):
                self.session.log("WARNING: the session still opens; it is closed all the same")
            closing = threading.Thread(target=close_session, name="close-session", daemon=True)
            closing.start()
            closing.join(SESSION_CLOSE_WAIT)
            if closing.is_alive():
                self.session.log("WARNING: the session did not close in time; the window ends")
            self.bridge.post(self._closed)

        def close_session() -> None:
            try:
                self.session.close()
            except Exception as exc:
                self.session.log(f"WARNING: closing: {exc!r}")

        threading.Thread(target=end, name="close", daemon=True).start()

    def _closed(self) -> None:
        self.closed = True
        self.close()
        QApplication.quit()


def keep_awake(until: threading.Event) -> None:
    """The keep-awake thread: Windows stays awake until `until` is set."""
    processes.keep_awake(True)
    until.wait()
    processes.keep_awake(False)


STYLE = """
QGroupBox { font-weight: 600; margin-top: 14px; padding: 10px 8px 8px 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
QGroupBox > QWidget { font-weight: normal; }
QPushButton { padding: 5px 14px; }
QPushButton#primary { font-weight: 600; padding: 6px 22px; }
QLabel#notice { color: #d0342c; }
QStatusBar QLabel { padding: 0 6px; }
QTableView { selection-background-color: palette(highlight);
             selection-color: palette(highlighted-text); }
QWidget#warning { border: 1px solid #c88a1d; border-radius: 6px; }
"""


def application() -> QApplication:
    """The one QApplication: Fusion (it follows Windows' dark or light setting on Windows
    10 too), and the taskbar names vpstream rather than Python. No command line reaches Qt."""
    found = QApplication.instance()
    if isinstance(found, QApplication):
        return found
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    QApplication.setApplicationName("vpstream")
    QApplication.setStyle("Fusion")
    app = QApplication([sys.argv[0]])
    app.setStyleSheet(STYLE)
    return app


def run(
    session: Session,
    runner: Runner,
    log_path: Path,
    other_admin: str = "",
    answer: Answer | None = None,
    then: Callable[[Window], None] | None = None,
) -> int:
    """Open the window and wait until it is closed. `answer` and `then` are for drives
    only: `answer` answers every question (and hears every note), `then` gets the window
    once it shows."""
    app = application()
    window = Window(session, runner, log_path, other_admin, answer)

    def bug(kind: Any, value: BaseException, trace: Any) -> None:
        lines = "".join(traceback.format_exception(kind, value, trace))
        session.log(f"WARNING: the window: {lines}")
        window.tell(model.bug_said(value), error=True)

    before, sys.excepthook = sys.excepthook, bug
    window.show()
    window.bridge.post(window.begin)
    if then is not None:
        window.bridge.post(functools.partial(then, window))
    try:
        app.exec()
    finally:
        sys.excepthook = before
    return 0
