"""Browser mode in the window.

The Prepare tab (`PrepareTab`) makes the versions a browser plays, for any file or folder,
one after another in one queue, while a stream runs or not; below the queue are the videos
already prepared, to stream or remove. On the Stream tab, `StartPanel` says whether the
chosen video is ready and what it starts with, and `PlayerPanel` sends the players of the
browsers watching their audio, subtitles and a time to go to. All of them ask through the
window's worker and show what its poll brings; none of them waits.
"""

from __future__ import annotations

import functools
import ntpath
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from vpstream.platform.windows.gui import model, widgets
from vpstream.stream import NeedsConfirm

if TYPE_CHECKING:
    from vpstream.platform.windows.gui.app import Window

LIBRARY_SECONDS = 5.0  # how often the list of prepared videos is read while it shows


def path_line() -> QLineEdit:
    """A chosen path: read-only, scrolled inside itself, so a long one never widens the
    window."""
    line = QLineEdit()
    line.setReadOnly(True)
    line.setFrame(False)
    line.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
    return line


def note() -> QLabel:
    """A label of a few lines that wraps."""
    label = QLabel()
    label.setWordWrap(True)
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    return label


def keep_room(widget: QWidget) -> None:
    """Hidden, `widget` keeps its room, so what is next to it does not move."""
    policy = widget.sizePolicy()
    policy.setRetainSizeWhenHidden(True)
    widget.setSizePolicy(policy)


def set_choices(box: QComboBox, choices: list[tuple[str, Any]], keep: bool = True) -> None:
    """The (label, value) choices of `box`; the chosen one stays when it is still there."""
    was = box.currentData() if keep else None
    box.blockSignals(True)
    box.clear()
    for label, value in choices:
        box.addItem(label, value)
    at = box.findData(was) if was is not None else -1
    box.setCurrentIndex(max(at, 0))
    box.blockSignals(False)


class JobRow(QWidget):
    """One video in the queue: what it does, its bar, and Cancel while it has parts left."""

    def __init__(self, cancel: Any) -> None:
        super().__init__()
        line = QHBoxLayout(self)
        line.setContentsMargins(0, 2, 0, 2)
        self.label = QLabel()
        self.label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.bar = widgets.Bar()
        self.cancel = QPushButton("Cancel")
        self.cancel.clicked.connect(cancel)
        keep_room(self.cancel)
        line.addWidget(self.label, 3)
        line.addWidget(self.bar, 2, Qt.AlignmentFlag.AlignVCenter)
        line.addWidget(self.cancel)

    def show_job(self, job: model.VideoJob) -> None:
        if self.label.text() != job.line:
            self.label.setText(job.line)
            self.label.setToolTip(job.line)
        self.bar.set(model.bar_target(job.state, job.progress))
        self.cancel.setVisible(job.state in ("queued", "running"))


