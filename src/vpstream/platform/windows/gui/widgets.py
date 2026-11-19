"""What both tabs of the window use: a bar that glides and has a calm busy look, a table
whose rows change where they are, and the file and folder pickers. The motion of the bar is
`model`'s; this only paints it."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QPainter, QPaintEvent, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QAbstractItemView, QFileDialog, QHeaderView, QTableView, QWidget

from vpstream.platform.windows.gui import model

BAR_HEIGHT = 10
KEY = Qt.ItemDataRole.UserRole  # where a row keeps its key (a device id, a library key)


class Bar(QWidget):
    """A progress bar that glides to each value it is given, 25 frames a second, and
    while it is given None shows a piece sliding across at a calm pace. Its timer runs
    only while it moves."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.value = 0.0
        self.target: float | None = 0.0
        self.busy_since = 0.0
        self.timer = QTimer(self)
        self.timer.setInterval(int(model.FRAME_SECONDS * 1000))
        self.timer.timeout.connect(self._frame)
        self.setMinimumWidth(160)
        self.setFixedHeight(BAR_HEIGHT)

    def sizeHint(self) -> QSize:
        return QSize(220, BAR_HEIGHT)

    def minimumSizeHint(self) -> QSize:
        return QSize(160, BAR_HEIGHT)

    def set(self, target: float | None) -> None:
        if target is None and self.target is not None:
            self.busy_since = time.monotonic()
        self.target = target
        if (target is None or target != self.value) and not self.timer.isActive():
            self.timer.start()
        self.update()

    def _frame(self) -> None:
        if self.target is not None:
            self.value = model.glide(self.value, self.target)
            if self.value == self.target:
                self.timer.stop()
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        whole = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = whole.height() / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(palette.mid())
        painter.drawRoundedRect(whole, radius, radius)
        if self.target is None:
            start, end = model.busy_span(time.monotonic() - self.busy_since)
        else:
            start, end = 0.0, self.value
        if end > start:
            part = QRectF(
                whole.left() + whole.width() * start,
                whole.top(),
                max(whole.width() * (end - start), whole.height()),
                whole.height(),
            ).intersected(whole)
            painter.setBrush(palette.highlight())
            painter.drawRoundedRect(part, radius, radius)
        painter.end()


def table(heads: Sequence[str], stretch: int) -> tuple[QTableView, QStandardItemModel]:
    """A read-only table that selects whole rows; column `stretch` takes the room left."""
    found = QStandardItemModel(0, len(heads))
    found.setHorizontalHeaderLabels(list(heads))
    view = QTableView()
    view.setModel(found)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setShowGrid(False)
    view.setWordWrap(False)
    view.verticalHeader().setVisible(False)
    header = view.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
    header.setHighlightSections(False)
    return view, found


def keys_of(found: QStandardItemModel) -> list[str]:
    return [str(found.item(row, 0).data(KEY)) for row in range(found.rowCount())]


def selected(view: QTableView) -> list[str]:
    """The keys of the selected rows, top first."""
    found = view.model()
    rows = sorted(index.row() for index in view.selectionModel().selectedRows())
    return [str(found.index(row, 0).data(KEY)) for row in rows]


def fill(
    view: QTableView, found: QStandardItemModel, rows: Sequence[tuple[str, Sequence[str]]]
) -> None:
    """Show `rows` (key, cells): a row whose key is there already only has its changed
    cells written, so nothing flickers and the selection stays on the same rows. The
    selection's signals are quiet meanwhile: whoever fills the table shows what follows."""
    chosen = selected(view)
    model_ = view.selectionModel()
    model_.blockSignals(True)
    try:
        _fill(view, found, rows, chosen)
    finally:
        model_.blockSignals(False)
    view.viewport().update()


def _fill(
    view: QTableView,
    found: QStandardItemModel,
    rows: Sequence[tuple[str, Sequence[str]]],
    chosen: list[str],
) -> None:
    keys = [key for key, _ in rows]
    for at in reversed(range(found.rowCount())):
        if str(found.item(at, 0).data(KEY)) not in keys:
            found.removeRow(at)
    moved = False
    for at, (key, cells) in enumerate(rows):
        have = keys_of(found)
        if key in have and have.index(key) != at:
            found.insertRow(at, found.takeRow(have.index(key)))
            moved = True
        elif key not in have:
            items = [QStandardItem(cell) for cell in cells]
            items[0].setData(key, KEY)
            found.insertRow(at, items)
            continue
        for column, cell in enumerate(cells):
            item = found.item(at, column)
            if item.text() != cell:
                item.setText(cell)
    if moved:  # a row taken out and put back is no longer selected: select it again
        model_ = view.selectionModel()
        model_.clearSelection()
        for at, key in enumerate(keys_of(found)):
            if key in chosen:
                model_.select(
                    found.index(at, 0),
                    model_.SelectionFlag.Select | model_.SelectionFlag.Rows,
                )


def pick(parent: QWidget, title: str, folder: bool, then: Callable[[str], None]) -> None:
    """Windows' own file or folder picker, opened without waiting: `then` gets the path."""
    dialog = QFileDialog(parent, title)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    if folder:
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly)
    else:
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        dialog.setNameFilters(list(model.VIDEO_FILTERS))
    dialog.fileSelected.connect(lambda path: then(path) if path else None)
    dialog.open()
