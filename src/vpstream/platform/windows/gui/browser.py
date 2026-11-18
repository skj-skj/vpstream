"""Browser mode in the window.

The Prepare tab (`PrepareTab`) makes the versions a browser plays, for any file or folder,
one after another in one queue, while a stream runs or not; below the queue are the videos
already prepared, to stream or remove. On the Stream tab, `StartPanel` says whether the
chosen video is ready and what it starts with, and `PlayerPanel` sends the players of the
browsers watching their audio, subtitles and a time to go to. All of them ask through the
window's worker and show what its poll brings.
"""

from __future__ import annotations

import functools
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING, Any

from vpstream.platform.windows.gui import model
from vpstream.stream import NeedsConfirm

if TYPE_CHECKING:
    from vpstream.platform.windows.gui.app import Window

OFF = "Off"
LIBRARY_SECONDS = 5.0  # how often the list of prepared videos is read while it shows


def disable(widget: ttk.Widget, off: bool = True) -> None:
    widget.state(["disabled" if off else "!disabled"])  # type: ignore[no-untyped-call]


class JobBar:
    """A job's bar. It moves gently while ffmpeg has not said how far it is yet (a busy
    bar), then glides to each new value, 25 times a second, where the poll brings one a
    second."""

    STEP_MS = 40
    BUSY_MS = 60  # the busy bar's step: slow enough to read as working, not flickering

    def __init__(self, parent: tk.Misc) -> None:
        self.bar = ttk.Progressbar(parent, length=180, maximum=1.0)
        self.bar.pack(side="left", padx=8)
        self.target = self.value = 0.0
        self.busy = False
        self.gliding = False

    def show(self, state: str, progress: float) -> None:
        busy = state == "running" and progress <= 0.0
        if busy != self.busy:
            self.busy = busy
            if busy:
                self.bar.configure(mode="indeterminate")
                self.bar.start(self.BUSY_MS)
            else:
                self.bar.stop()
                self.bar.configure(mode="determinate", value=self.value)
        self.target = 1.0 if state == "done" else progress
        if not busy and not self.gliding:
            self.gliding = True
            self.bar.after(self.STEP_MS, self._glide)

    def _glide(self) -> None:
        if not self.bar.winfo_exists() or self.busy:
            self.gliding = False
            return
        gap = self.target - self.value
        self.value = self.target if abs(gap) < 0.002 else self.value + gap * 0.2
        self.bar.configure(value=self.value)
        if self.value == self.target:
            self.gliding = False
        else:
            self.bar.after(self.STEP_MS, self._glide)


