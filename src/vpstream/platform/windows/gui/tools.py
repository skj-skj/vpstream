"""The Tools tab: which ffmpeg, Caddy and MKVToolNix vpstream uses and where they are,
whether a newer release is out, and vpstream's own copies: install one, update it, remove
it (the one installed for the whole system is used after).

What runs the programs, reads the disk or installs goes through the window's worker; the
look for new releases (the network) runs on a thread of its own, so it never holds up a
Start. What the tab says and offers is `model.tool_row`'s.
"""

from __future__ import annotations

import contextlib
import functools
import threading
from typing import TYPE_CHECKING, Any

from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from vpstream.platform import windows
from vpstream.platform.common import SetupError
from vpstream.platform.windows import install, tools_dir
from vpstream.platform.windows.gui import model, widgets
from vpstream.platform.windows.gui.browser import keep_room, note, path_line

if TYPE_CHECKING:
    from vpstream.platform.windows.gui.app import Window


class ToolLine:
    """One program's widgets in the grid."""

    def __init__(self, grid: QGridLayout, at: int, title: str) -> None:
        name = QLabel(title)
        name.setStyleSheet("font-weight: 600")
        self.where = path_line()
        self.version = QLabel()
        self.latest = QLabel()
        self.said = QLabel()
        self.bar = widgets.Bar()
        self.action = QPushButton(model.INSTALL_OWN)
        self.remove = QPushButton("Remove")
        for widget in (self.bar, self.said, self.action, self.remove):
            keep_room(widget)
        self.bar.hide()
        row = at * 2 + 1
        grid.addWidget(name, row, 0)
        grid.addWidget(self.where, row, 1)
        grid.addWidget(self.version, row, 2)
        grid.addWidget(self.latest, row, 3)
        grid.addWidget(self.action, row, 4)
        grid.addWidget(self.remove, row, 5)
        under = QHBoxLayout()
        under.addWidget(self.said, 1)
        under.addWidget(self.bar, 1)
        grid.addLayout(under, row + 1, 1, 1, 3)


