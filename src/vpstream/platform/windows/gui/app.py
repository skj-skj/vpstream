"""The vpstream window (Tk). One window: what is missing and installing it, what to stream
and how, and while it streams its address, the devices and Stop.

The window never waits: whatever asks Windows, Caddy or the disk runs on a worker thread,
and its answer comes back through a queue that the window's own thread empties every
100 ms. Only that thread touches Tk. What the window says and sends is in `model`.
"""

from __future__ import annotations

import contextlib
import functools
import os
import queue
import threading
import tkinter as tk
import traceback
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any

from vpstream import __version__, local
from vpstream.platform.common import UrlHttp
from vpstream.platform.windows import firewall, install, network, processes, shortcut, tools_dir
from vpstream.platform.windows.gui import browser, model
from vpstream.platform.windows.session import Session
from vpstream.tools import Runner

POLL_MS = 1000
DRAIN_MS = 100
PX, PY = 8, 4  # the space around each part


def enable(button: ttk.Button, on: bool) -> None:
    button.state(["!disabled" if on else "disabled"])  # type: ignore[no-untyped-call]


class Worker:
    """One thread that runs what the window asks, one thing after another."""

    def __init__(self, answers: queue.Queue[Callable[[], None]], log: Callable[[str], None]):
        self.jobs: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self.answers, self.log = answers, log
        self.busy = 0
        threading.Thread(target=self._run, name="window-worker", daemon=True).start()

    def _run(self) -> None:
        while (job := self.jobs.get()) is not None:
            job()

    def do(
        self,
        work: Callable[[], Any],
        done: Callable[[Any], None] | None = None,
        failed: Callable[[Exception], None] | None = None,
    ) -> None:
        """Run `work` here; `done` (or `failed`) then runs on the window's thread."""
        self.busy += 1

        def job() -> None:
            try:
                found = work()
            except Exception as exc:
                self.log(f"WARNING: {exc!r}\n{traceback.format_exc()}")
                self.answers.put(functools.partial(self._end, failed, exc))
            else:
                self.answers.put(lambda: self._end(done, found))

        self.jobs.put(job)

    def _end(self, then: Callable[[Any], None] | None, value: Any) -> None:
        self.busy -= 1
        if then is not None:
            then(value)