class PrepareTab:
    """Pick a video and its subtitles, prepare it; the queue; the prepared videos."""

    def __init__(self, parent: ttk.Frame, window: Window) -> None:
        self.window = window
        self.frame = parent
        self.path = tk.StringVar()
        self.kind = "file"
        self.plans: list[dict[str, Any]] = []
        self.source = ""
        self.subs: list[model.Track] = []
        self.checks: dict[int, tk.BooleanVar] = {}
        self.job_rows: dict[int, tuple[ttk.Frame, ttk.Label, JobBar]] = {}
        self.active = 0
        self.library: list[model.LibraryRow] = []
        self.library_raw: list[dict[str, Any]] = []
        self.read_at = 0.0

        what = ttk.LabelFrame(parent, text="Prepare a video for browsers and TVs")
        what.pack(fill="x", padx=8, pady=4)
        row = ttk.Frame(what)
        row.pack(fill="x", padx=8, pady=4)
        ttk.Button(row, text="Video file...", command=self._pick_file).pack(side="left")
        ttk.Button(row, text="Folder...", command=self._pick_folder).pack(side="left", padx=4)
        ttk.Label(row, textvariable=self.path, wraplength=520).pack(side="left", padx=8)
        ttk.Label(what, text="Subtitles to prepare:").pack(anchor="w", padx=8)
        self.sub_box = ttk.Frame(what)
        self.sub_box.pack(fill="x", padx=16)
        self.left = ttk.Label(what, justify="left", wraplength=700)
        self.left.pack(fill="x", padx=8, pady=2)
        self.prepare_button = ttk.Button(what, text="Prepare", command=self.prepare)
        self.prepare_button.pack(anchor="e", padx=8, pady=4)
        disable(self.prepare_button)

        queue = ttk.LabelFrame(parent, text="Queue (one after another, while you watch)")
        queue.pack(fill="x", padx=8, pady=4)
        self.jobs = ttk.Frame(queue)
        self.jobs.pack(fill="x", padx=8, pady=2)
        self.empty = ttk.Label(queue, text="Nothing is being prepared.")
        self.empty.pack(anchor="w", padx=8, pady=2)

        shelf = ttk.LabelFrame(parent, text="Prepared videos")
        shelf.pack(fill="both", expand=True, padx=8, pady=4)
        columns = ("name", "subs", "size", "state")
        self.table = ttk.Treeview(shelf, columns=columns, show="headings", height=7)
        for column, head, width in zip(
            columns, ("Video", "Subtitles", "Size", ""), (300, 170, 80, 220), strict=True
        ):
            self.table.heading(column, text=head)
            self.table.column(column, width=width, anchor="w")
        self.table.pack(fill="both", expand=True, padx=8, pady=4)
        row = ttk.Frame(shelf)
        row.pack(fill="x", padx=8, pady=4)
        ttk.Button(row, text="Stream it", command=self.stream_it).pack(side="right")
        ttk.Button(row, text="Remove", command=self.remove).pack(side="right", padx=4)
        ttk.Label(
            row,
            text="Stream it while another streams: the next one plays at the same address.",
            foreground="#555",
        ).pack(side="left")

    # --- choosing what to prepare

    def _pick_file(self) -> None:
        found = filedialog.askopenfilename(
            parent=self.window.root,
            title="A video to prepare",
            filetypes=[("Videos", "*.mkv *.mp4 *.m4v *.mov *.avi *.webm *.ts"), ("All", "*.*")],
        )
        if found:
            self.load(found, "file")

    def _pick_folder(self) -> None:
        found = filedialog.askdirectory(parent=self.window.root, title="A folder of videos")
        if found:
            self.load(found, "dir")

    def load(self, path: str, kind: str) -> None:
        self.path.set(os.path.normpath(path))
        self.kind = kind
        self.source, self.plans = "", []
        self.plan()

    def chosen_subs(self) -> list[int]:
        return [n for n, var in self.checks.items() if var.get()]

    def plan(self) -> None:
        """What is still to make, for the chosen subtitles (a plan only: nothing starts)."""
        if not self.path.get():
            return
        disable(self.prepare_button)
        self.left.configure(text="Looking at the video...")
        source = self.path.get()
        options = {self.kind: source, "sub": model.sub_spec(self.chosen_subs()), "dry_run": True}
        self.window.worker.do(
            lambda: self.window.session.control.prepare(options),
            lambda found: self._planned(source, found),
            self._failed,
        )

    def _planned(self, source: str, found: dict[str, Any]) -> None:
        if source != self.path.get():
            return  # another was chosen meanwhile
        self.plans = found["plans"]
        if source != self.source:
            self.source = source
            self._show_tracks()
        left = model.what_is_left(self.plans)
        self.left.configure(
            text="All of it is prepared." if not left else "To prepare:\n" + "\n".join(left)
        )
        disable(self.prepare_button, not left)

    def _show_tracks(self) -> None:
        _, self.subs = model.plan_tracks(self.plans)
        for child in self.sub_box.winfo_children():
            child.destroy()
        self.checks = {}
        rows = (len(self.subs) + 1) // 2  # two columns: a file with many tracks fits
        for at, track in enumerate(self.subs):
            var = tk.BooleanVar(value=False)
            self.checks[track.number] = var
            box = ttk.Checkbutton(self.sub_box, text=track.text, variable=var, command=self.plan)
            if track.how == "unsupported":
                disable(box)
            box.grid(row=at % rows, column=at // rows, sticky="w", padx=(0, 16))
        if not self.subs:
            ttk.Label(self.sub_box, text="(none in this video)").grid(row=0, column=0)

    def prepare(self, confirmed: bool = False) -> None:
        options: dict[str, Any] = {
            self.kind: self.path.get(),
            "sub": model.sub_spec(self.chosen_subs()),
        }
        if confirmed:
            options["confirmed"] = True
        disable(self.prepare_button)

        def queued(found: dict[str, Any]) -> None:
            self.window.say(f"Queued: {len(found['jobs'])} parts. Choose the next one any time.")
            self.plan()

        self.window.worker.do(
            lambda: self.window.session.control.prepare(options), queued, self._prepare_failed
        )

    def _prepare_failed(self, exc: Exception) -> None:
        if isinstance(exc, NeedsConfirm):
            if messagebox.askyesno(
                "vpstream", f"{exc}.\n\nPrepare it all the same?", parent=self.window.root
            ):
                self.prepare(confirmed=True)
                return
            disable(self.prepare_button, False)
            return
        self._failed(exc)

    def _failed(self, exc: Exception) -> None:
        self.left.configure(text=str(exc))
        disable(self.prepare_button, False)
        self.window.failed(exc)

    # --- the queue

    def show(self, status: dict[str, Any]) -> None:
        jobs = model.all_jobs(status.get("prepare") or [])
        ids = [int(job["id"]) for job in jobs]
        for key in [k for k in self.job_rows if k not in ids]:
            self.job_rows.pop(key)[0].destroy()
        for job in jobs:
            key = int(job["id"])
            if key not in self.job_rows:
                row = ttk.Frame(self.jobs)
                row.pack(fill="x", pady=1)
                label = ttk.Label(row, width=64)
                label.pack(side="left")
                bar = JobBar(row)
                source = str(job["source"])
                cancel = functools.partial(self.cancel, source)
                ttk.Button(row, text="Cancel", command=cancel).pack(side="left")
                self.job_rows[key] = (row, label, bar)
            _, label, bar = self.job_rows[key]
            line = model.job_line(job)
            if label.cget("text") != line:
                label.configure(text=line)
            bar.show(str(job.get("state")), float(job.get("progress") or 0))
        if jobs and self.empty.winfo_manager():
            self.empty.pack_forget()
        elif not jobs and not self.empty.winfo_manager():
            self.empty.pack(anchor="w", padx=8, pady=2)
        active = sum(1 for j in jobs if j.get("state") in ("queued", "running"))
        ended = active < self.active
        self.active = active
        if ended:
            self.window.say("Prepared." if not active else f"{active} parts still to prepare.")
            self.plan()
        now = float(status.get("now") or 0)
        if ended or now - self.read_at >= LIBRARY_SECONDS:
            self.read_at = now
            self.refresh_library()

    def cancel(self, source: str) -> None:
        self.window.worker.do(
            lambda: self.window.session.control.cancel_prepare({"path": source}),
            lambda found: self.window.say(f"Cancelled {found['cancelled']} parts."),
            self.window.failed,
        )

    # --- the prepared videos

    def refresh_library(self) -> None:
        self.window.worker.do(self.window.session.control.prepared, self._library)

    def _library(self, rows: list[dict[str, Any]]) -> None:
        self.library_raw = rows
        self.library = model.library_rows(rows)
        chosen = set(self.table.selection())
        keys = [r.key for r in self.library]
        gone = [i for i in self.table.get_children() if i not in keys]
        if gone:
            self.table.delete(*gone)
        for at, row in enumerate(self.library):
            if self.table.exists(row.key):
                if tuple(self.table.item(row.key, "values")) != row.cells:
                    self.table.item(row.key, values=row.cells)
                if self.table.index(row.key) != at:
                    self.table.move(row.key, "", at)
            else:
                self.table.insert("", at, iid=row.key, values=row.cells)
        self.table.selection_set([k for k in keys if k in chosen])
        self.window.start_panel.library_changed()

    def _selected(self) -> model.LibraryRow | None:
        chosen = self.table.selection()
        row = next((r for r in self.library if chosen and r.key == chosen[0]), None)
        if row is None:
            messagebox.showinfo(
                "vpstream", "Select a video in the list first.", parent=self.window.root
            )
        return row

    def stream_it(self) -> None:
        row = self._selected()
        if row is None:
            return
        if not row.ready:
            messagebox.showinfo("vpstream", f"{row.name} is {row.state}.", parent=self.window.root)
            return
        self.window.stream_prepared(row.source)

    def remove(self) -> None:
        row = self._selected()
        if row is None or not messagebox.askyesno(
            "vpstream",
            f"Remove what was prepared of {row.name} ({row.size})? The video itself stays.",
            parent=self.window.root,
        ):
            return

        def removed(found: dict[str, Any]) -> None:
            if found["kept"]:
                self.window.say("It is streaming or being prepared, so it was kept.")
            self.refresh_library()

        self.window.worker.do(
            lambda: self.window.session.control.remove_prepared({"path": row.source}),
            removed,
            self.window.failed,
        )


class StartPanel:
    """On the Stream tab, in browser mode: whether the chosen video is prepared, and the
    audio and subtitles it starts with. `ready` says whether Start can go."""

    def __init__(self, parent: tk.Misc, window: Window) -> None:
        self.window = window
        self.frame = ttk.LabelFrame(parent, text="For browsers and TVs")
        self.ready = False
        self.tracks: tuple[list[model.Track], list[model.Track]] = ([], [])
        self.state = ttk.Label(self.frame, justify="left", wraplength=640)
        self.state.pack(fill="x", padx=8, pady=2)
        row = ttk.Frame(self.frame)
        row.pack(fill="x", padx=8, pady=2)
        ttk.Label(row, text="Audio it starts with").pack(side="left")
        self.audio = tk.StringVar()
        self.audio_box = ttk.Combobox(row, textvariable=self.audio, state="readonly", width=34)
        self.audio_box.pack(side="left", padx=8)
        row = ttk.Frame(self.frame)
        row.pack(fill="x", padx=8, pady=2)
        ttk.Label(row, text="Subtitles it starts with").pack(side="left")
        self.sub = tk.StringVar(value=OFF)
        self.sub_box = ttk.Combobox(row, textvariable=self.sub, state="readonly", width=34)
        self.sub_box.pack(side="left", padx=8)
        self.to_prepare = ttk.Button(self.frame, text="Prepare it...", command=self._prepare_it)
        self.to_prepare.pack(anchor="e", padx=8, pady=4)

    def audio_number(self) -> int | None:
        return _number(self.audio.get())

    def sub_number(self) -> int | None:
        return _number(self.sub.get())

    def check(self) -> None:
        """Is the chosen video (or every video of the folder) prepared?"""
        path = self.window.path.get()
        self.ready = False
        self.window.update_start()
        if not path:
            return
        self.state.configure(text="Looking at what is prepared...")
        options = {self.window.kind: path, "sub": "none", "dry_run": True}
        self.window.worker.do(
            lambda: self.window.session.control.prepare(options),
            lambda found: self._checked(path, found),
            self.window.failed,
        )

    def _checked(self, path: str, found: dict[str, Any]) -> None:
        if path != self.window.path.get():
            return
        plans = found["plans"]
        left = model.what_is_left(plans)
        self.ready = not left
        self.tracks = model.plan_tracks(plans)
        audio = self.tracks[0]
        self.audio_box.configure(values=[t.text for t in audio])
        if self.audio.get() not in self.audio_box.cget("values"):
            self.audio.set(audio[0].text if audio else "")
        self.library_changed()
        if self.ready:
            self.state.configure(text="Prepared: it can start.")
            self.to_prepare.pack_forget()
        else:
            more = "\n..." if len(left) > 5 else ""
            self.state.configure(text="Not prepared yet:\n" + "\n".join(left[:5]) + more)
            self.to_prepare.pack(anchor="e", padx=8, pady=4)
        self.window.update_start()

    def library_changed(self) -> None:
        """The subtitles a file starts with are those prepared for it (a folder: off)."""
        if self.window.kind != "file":
            self.sub_box.configure(values=[OFF])
            self.sub.set(OFF)
            return
        made = model.prepared_subs(self.window.prepare_tab.library_raw, self.window.path.get())
        labels = [t.text for t in self.tracks[1] if t.number in made]
        self.sub_box.configure(values=[OFF, *labels])
        if self.sub.get() not in self.sub_box.cget("values"):
            self.sub.set(OFF)

    def _prepare_it(self) -> None:
        self.window.prepare_tab.load(self.window.path.get(), self.window.kind)
        self.window.tabs.select(self.window.prepare_tab.frame)  # type: ignore[no-untyped-call]


class PlayerPanel:
    """For the browsers selected in the device list (or all): their audio, subtitles and
    where they are, sent to their players."""

    def __init__(self, parent: tk.Misc, window: Window) -> None:
        self.window = window
        self.frame = ttk.LabelFrame(parent, text="The players of the selected browsers")
        self.where = ttk.Label(self.frame, text="Select a browser in the list.")
        self.where.pack(anchor="w", padx=8)
        row = ttk.Frame(self.frame)
        row.pack(fill="x", padx=8, pady=4)
        ttk.Label(row, text="Audio").pack(side="left")
        self.audio = tk.StringVar()
        self.audio_box = ttk.Combobox(row, textvariable=self.audio, state="readonly", width=24)
        self.audio_box.pack(side="left", padx=4)
        ttk.Label(row, text="Subtitles").pack(side="left", padx=(8, 0))
        self.sub = tk.StringVar()
        self.sub_box = ttk.Combobox(row, textvariable=self.sub, state="readonly", width=28)
        self.sub_box.pack(side="left", padx=4)
        ttk.Label(row, text="Go to").pack(side="left", padx=(8, 0))
        self.seek = tk.StringVar()
        ttk.Entry(row, textvariable=self.seek, width=8).pack(side="left", padx=4)
        row = ttk.Frame(self.frame)
        row.pack(fill="x", padx=8, pady=4)
        ttk.Button(row, text="Send to all", command=lambda: self.send(everyone=True)).pack(
            side="right"
        )
        ttk.Button(row, text="Send to selected", command=self.send).pack(side="right", padx=4)
        self.menu: dict[str, list[Any]] = {"audio": [], "subs": []}
        self.shown_for: tuple[str, ...] = ()

    def show(self, status: dict[str, Any], selected: list[str]) -> None:
        running = status["stream"]
        devices = {d["id"]: d for d in running["devices"]}
        first = devices.get(selected[0]) if selected else None
        self.where.configure(
            text=model.where_it_is(first) if first else "Select a browser in the list."
        )
        menu = model.menu_for(status, first)
        key = (str(selected[:1]), str(menu))
        if key != self.shown_for:  # only when it changes: a choice being made stays
            self.shown_for, self.menu = key, menu
            self.audio_box.configure(values=["", *(m["label"] for m in menu["audio"])])
            self.sub_box.configure(values=["", *(m["label"] for m in menu["subs"])])
            self.audio.set("")
            self.sub.set("")

    def send(self, everyone: bool = False) -> None:
        options: dict[str, Any] = {"all": True} if everyone else {}
        if not everyone:
            chosen = list(self.window.devices.selection())
            if not chosen:
                messagebox.showinfo(
                    "vpstream", "Select a browser in the list first.", parent=self.window.root
                )
                return
            options["devices"] = chosen
        audio = _value(self.menu["audio"], self.audio.get())
        sub = _value(self.menu["subs"], self.sub.get())
        if audio is not None:
            options["audio"] = audio
        if sub is not None:
            options["sub"] = sub
        if self.seek.get().strip():
            try:
                options["seek"] = model.seconds_of(self.seek.get())
            except ValueError as exc:
                messagebox.showinfo("vpstream", str(exc), parent=self.window.root)
                return
        if len(options) == 1:
            messagebox.showinfo(
                "vpstream",
                "Choose audio, subtitles or a time to go to first.",
                parent=self.window.root,
            )
            return

        def sent(found: dict[str, Any]) -> None:
            self.seek.set("")
            self.window._show(found)

        self.window.worker.do(
            lambda: self.window.session.control.set_clients(options), sent, self.window.failed
        )


def _number(text: str) -> int | None:
    head = text.split(":", 1)[0].strip()
    return int(head) if head.isdigit() else None


def _value(menu: list[dict[str, Any]], label: str) -> str | None:
    return next((str(m["value"]) for m in menu if m["label"] == label), None) if label else None