class ToolsTab(QWidget):
    def __init__(self, main: Window) -> None:
        super().__init__()
        self.main = main
        self.found: dict[str, install.Found] = {}
        self.latest: dict[str, str] = {}
        self.problems: dict[str, str] = {}
        self.checking = False
        self.busy = ""  # the program being installed or removed
        self.looked = False  # looked for new releases once, when the tab first showed
        self.looking = False  # a look at what is there is queued
        page = QVBoxLayout(self)
        intro = note()
        intro.setText(model.TOOLS_INTRO)
        page.addWidget(intro)
        box = QGroupBox("Programs vpstream uses")
        grid = QGridLayout(box)
        for column, head in enumerate(("Program", "Used from", "Version", "Latest release")):
            label = QLabel(head)
            label.setStyleSheet("font-weight: 600")
            grid.addWidget(label, 0, column)
        grid.setColumnStretch(1, 1)
        self.lines: dict[str, ToolLine] = {}
        for at, tool in enumerate(install.TOOLS.values()):
            line = ToolLine(grid, at, tool.title)
            line.action.clicked.connect(functools.partial(self.get_copy, tool.name))
            line.remove.clicked.connect(functools.partial(self.remove_copy, tool.name))
            self.lines[tool.name] = line
        page.addWidget(box)
        row = QHBoxLayout()
        row.addStretch(1)
        self.check_button = QPushButton("Look for new releases")
        self.check_button.clicked.connect(self.check)
        row.addWidget(self.check_button)
        page.addLayout(row)
        page.addStretch(1)
        self.show_rows()

    # --- what is there

    def shown(self) -> None:
        """The tab came to the front: look again; for new releases only the first time."""
        self.look()
        if not self.looked:
            self.looked = True
            self.check()

    def look(self) -> None:
        if self.looking:
            return  # one is queued already: tabs switched quickly do not pile them up
        self.looking = True
        runner = self.main.runner
        self.main.worker.do(lambda: install.found(runner, tools_dir()), self._found, self._lost)

    def _found(self, rows: list[install.Found]) -> None:
        self.looking = False
        self.found = {row.tool.name: row for row in rows}
        self.show_rows()

    def _lost(self, exc: Exception) -> None:
        self.looking = False  # shown again, the tab looks again
        self.main.failed(exc)

    def check(self) -> None:
        """Each program's latest release, from its release page (the network): on a
        thread of its own."""
        if self.checking:
            return
        self.checking = True
        self.show_rows()

        def work() -> None:
            latest: dict[str, str] = {}
            problems: dict[str, str] = {}
            try:
                http = windows.http()
                for tool in install.TOOLS.values():
                    try:
                        latest[tool.name] = install.latest(tool, http).version
                    except SetupError as exc:
                        problems[tool.name] = str(exc)
            except Exception as exc:  # whatever it is, the tab hears the look ended
                for tool in install.TOOLS.values():
                    problems.setdefault(tool.name, repr(exc))
            finally:
                with contextlib.suppress(RuntimeError):  # the window is gone
                    self.main.bridge.post(functools.partial(self._checked, latest, problems))

        threading.Thread(target=work, name="releases", daemon=True).start()

    def _checked(self, latest: dict[str, str], problems: dict[str, str]) -> None:
        self.checking = False
        self.latest, self.problems = latest, problems
        self.show_rows()

    def rows(self) -> list[model.ToolRow]:
        found = []
        for tool in install.TOOLS.values():
            row = self.found.get(tool.name)
            found.append(
                model.tool_row(
                    tool.name,
                    tool.title,
                    row.path if row else None,
                    bool(row and row.own),
                    row.version if row else "",
                    row.own_version if row else None,
                    self.latest.get(tool.name),
                    self.problems.get(tool.name, ""),
                    self.checking,
                )
            )
        return found

    def show_rows(self) -> None:
        for row in self.rows():
            line = self.lines[row.name]
            line.where.setText(row.where if self.found else model.LOOKING)
            line.where.setCursorPosition(0)
            line.version.setText(row.version if self.found else "")
            line.latest.setText(row.latest)
            line.latest.setToolTip(row.latest)
            line.action.setText(row.action or model.UPDATE)
            line.action.setVisible(bool(row.action))
            line.action.setEnabled(not self.busy and bool(self.found))
            line.remove.setVisible(row.can_remove)
            line.remove.setEnabled(not self.busy)
        self.check_button.setEnabled(not self.checking)

    # --- installing, updating and removing vpstream's copies

    def get_copy(self, name: str) -> None:
        """The latest release (looked up again now), then a question, then the download."""
        if self.busy:
            return
        tool = install.TOOLS[name]
        self._busy(name, model.CHECKING)

        def asked(build: install.Build) -> None:
            row = self.found.get(name)
            question = model.ask_install(
                tool.title, build.version, build.size, row.path if row else None,
                bool(row and row.own),
            )  # fmt: skip
            self.main.ask(question, functools.partial(self._download, build), self._done)

        self.main.worker.do(lambda: install.latest(tool, windows.http()), asked, self._failed)

    def _download(self, build: install.Build) -> None:
        name = build.tool.name

        def progress(what: str, done: int, total: int) -> None:
            self.main.bridge.post(functools.partial(self._progress, name, what, done, total))

        runner, log = self.main.runner, self.main.session.log

        def work() -> str:
            said = install.install_build(build, tools_dir(), runner, windows.http(), progress)
            log(said)
            return said

        self.main.worker.do(work, self._done, self._failed)

    def _progress(self, name: str, what: str, done: int, total: int) -> None:
        line = self.lines[name]
        line.said.setText(what)
        line.bar.set(model.setup_target(what, done, total))

    def remove_copy(self, name: str) -> None:
        if self.busy:
            return
        tool = install.TOOLS[name]

        log = self.main.session.log

        def work() -> str:
            said = model.tool_removed(tool.title, install.remove_own(tools_dir(), tool))
            log(said)
            return said

        def go() -> None:
            self._busy(name, "")
            self.main.worker.do(work, self._done, self._failed)

        self.main.ask(model.ask_remove_tool(tool.title), go)

    def _busy(self, name: str, said: str) -> None:
        self.busy = name
        line = self.lines[name]
        line.said.setText(said)
        line.said.show()
        line.bar.set(None)
        line.bar.show()
        self.show_rows()

    def _done(self, said: Any = None) -> None:
        if self.busy:
            line = self.lines[self.busy]
            line.bar.hide()
            line.said.setText("")
        self.busy = ""
        if said:
            self.main.say(str(said))
        self.look()
        self.main.tools_changed()

    def _failed(self, exc: Exception) -> None:
        self._done()
        self.main.failed(exc)