class Window:
    def __init__(
        self, root: tk.Tk, session: Session, runner: Runner, log_path: Path, other_admin: str
    ) -> None:
        self.root, self.session, self.runner = root, session, runner
        self.log_path, self.other_admin = log_path, other_admin
        self.answers: queue.Queue[Callable[[], None]] = queue.Queue()
        self.worker = Worker(self.answers, session.log)
        self.status: dict[str, Any] | None = None
        self.addresses: list[network.Address] = []
        self.choices: list[model.Choice] = []
        self.blocking: list[str] = []
        self.awake = False
        self.was_streaming = False
        self.stopping_on_purpose = False
        self.closing = False
        self.destroyed = False
        self.starting = False
        root.title("vpstream")
        root.minsize(820, 640)
        with contextlib.suppress(tk.TclError):
            root.iconbitmap(default=str(shortcut.ICON))  # type: ignore[no-untyped-call]
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self.update_start()
        root.after(DRAIN_MS, self._drain)
        self._poll()

    # --- the layout

    def _build(self) -> None:
        top = ttk.Frame(self.root)
        top.pack(fill="both", expand=True)
        self.notice = ttk.Label(top, foreground="#b00020", wraplength=680, justify="left")
        self.notice.pack(fill="x", padx=PX, pady=PY)
        self.warn_box = ttk.Frame(top)
        self.warn_box.pack(fill="x")

        self.setup = ttk.LabelFrame(top, text="Programs vpstream needs")
        self.setup_rows: dict[str, tuple[ttk.Label, ttk.Progressbar]] = {}

        self.pick = ttk.LabelFrame(top, text="What to stream")
        self.pick.pack(fill="x", padx=PX, pady=PY)
        self.path = tk.StringVar()
        self.kind = "file"
        row = ttk.Frame(self.pick)
        row.pack(fill="x", padx=PX, pady=PY)
        ttk.Button(row, text="Video file...", command=self._pick_file).pack(side="left")
        ttk.Button(row, text="Folder...", command=self._pick_folder).pack(side="left", padx=4)
        ttk.Label(row, textvariable=self.path, wraplength=480).pack(side="left", padx=8)

        form = ttk.Frame(self.pick)
        form.pack(fill="x", padx=PX, pady=PY)
        ttk.Label(form, text="Name in the address (optional)").grid(row=0, column=0, sticky="w")
        self.alias = tk.StringVar()
        ttk.Entry(form, textvariable=self.alias, width=24).grid(row=0, column=1, sticky="w")
        ttk.Label(form, text="Watch it with").grid(row=1, column=0, sticky="w")
        self.mode = tk.StringVar(value=model.MODES[model.DIRECT])
        mode = ttk.Combobox(
            form, textvariable=self.mode, values=list(model.MODES.values()), state="readonly",
            width=40,
        )  # fmt: skip
        mode.grid(row=1, column=1, sticky="w")
        mode.bind("<<ComboboxSelected>>", lambda e: self._mode_changed())
        ttk.Label(form, text="Who may watch").grid(row=2, column=0, sticky="w")
        self.policy = tk.StringVar(value=model.default_policy(model.DIRECT))
        ttk.Combobox(
            form, textvariable=self.policy, values=list(model.POLICIES.values()),
            state="readonly", width=40,
        ).grid(row=2, column=1, sticky="w")  # fmt: skip
        ttk.Label(form, text="This PC's address").grid(row=3, column=0, sticky="w")
        self.address = tk.StringVar(value="looking...")
        self.address_box = ttk.Combobox(form, textvariable=self.address, state="readonly", width=40)
        self.address_box.grid(row=3, column=1, sticky="w")
        self.address_box.bind("<<ComboboxSelected>>", lambda e: self._address_chosen())
        ttk.Button(form, text="Look again", command=self._look).grid(row=3, column=2, padx=4)
        for child in form.winfo_children():
            child.grid_configure(pady=2, padx=4)
        self.prepare = browser.PreparePanel(self.pick, self)
        self.start_button = ttk.Button(self.pick, text="Start", command=self._start)
        self.start_button.pack(anchor="e", padx=PX, pady=PY)
        self.setup_missing = False

        self.live = ttk.LabelFrame(top, text="Streaming")
        row = ttk.Frame(self.live)
        row.pack(fill="x", padx=PX, pady=PY)
        self.url = tk.StringVar()
        url = ttk.Entry(row, textvariable=self.url, state="readonly", font=("Segoe UI", 14))
        url.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Copy", command=self._copy).pack(side="left", padx=4)
        self.lines = ttk.Label(self.live, justify="left", wraplength=680)
        self.lines.pack(fill="x", padx=PX, pady=PY)
        columns = ("name", "address", "what", "state", "playing", "sent", "seen")
        heads = ("Device", "Address", "What", "State", "Playing", "Sent", "Last seen")
        widths = (130, 100, 110, 140, 200, 70, 80)
        self.devices = ttk.Treeview(self.live, columns=columns, show="headings", height=6)
        for column, head, width in zip(columns, heads, widths, strict=True):
            self.devices.heading(column, text=head)
            self.devices.column(column, width=width, anchor="w")
        self.devices.pack(fill="both", expand=True, padx=PX, pady=PY)
        row = ttk.Frame(self.live)
        row.pack(fill="x", padx=PX, pady=PY)
        ttk.Button(row, text="Let in", command=lambda: self._device(True)).pack(side="left")
        ttk.Button(row, text="Keep out", command=lambda: self._device(False)).pack(
            side="left", padx=4
        )
        ttk.Button(row, text="Rename...", command=self._rename).pack(side="left")
        ttk.Button(row, text="Stop", command=self._stop).pack(side="right")
        ttk.Button(row, text="Change name", command=self._change_alias).pack(side="right", padx=4)
        self.new_alias = tk.StringVar()
        ttk.Entry(row, textvariable=self.new_alias, width=16).pack(side="right")
        self.players = browser.PlayerPanel(self.live, self)

        bottom = ttk.Frame(top)
        bottom.pack(fill="x", side="bottom", padx=PX, pady=PY)
        self.said = ttk.Label(bottom, text="Starting...", wraplength=520)
        self.said.pack(side="left")
        ttk.Button(bottom, text="Open log", command=self._open_log).pack(side="right")
        ttk.Label(bottom, text=f"Log: {self.log_path}", foreground="#555").pack(side="right")
        ttk.Label(bottom, text=f"vpstream {__version__}").pack(side="right", padx=8)

    # --- the window's own thread

    def _drain(self) -> None:
        while True:
            try:
                then = self.answers.get_nowait()
            except queue.Empty:
                break
            try:
                then()
            except Exception as exc:  # a bug in the window must not end it
                self.session.log(f"WARNING: the window: {exc!r}\n{traceback.format_exc()}")
        if not self.destroyed:
            self.root.after(DRAIN_MS, self._drain)

    def _poll(self) -> None:
        if self.closing:
            return

        def look() -> None:
            try:
                found = self.session.control.status()
            except Exception as exc:
                self.session.log(f"WARNING: status: {exc!r}")
                found = None
            self.answers.put(lambda: self._show(found))

        threading.Thread(target=look, name="poll", daemon=True).start()
        self.root.after(POLL_MS, self._poll)

    def say(self, text: str) -> None:
        self.said.configure(text=text)

    def failed(self, exc: Exception) -> None:
        self.say("")
        messagebox.showerror("vpstream", str(exc), parent=self.root)

    # --- what is there

    def begin(self) -> None:
        """Once the window shows: the session (which may take up a stream a crash left),
        what is missing, the networks, the firewall."""
        self.say("Starting...")
        self.worker.do(self.session.open, lambda _: self._opened(), self.failed)

    def _opened(self) -> None:
        self.notice.configure(text=self.session.notice)
        missing = install.missing(self.runner)
        if missing:
            self._offer_setup(missing)
        self._look()
        caddy = self.runner.which("caddy")
        if caddy:
            self.worker.do(lambda: firewall.blocking_rules(self.runner, caddy), self._blocking)
        self.say("Ready.")

    def _offer_setup(self, missing: list[str]) -> None:
        for child in self.setup.winfo_children():
            child.destroy()
        ttk.Label(
            self.setup,
            text="vpstream needs these programs. It downloads them from their own sites "
            "and checks each one before it is used.",
            wraplength=680,
        ).pack(fill="x", padx=PX, pady=PY)
        for name in missing:
            row = ttk.Frame(self.setup)
            row.pack(fill="x", padx=PX, pady=PY)
            label = ttk.Label(row, text=f"{name}: not installed", width=48)
            label.pack(side="left")
            bar = ttk.Progressbar(row, length=220, mode="determinate")
            bar.pack(side="left", padx=8)
            self.setup_rows[name] = (label, bar)
        self.install_button = ttk.Button(self.setup, text="Install", command=self._install)
        self.install_button.pack(anchor="e", padx=PX, pady=PY)
        self.setup.pack(fill="x", before=self.pick, padx=PX, pady=PY)
        self.setup_missing = True
        self.update_start()

    def _install(self) -> None:
        enable(self.install_button, False)
        names = list(self.setup_rows)

        def progress(name: str) -> Callable[[str, int, int], None]:
            def show(what: str, done: int, total: int) -> None:
                self.answers.put(lambda: self._progress(name, what, done, total))

            return show

        def work() -> list[str]:
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
                    self.answers.put(functools.partial(self._progress, name, "installed", 1, 1))
                except Exception as exc:
                    problems.append(f"{name}: {exc}")
                    self.answers.put(
                        functools.partial(self._progress, name, f"failed: {exc}", 0, 1)
                    )
            return problems

        self.worker.do(work, self._installed, self.failed)

    def _progress(self, name: str, what: str, done: int, total: int) -> None:
        label, bar = self.setup_rows[name]
        label.configure(text=f"{name}: {what}")
        if total:
            bar.configure(mode="determinate", maximum=total, value=done)
        else:
            bar.configure(mode="indeterminate")
            bar.start(15)
        if what == "installed" or what.startswith("failed"):
            bar.stop()
            bar.configure(mode="determinate", maximum=1, value=1 if what == "installed" else 0)

    def _installed(self, problems: list[str]) -> None:
        missing = install.missing(self.runner)
        required = [n for n in missing if n != install.MKVTOOLNIX]
        if problems:
            messagebox.showwarning("vpstream", "\n\n".join(problems), parent=self.root)
        if required:
            self._offer_setup(missing)
            return
        self.setup.pack_forget()
        self.setup_missing = False
        self.update_start()
        caddy = self.runner.which("caddy")
        if caddy:
            self.worker.do(lambda: firewall.blocking_rules(self.runner, caddy), self._blocking)
        self.say("Everything vpstream needs is installed.")

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
        self.address_box.configure(values=[c.label for c in self.choices])
        self.address.set(model.chosen_label(self.choices, self.session.config.local_address))
        self._warnings()

    def _address_chosen(self) -> None:
        value = model.address_of(self.choices, self.address.get())
        if value is None or value == self.session.config.local_address:
            return
        self.worker.do(lambda: self.session.choose_address(value), lambda _: self._warnings())

    def _blocking(self, names: list[str]) -> None:
        self.blocking = names
        self._warnings()

    def _warnings(self) -> None:
        for child in self.warn_box.winfo_children():
            child.destroy()
        ip = self.session.control.host()
        address = model.network_of(self.addresses, ip)
        for text in model.warnings(address, self.blocking, self.other_admin):
            row = ttk.Frame(self.warn_box)
            row.pack(fill="x", padx=PX, pady=PY)
            ttk.Label(row, text=text, wraplength=540, foreground="#8a4b00").pack(side="left")
            if "Public" in text and address is not None:
                index = address.index
                ttk.Button(
                    row,
                    text="Make it private",
                    command=functools.partial(self._make_private, index),
                ).pack(side="right")
            if "block Caddy" in text:
                ttk.Button(row, text="Remove them", command=self._unblock).pack(side="right")

    def _make_private(self, index: int) -> None:
        self.worker.do(
            lambda: network.make_private(self.runner, index), lambda _: self._look(), self.failed
        )

    def _unblock(self) -> None:
        caddy = self.runner.which("caddy") or ""
        self.worker.do(
            lambda: firewall.remove_blocking(self.runner, caddy),
            lambda _: self._blocking([]),
            self.failed,
        )

    # --- starting and stopping

    def _pick_file(self) -> None:
        found = filedialog.askopenfilename(
            parent=self.root,
            title="A video to stream",
            filetypes=[("Videos", "*.mkv *.mp4 *.m4v *.mov *.avi *.webm *.ts"), ("All", "*.*")],
        )
        if found:
            self._chosen(found, "file")

    def _pick_folder(self) -> None:
        found = filedialog.askdirectory(parent=self.root, title="A folder of videos")
        if found:
            self._chosen(found, "dir")

    def _chosen(self, path: str, kind: str) -> None:
        self.path.set(os.path.normpath(path))
        self.kind = kind
        if self.browser_mode():
            self.prepare.source_changed()
        self.update_start()

    def browser_mode(self) -> bool:
        return self.mode.get() == model.MODES[model.BROWSER]

    def _mode_changed(self) -> None:
        mode = model.BROWSER if self.browser_mode() else model.DIRECT
        self.policy.set(model.default_policy(mode))
        if mode == model.BROWSER:
            self.prepare.frame.pack(fill="x", before=self.start_button, padx=PX, pady=PY)
            self.prepare.source_changed()
        else:
            self.prepare.frame.pack_forget()
        self.update_start()

    def update_start(self) -> None:
        """Start can go when a video is chosen, the programs are there, nothing is starting,
        and in browser mode once everything it plays is prepared."""
        ready = bool(self.path.get()) and not self.setup_missing and not self.starting
        if self.browser_mode():
            ready = ready and self.prepare.ready
        enable(self.start_button, ready)

    def _start(self) -> None:
        mode = model.BROWSER if self.browser_mode() else model.DIRECT
        try:
            options = model.start_options(
                self.path.get(), self.kind, self.alias.get(), mode, self.policy.get()
            )
        except ValueError as exc:
            messagebox.showinfo("vpstream", str(exc), parent=self.root)
            return
        if mode == model.BROWSER:
            options = model.browser_start(
                options, self.prepare.audio_number(), self.prepare.start_sub_number()
            )
        self.starting = True
        self.update_start()
        self.say("Starting the stream...")

        def started(found: dict[str, Any]) -> None:
            self.starting = False
            self.update_start()
            self.say("The stream is on.")
            self._show(found)

        def failed(exc: Exception) -> None:
            self.starting = False
            self.update_start()
            self.failed(exc)

        self.worker.do(lambda: self.session.start(options), started, failed)

    def _stop(self) -> None:
        self.stopping_on_purpose = True
        self.say("Stopping...")

        def stopped(found: dict[str, Any]) -> None:
            self.say("Stopped.")
            self._show(found)

        self.worker.do(self.session.stop, stopped, self.failed)

    def _copy(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.url.get())
        self.say("The address is copied.")

    def _selected(self) -> str | None:
        chosen = self.devices.selection()
        if not chosen:
            messagebox.showinfo("vpstream", "Select a device in the list first.", parent=self.root)
            return None
        return str(chosen[0])

    def _device(self, allowed: bool) -> None:
        device = self._selected()
        if device is not None:
            self.worker.do(
                lambda: self.session.control.set_device(device, allowed), self._show, self.failed
            )

    def _rename(self) -> None:
        device = self._selected()
        if device is None:
            return
        name = simpledialog.askstring("vpstream", "A name for this device:", parent=self.root)
        if name is not None:
            options = {"devices": [device], "name": name.strip()}
            self.worker.do(
                lambda: self.session.control.set_clients(options), self._show, self.failed
            )

    def _change_alias(self) -> None:
        alias = self.new_alias.get().strip()
        if alias:
            self.worker.do(lambda: self.session.control.set_alias(alias), self._show, self.failed)

    # --- what the stream is doing

    def _show(self, status: dict[str, Any] | None) -> None:
        if status is None or self.closing:
            return
        self.status = status
        if self.browser_mode():
            self.prepare.show(status)
        running = status.get("stream")
        if running is None:
            if self.was_streaming:
                self.live.pack_forget()
                self.pick.pack(fill="x", padx=PX, pady=PY)
                why = self.session.stop_reason()
                if not self.stopping_on_purpose and why:
                    self.say(f"The stream stopped: {why}.")
                self._awake(False)
            self.was_streaming = self.stopping_on_purpose = False
            return
        if not self.was_streaming:
            self.pick.pack_forget()
            self.live.pack(fill="both", expand=True, padx=PX, pady=PY)
            self.new_alias.set(running["alias"])
            if running["mode"] == model.BROWSER:
                self.players.frame.pack(fill="x", padx=PX, pady=PY)
            else:
                self.players.frame.pack_forget()
            self._awake(True)
        self.was_streaming = True
        self.url.set(running["url"])
        self.lines.configure(text="\n".join(model.stream_lines(status)))
        rows = model.device_rows(running, status.get("now"))
        chosen = set(self.devices.selection())
        self.devices.delete(*self.devices.get_children())
        for row in rows:
            self.devices.insert("", "end", iid=row.id, values=row.cells)
        self.devices.selection_set([r.id for r in rows if r.id in chosen])
        if running["mode"] == model.BROWSER:
            self.players.show(status, [r.id for r in rows if r.id in chosen])

    def _awake(self, on: bool) -> None:
        if on != self.awake:
            processes.keep_awake(on)
            self.awake = on

    # --- the end

    def _open_log(self) -> None:
        try:
            os.startfile(self.log_path)  # type: ignore[attr-defined,unused-ignore]
        except OSError as exc:
            messagebox.showerror("vpstream", f"{self.log_path}: {exc}", parent=self.root)

    def close(self) -> None:
        if (
            self.status
            and self.status.get("stream")
            and not messagebox.askyesno(
                "vpstream", "A stream is running. Stop it and close vpstream?", parent=self.root
            )
        ):
            return
        self.closing = True
        self.say("Closing...")
        self._awake(False)

        def done() -> None:
            self.destroyed = True
            self.root.destroy()

        def end() -> None:
            self._close_session()
            self.answers.put(done)

        threading.Thread(target=end, name="close", daemon=True).start()

    def _close_session(self) -> None:
        try:
            self.session.close()
        except Exception as exc:
            self.session.log(f"WARNING: closing: {exc!r}")


def run(session: Session, runner: Runner, log_path: Path, other_admin: str = "") -> int:
    root = tk.Tk()
    window = Window(root, session, runner, log_path, other_admin)

    def bug(kind: Any, value: BaseException, trace: Any) -> None:
        session.log(
            "WARNING: the window: " + "".join(traceback.format_exception(kind, value, trace))
        )
        messagebox.showerror("vpstream", f"Something went wrong: {value}\n\nThe log has more.")

    root.report_callback_exception = bug
    root.after(50, window.begin)
    root.mainloop()
    return 0
