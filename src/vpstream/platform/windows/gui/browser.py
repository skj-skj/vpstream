"""Browser mode in the window: what to prepare and its progress (`PreparePanel`, under
"What to stream"), and the players of the browsers watching (`PlayerPanel`, under the
devices). Both ask through the window's worker and show what the window's poll brings.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING, Any

from vpstream.platform.windows.gui import model
from vpstream.stream import NeedsConfirm

if TYPE_CHECKING:
    from vpstream.platform.windows.gui.app import Window

OFF = "Off"


class PreparePanel:
    """The tracks of the chosen video, what of it is still to make, Prepare, and a bar for
    each job. `ready` says whether Start can go: nothing is left to make."""

    def __init__(self, parent: tk.Misc, window: Window) -> None:
        self.window = window
        self.frame = ttk.LabelFrame(parent, text="For browsers and TVs")
        self.ready = False
        self.plans: list[dict[str, Any]] = []
        self.source = ""
        self.subs: list[model.Track] = []
        self.checks: dict[int, tk.BooleanVar] = {}
        self.was_busy = False
        row = ttk.Frame(self.frame)
        row.pack(fill="x", padx=8, pady=2)
        ttk.Label(row, text="Audio it starts with").pack(side="left")
        self.audio = tk.StringVar()
        self.audio_box = ttk.Combobox(row, textvariable=self.audio, state="readonly", width=40)
        self.audio_box.pack(side="left", padx=8)
        ttk.Label(self.frame, text="Subtitles to prepare:").pack(anchor="w", padx=8)
        self.sub_box = ttk.Frame(self.frame)
        self.sub_box.pack(fill="x", padx=16)
        row = ttk.Frame(self.frame)
        row.pack(fill="x", padx=8, pady=2)
        ttk.Label(row, text="Subtitles it starts with").pack(side="left")
        self.start_sub = tk.StringVar(value=OFF)
        self.start_sub_box = ttk.Combobox(
            row, textvariable=self.start_sub, state="readonly", width=40
        )
        self.start_sub_box.pack(side="left", padx=8)
        self.left = ttk.Label(self.frame, justify="left", wraplength=640)
        self.left.pack(fill="x", padx=8, pady=2)
        self.jobs = ttk.Frame(self.frame)
        self.jobs.pack(fill="x", padx=8)
        self.prepare_button = ttk.Button(self.frame, text="Prepare", command=self.prepare)
        self.prepare_button.pack(anchor="e", padx=8, pady=4)

    # --- what was chosen

    def options(self) -> dict[str, Any]:
        return {self.window.kind: self.window.path.get()}

    def chosen_subs(self) -> list[int]:
        return [n for n, var in self.checks.items() if var.get()]

    def audio_number(self) -> int | None:
        return _number(self.audio.get())

    def start_sub_number(self) -> int | None:
        return _number(self.start_sub.get())

    # --- asking

    def source_changed(self) -> None:
        """A new file or folder: its tracks are shown afresh."""
        self.source, self.plans, self.ready = "", [], False
        self.plan()

    def plan(self) -> None:
        """What is still to make, for the chosen subtitles (a plan only: nothing starts)."""
        if not self.window.path.get():
            return
        self.ready = False
        self.window.update_start()
        self.left.configure(text="Looking at the video...")
        options = {**self.options(), "sub": model.sub_spec(self.chosen_subs()), "dry_run": True}
        source = self.window.path.get()
        self.window.worker.do(
            lambda: self.window.session.control.prepare(options),
            lambda found: self._planned(source, found),
            self._failed,
        )

    def _planned(self, source: str, found: dict[str, Any]) -> None:
        if source != self.window.path.get():
            return  # another was chosen meanwhile
        self.plans = found["plans"]
        if source != self.source:
            self.source = source
            self._show_tracks()
        left = model.what_is_left(self.plans)
        self.ready = not left
        self.left.configure(
            text="Ready: Start can go." if self.ready else "Still to prepare:\n" + "\n".join(left)
        )
        self.window.update_start()

    def _show_tracks(self) -> None:
        audio, self.subs = model.plan_tracks(self.plans)
        self.audio_box.configure(values=[t.text for t in audio])
        self.audio.set(audio[0].text if audio else "")
        for child in self.sub_box.winfo_children():
            child.destroy()
        self.checks = {}
        for track in self.subs:
            var = tk.BooleanVar(value=False)
            self.checks[track.number] = var
            box = ttk.Checkbutton(
                self.sub_box, text=track.text, variable=var, command=self._subs_changed
            )
            if track.how == "unsupported":
                box.state(["disabled"])  # type: ignore[no-untyped-call]
            box.pack(anchor="w")
        if not self.subs:
            ttk.Label(self.sub_box, text="(this video has none)").pack(anchor="w")
        self._subs_changed(plan=False)

    def _subs_changed(self, plan: bool = True) -> None:
        chosen = [t for t in self.subs if t.number in self.chosen_subs()]
        self.start_sub_box.configure(values=[OFF, *(t.text for t in chosen)])
        if self.start_sub.get() not in self.start_sub_box.cget("values"):
            self.start_sub.set(OFF)
        if plan:
            self.plan()

    def prepare(self, confirmed: bool = False) -> None:
        options = {**self.options(), "sub": model.sub_spec(self.chosen_subs())}
        if confirmed:
            options["confirmed"] = True
        self.window.say("Preparing...")
        self.window.worker.do(
            lambda: self.window.session.control.prepare(options),
            lambda found: self.window.say(f"{len(found['jobs'])} jobs queued."),
            self._prepare_failed,
        )

    def _prepare_failed(self, exc: Exception) -> None:
        if isinstance(exc, NeedsConfirm):
            if messagebox.askyesno(
                "vpstream", f"{exc}.\n\nPrepare it all the same?", parent=self.window.root
            ):
                self.prepare(confirmed=True)
            return
        self._failed(exc)

    def _failed(self, exc: Exception) -> None:
        self.left.configure(text=str(exc))
        self.window.failed(exc)

    # --- what the poll brings

    def show(self, status: dict[str, Any]) -> None:
        names = [p["name"] for p in self.plans]
        jobs = model.jobs_of(status.get("prepare") or [], names)
        for child in self.jobs.winfo_children():
            child.destroy()
        for job in jobs:
            row = ttk.Frame(self.jobs)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=model.job_line(job), width=60).pack(side="left")
            bar = ttk.Progressbar(row, length=180, maximum=1.0, value=job.get("progress") or 0)
            bar.pack(side="left", padx=8)
        now_busy = model.busy(jobs)
        if now_busy:
            self.prepare_button.state(["disabled"])  # type: ignore[no-untyped-call]
        else:
            self.prepare_button.state(["!disabled"])  # type: ignore[no-untyped-call]
        if self.was_busy and not now_busy:
            self.plan()  # done: is it ready now?
        self.was_busy = now_busy


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