class PrepareTab(QWidget):
    """Pick a video and its subtitles, prepare it; the queue; the prepared videos."""

    def __init__(self, main: Window) -> None:
        super().__init__()
        self.main = main
        self.kind = "file"
        self.plans: list[dict[str, Any]] = []
        self.source = ""
        self.checks: dict[int, QCheckBox] = {}
        self.job_rows: dict[int, JobRow] = {}
        self.active = 0
        self.library: list[model.LibraryRow] = []
        self.library_raw: list[dict[str, Any]] = []
        self.read_at = 0.0
        page = QVBoxLayout(self)

        what = QGroupBox("Prepare a video for browsers and TVs")
        box = QVBoxLayout(what)
        row = QHBoxLayout()
        pick_file = QPushButton("Video file...")
        pick_file.clicked.connect(lambda: self._pick(folder=False))
        pick_folder = QPushButton("Folder...")
        pick_folder.clicked.connect(lambda: self._pick(folder=True))
        self.path = path_line()
        row.addWidget(pick_file)
        row.addWidget(pick_folder)
        row.addWidget(self.path, 1)
        box.addLayout(row)
        box.addWidget(QLabel("Subtitles to prepare:"))
        self.sub_box = QWidget()
        self.sub_grid = QGridLayout(self.sub_box)
        self.sub_grid.setContentsMargins(16, 0, 0, 0)
        box.addWidget(self.sub_box)
        self.left = note()
        box.addWidget(self.left)
        row = QHBoxLayout()
        row.addStretch(1)
        self.prepare_button = QPushButton("Prepare")
        self.prepare_button.setEnabled(False)
        self.prepare_button.clicked.connect(lambda: self.prepare())
        row.addWidget(self.prepare_button)
        box.addLayout(row)
        page.addWidget(what)

        queue = QGroupBox("Queue (one after another, while you watch)")
        box = QVBoxLayout(queue)
        self.jobs = QVBoxLayout()
        box.addLayout(self.jobs)
        self.empty = QLabel(model.NOTHING_QUEUED)
        box.addWidget(self.empty)
        page.addWidget(queue)

        shelf = QGroupBox("Prepared videos")
        box = QVBoxLayout(shelf)
        self.table, self.rows = widgets.table(("Video", "Subtitles", "Size", "State"), 0)
        self.table.setMinimumHeight(180)
        box.addWidget(self.table, 1)
        row = QHBoxLayout()
        hint = note()
        hint.setText(model.SWITCH_HINT)
        row.addWidget(hint, 1)
        remove = QPushButton("Remove")
        remove.clicked.connect(self.remove)
        stream_it = QPushButton("Stream it")
        stream_it.clicked.connect(self.stream_it)
        row.addWidget(remove)
        row.addWidget(stream_it)
        box.addLayout(row)
        page.addWidget(shelf, 1)

    # --- choosing what to prepare

    def _pick(self, folder: bool) -> None:
        title = "A folder of videos" if folder else "A video to prepare"
        widgets.pick(self, title, folder, lambda path: self.load(path, "dir" if folder else "file"))

    def load(self, path: str, kind: str) -> None:
        self.path.setText(ntpath.normpath(path))
        self.path.setCursorPosition(0)
        self.kind = kind
        self.source, self.plans = "", []
        self.plan()

    def chosen_subs(self) -> list[int]:
        return [n for n, box in self.checks.items() if box.isChecked()]

    def plan(self) -> None:
        """What is still to make, for the chosen subtitles (a plan only: nothing starts)."""
        source = self.path.text()
        if not source:
            return
        self.prepare_button.setEnabled(False)
        self.left.setText(model.LOOKING_VIDEO)
        options = {self.kind: source, "sub": model.sub_spec(self.chosen_subs()), "dry_run": True}
        self.main.worker.do(
            lambda: self.main.session.control.prepare(options),
            lambda found: self._planned(source, found),
            self._failed,
        )

    def _planned(self, source: str, found: dict[str, Any]) -> None:
        if source != self.path.text():
            return  # another was chosen meanwhile
        self.plans = found["plans"]
        if source != self.source:
            self.source = source
            self._show_tracks()
        left = model.what_is_left(self.plans)
        self.left.setText(model.left_text(left))
        self.prepare_button.setEnabled(bool(left))

    def _show_tracks(self) -> None:
        _, subs = model.plan_tracks(self.plans)
        while self.sub_grid.count():
            item = self.sub_grid.takeAt(0)
            gone = item.widget() if item is not None else None
            if gone is not None:
                gone.deleteLater()
        self.checks = {}
        rows = max((len(subs) + 1) // 2, 1)  # two columns: a file with many tracks fits
        for at, track in enumerate(subs):
            box = QCheckBox(track.text)
            box.setEnabled(track.how != "unsupported")
            box.toggled.connect(lambda _on: self.plan())
            self.checks[track.number] = box
            self.sub_grid.addWidget(box, at % rows, at // rows)
        if not subs:
            self.sub_grid.addWidget(QLabel(model.NO_SUBS), 0, 0)

    def prepare(self, confirmed: bool = False) -> None:
        options: dict[str, Any] = {
            self.kind: self.path.text(),
            "sub": model.sub_spec(self.chosen_subs()),
        }
        if confirmed:
            options["confirmed"] = True
        self.prepare_button.setEnabled(False)

        def queued(found: dict[str, Any]) -> None:
            self.main.say(model.queued_said(len(found["jobs"])))
            self.plan()

        self.main.worker.do(
            lambda: self.main.session.control.prepare(options), queued, self._prepare_failed
        )

    def _prepare_failed(self, exc: Exception) -> None:
        if isinstance(exc, NeedsConfirm):
            self.main.ask(
                model.ask_long(str(exc)),
                lambda: self.prepare(confirmed=True),
                lambda: self.prepare_button.setEnabled(True),
            )
            return
        self._failed(exc)

    def _failed(self, exc: Exception) -> None:
        self.left.setText(str(exc))
        self.prepare_button.setEnabled(True)
        self.main.failed(exc)

    # --- the queue

    def show_status(self, status: dict[str, Any]) -> None:
        jobs = model.video_jobs(status.get("prepare") or [])
        keys = [job.key for job in jobs]
        for key in [k for k in self.job_rows if k not in keys]:
            gone = self.job_rows.pop(key)
            self.jobs.removeWidget(gone)
            gone.deleteLater()
        for at, job in enumerate(jobs):
            row = self.job_rows.get(job.key)
            if row is None:
                row = self.job_rows[job.key] = JobRow(functools.partial(self.cancel, job.source))
                self.jobs.insertWidget(at, row)
            elif self.jobs.indexOf(row) != at:
                self.jobs.removeWidget(row)
                self.jobs.insertWidget(at, row)
            row.show_job(job)
        self.empty.setVisible(not jobs)
        active = sum(1 for j in jobs if j.state in ("queued", "running"))
        ended = active < self.active
        self.active = active
        if ended:
            self.main.say(model.prepared_said(active))
            self.plan()
        now = float(status.get("now") or 0)
        if ended or now - self.read_at >= LIBRARY_SECONDS:
            self.read_at = now
            self.refresh_library()

    def cancel(self, source: str) -> None:
        self.main.worker.do(
            lambda: self.main.session.control.cancel_prepare({"path": source}),
            lambda found: self.main.say(model.cancelled_said(found["cancelled"])),
            self.main.failed,
        )

    # --- the prepared videos

    def refresh_library(self) -> None:
        self.main.worker.do(self.main.session.control.prepared, self._library)

    def _library(self, rows: list[dict[str, Any]]) -> None:
        self.library_raw = rows
        self.library = model.library_rows(rows)
        widgets.fill(self.table, self.rows, [(r.key, r.cells) for r in self.library])
        self.main.start_panel.library_changed()

    def _selected(self) -> model.LibraryRow | None:
        chosen = widgets.selected(self.table)
        row = next((r for r in self.library if chosen and r.key == chosen[0]), None)
        if row is None:
            self.main.tell(model.PICK_PREPARED)
        return row

    def stream_it(self) -> None:
        row = self._selected()
        if row is None:
            return
        if not row.ready:
            self.main.tell(model.not_ready(row))
            return
        self.main.stream_prepared(row.source)

    def remove(self) -> None:
        row = self._selected()
        if row is not None:
            self.main.ask(model.ask_remove(row), functools.partial(self._remove, row))

    def _remove(self, row: model.LibraryRow) -> None:
        def removed(found: dict[str, Any]) -> None:
            if found["kept"]:
                self.main.say(model.KEPT)
            self.refresh_library()

        self.main.worker.do(
            lambda: self.main.session.control.remove_prepared({"path": row.source}),
            removed,
            self.main.failed,
        )


class StartPanel(QGroupBox):
    """On the Stream tab, in browser mode: whether the chosen video is prepared, and the
    audio and subtitles it starts with. `ready` says whether Start can go."""

    def __init__(self, main: Window) -> None:
        super().__init__("For browsers and TVs")
        self.main = main
        self.ready = False
        self.tracks: tuple[list[model.Track], list[model.Track]] = ([], [])
        box = QVBoxLayout(self)
        self.state = note()
        box.addWidget(self.state)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        self.audio = QComboBox()
        self.sub = QComboBox()
        for combo in (self.audio, self.sub):
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(34)
        form.addRow("Audio it starts with", self.audio)
        form.addRow("Subtitles it starts with", self.sub)
        box.addLayout(form)
        row = QHBoxLayout()
        row.addStretch(1)
        self.to_prepare = QPushButton("Prepare it...")
        self.to_prepare.clicked.connect(self._prepare_it)
        keep_room(self.to_prepare)
        row.addWidget(self.to_prepare)
        box.addLayout(row)
        set_choices(self.sub, [(model.OFF, None)])

    def audio_number(self) -> int | None:
        found = self.audio.currentData()
        return int(found) if found is not None else None

    def sub_number(self) -> int | None:
        found = self.sub.currentData()
        return int(found) if found is not None else None

    def check(self) -> None:
        """Is the chosen video (or every video of the folder) prepared?"""
        path = self.main.path.text()
        self.ready = False
        self.main.update_start()
        if not path:
            return
        self.state.setText(model.LOOKING_PREPARED)
        options = {self.main.kind: path, "sub": "none", "dry_run": True}
        self.main.worker.do(
            lambda: self.main.session.control.prepare(options),
            lambda found: self._checked(path, found),
            self.main.failed,
        )

    def _checked(self, path: str, found: dict[str, Any]) -> None:
        if path != self.main.path.text():
            return
        plans = found["plans"]
        left = model.what_is_left(plans)
        self.ready = not left
        self.tracks = model.plan_tracks(plans)
        set_choices(self.audio, [(t.text, t.number) for t in self.tracks[0]])
        self.library_changed()
        self.state.setText(model.start_state(left))
        self.to_prepare.setVisible(not self.ready)
        self.main.update_start()

    def library_changed(self) -> None:
        """The subtitles a file starts with are those prepared for it (a folder: off)."""
        if self.main.kind != "file":
            set_choices(self.sub, [(model.OFF, None)], keep=False)
            return
        made = model.prepared_subs(self.main.prepare_tab.library_raw, self.main.path.text())
        subs = [(t.text, t.number) for t in self.tracks[1] if t.number in made]
        set_choices(self.sub, [(model.OFF, None), *subs])

    def _prepare_it(self) -> None:
        self.main.prepare_tab.load(self.main.path.text(), self.main.kind)
        self.main.tabs.setCurrentWidget(self.main.prepare_page)


class PlayerPanel(QGroupBox):
    """For the browsers selected in the device list (or all): their audio, subtitles and
    where they are, sent to their players."""

    def __init__(self, main: Window) -> None:
        super().__init__("The players of the selected browsers")
        self.main = main
        box = QVBoxLayout(self)
        self.where = QLabel(model.SELECT_BROWSER)
        box.addWidget(self.where)
        row = QHBoxLayout()
        self.audio = QComboBox()
        self.sub = QComboBox()
        self.audio.setMinimumContentsLength(20)
        self.sub.setMinimumContentsLength(24)
        self.seek = QLineEdit()
        self.seek.setPlaceholderText("12:30")
        self.seek.setMaximumWidth(90)
        for label, widget in (("Audio", self.audio), ("Subtitles", self.sub), ("Go to", self.seek)):
            row.addWidget(QLabel(label))
            row.addWidget(widget)
        row.addStretch(1)
        box.addLayout(row)
        row = QHBoxLayout()
        row.addStretch(1)
        to_selected = QPushButton("Send to selected")
        to_selected.clicked.connect(lambda: self.send(everyone=False))
        to_all = QPushButton("Send to all")
        to_all.clicked.connect(lambda: self.send(everyone=True))
        row.addWidget(to_selected)
        row.addWidget(to_all)
        box.addLayout(row)
        self.shown_for: tuple[str, str] = ("", "")

    def show_status(self, status: dict[str, Any], selected: list[str]) -> None:
        running = status["stream"]
        devices = {d["id"]: d for d in running["devices"]}
        first = devices.get(selected[0]) if selected else None
        where = model.where_it_is(first) if first else model.SELECT_BROWSER
        if self.where.text() != where:
            self.where.setText(where)
        menu = model.menu_for(status, first)
        key = (str(selected[:1]), str(menu))
        if key != self.shown_for:  # only when it changes: a choice being made stays
            self.shown_for = key
            nothing: list[tuple[str, Any]] = [("", None)]
            set_choices(
                self.audio,
                nothing + [(m["label"], str(m["value"])) for m in menu["audio"]],
                keep=False,
            )
            set_choices(
                self.sub,
                nothing + [(m["label"], str(m["value"])) for m in menu["subs"]],
                keep=False,
            )

    def send(self, everyone: bool) -> None:
        devices = None if everyone else widgets.selected(self.main.devices)
        try:
            options = model.send_options(
                devices, self.audio.currentData(), self.sub.currentData(), self.seek.text()
            )
        except ValueError as exc:
            self.main.tell(str(exc))
            return

        def sent(found: dict[str, Any]) -> None:
            self.seek.clear()
            self.main.show_status(found)

        self.main.worker.do(
            lambda: self.main.session.control.set_clients(options), sent, self.main.failed
        )
