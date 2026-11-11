"""`vpstream prepare`: which versions browser mode needs, how long they take, and making
them in the daemon, one after the other, at low priority.

Planning and estimating are pure and unit tested. `Preparer` runs the jobs; the cache
layout is in `prepared`.
"""

from __future__ import annotations

import collections
import contextlib
import itertools
import os
import re
import secrets
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vpstream import ffargs, media, prepared, probe, subtitles, tracks
from vpstream.files import has_room, source_key, under, write_atomic
from vpstream.probe import MKVEXTRACT, MediaInfo, ProbeError, SubtitleTrack
from vpstream.tools import (
    Cancelled,
    Runner,
    ToolMissing,
    ffmpeg_names,
    low_priority_prefix,
    require,
    run_cancellable,
)

BASE, SOFT, BURN = "base", probe.SOFT, "burn"  # the kinds of task
QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
ACTIVE = (QUEUED, RUNNING)

# Measured on the VPS (4 vCPU) in milestone 0: a copy runs at 11x to 17x; an x264 veryfast
# encode of 1080p at 24 fps at 1.6x to 2.5x, slower in proportion to pixels per second; tone
# mapping HDR costs about two thirds more. The low ends are used, so estimates err long.
# They hold for that host; `vpstream bench` measures another one (see `Speeds`).
COPY_SPEED = 11.0
ENCODE_SPEED = 1.6  # times real time, for 1080p at 24 fps
REFERENCE_PIXELS_PER_FRAME = 1920 * 1080
REFERENCE_FPS = 24.0
TONEMAP_SPEED = 0.35
SOFT_SECONDS = 10.0  # reading the whole file for one subtitle track
SOFT_SPEED = 200.0
HISTORY = 500  # finished jobs kept for status: enough for a long show to be followed


@dataclass(frozen=True)
class Speeds:
    """How fast this host prepares: a copy, and an encode of 1080p at 24 fps, in times
    real time. Set in the configuration from what `vpstream bench` measured."""

    copy: float = COPY_SPEED
    encode: float = ENCODE_SPEED


DEFAULT_SPEEDS = Speeds()


class PrepareError(RuntimeError):
    """A version could not be made; the message says why."""


@dataclass(frozen=True)
class Task:
    """One version to make: the base (a copy or a transcode), a soft subtitle track, or a
    burned-in subtitle track."""

    kind: str
    sub: int = 0  # the subtitle track number, for SOFT and BURN
    copy: bool = False  # for BASE

    @property
    def name(self) -> str:
        if self.kind == BASE:
            return "base version (copy)" if self.copy else "base version (transcode to H.264)"
        return f"subtitle {self.sub} ({'soft' if self.kind == SOFT else 'burned in'})"

    @property
    def encodes(self) -> bool:
        """Whether this is a full video encode."""
        return self.kind == BURN or (self.kind == BASE and not self.copy)


def base_task(info: MediaInfo) -> Task:
    if not info.video:
        raise PrepareError(f"{info.path} has no video track")
    v = info.video[0]
    return Task(BASE, copy=ffargs.copy_ok(v.codec, v.pix_fmt, v.bit_depth))


def plan(
    info: MediaInfo, spec: str | None, have: prepared.Versions
) -> tuple[list[Task], list[str]]:
    """The versions of one video still to make for subtitle choice `spec` (see
    `tracks.select_subtitles`), the base first, and notes on choices that name nothing.
    Raises PrepareError for a file without video, SelectionError for a bad `spec`."""
    todo = [] if have.base else [base_task(info)]
    chosen, notes = tracks.select_subtitles(info.subtitles, spec)
    for track in chosen:
        if track.treatment == probe.SOFT and track.number not in have.soft:
            todo.append(Task(SOFT, track.number))
        elif track.treatment == probe.BURNED_IN and track.number not in have.burn:
            todo.append(Task(BURN, track.number))
    return todo, notes


def size_known(info: MediaInfo) -> bool:
    """Whether ffprobe told the video's size, which an encode's time depends on."""
    return bool(info.video) and info.video[0].width > 0 and info.video[0].height > 0


def load_factor(info: MediaInfo) -> float:
    """How much more work a frame of this video is than one of 1080p at 24 fps. A size
    ffprobe did not tell counts as 1080p, an unknown frame rate as 30 fps."""
    v = info.video[0] if info.video else None
    if v is None:
        return 1.0
    pixels = v.width * v.height if size_known(info) else REFERENCE_PIXELS_PER_FRAME
    fps = v.fps if v.fps > 0 else 30.0
    factor = pixels * fps / (REFERENCE_PIXELS_PER_FRAME * REFERENCE_FPS)
    return factor / TONEMAP_SPEED if v.hdr else factor


def estimate(info: MediaInfo, task: Task, speeds: Speeds = DEFAULT_SPEEDS) -> float:
    """Seconds one task is expected to take on this host."""
    if task.kind == SOFT:
        return SOFT_SECONDS + info.duration / SOFT_SPEED
    if task.kind == BASE and task.copy:
        return info.duration / speeds.copy
    return info.duration * load_factor(info) / speeds.encode


def output_bytes(info: MediaInfo, task: Task, settings: ffargs.EncodeSettings) -> int:
    """About how much a version takes on disk: a copy is the size of the source, an encode
    at most its bit rate cap."""
    if task.kind == SOFT:
        return 1024 * 1024
    if task.kind == BASE and task.copy:
        return info.size
    maxrate = settings.maxrate.upper().removesuffix("M")
    try:
        bits = float(maxrate) * 1e6
    except ValueError:
        bits = 8e6
    return int(bits / 8 * info.duration)


@dataclass
class VideoPlan:
    """What is still to make for one video, or why it cannot be prepared."""

    source: str
    entry: Path
    info: MediaInfo | None
    tasks: list[Task] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    speeds: Speeds = DEFAULT_SPEEDS

    @property
    def duration(self) -> float:
        return self.info.duration if self.info else 0.0

    @property
    def seconds(self) -> float:
        info = self.info
        return sum(estimate(info, t, self.speeds) for t in self.tasks) if info else 0.0

    @property
    def longer_than_film(self) -> bool:
        return self.seconds > self.duration > 0

    @property
    def unknown(self) -> bool:
        """Whether an encode's time cannot be told: ffprobe gave no length or no size."""
        info = self.info
        encodes = any(t.encodes for t in self.tasks)
        return info is not None and encodes and (info.duration <= 0 or not size_known(info))

    @property
    def needs_asking(self) -> bool:
        return self.longer_than_film or self.unknown

    @property
    def transcodes(self) -> bool:
        """Whether the base version must be a transcode."""
        return any(t.kind == BASE and not t.copy for t in self.tasks)

    def to_dict(self) -> dict[str, Any]:
        info = self.info
        tasks = [] if info is None else [
            {"task": t.name, "kind": t.kind, "sub": t.sub,
             "seconds": round(estimate(info, t, self.speeds), 1)}
            for t in self.tasks
        ]  # fmt: skip
        return {
            "source": media.shown(self.source),
            "name": media.shown(Path(self.source).name),
            "duration": self.duration,
            "seconds": round(self.seconds, 1),
            "longer_than_film": self.longer_than_film,
            "unknown": self.unknown,
            "tasks": tasks,
            "notes": self.notes,
            "tracks": [] if info is None else _tracks(info),
        }


def _tracks(info: MediaInfo) -> list[dict[str, Any]]:
    """A video's tracks as the dashboard lists them before a prepare."""
    found: list[dict[str, Any]] = [
        {"kind": "audio", "number": a.number, "label": tracks.label(a), "treatment": "AAC"}
        for a in info.audio
    ]
    found += [
        {"kind": "subtitle", "number": s.number, "label": tracks.label(s),
         "treatment": s.treatment}
        for s in info.subtitles
    ]  # fmt: skip
    return found


def plan_video(
    runner: Runner,
    cache_dir: Path,
    source: str,
    spec: str | None,
    pending: set[Task],
    speeds: Speeds = DEFAULT_SPEEDS,
) -> VideoPlan:
    """Look at one video and plan what is still to make of it, leaving out what is made
    already and what is queued (`pending`). A file ffprobe cannot read gets a note."""
    entry = prepared.entry_dir(cache_dir, source)
    try:
        info = probe.probe(runner, source)
        todo, notes = plan(info, spec, prepared.versions(entry))
    except (ProbeError, PrepareError) as exc:
        return VideoPlan(source, entry, None, notes=[f"cannot be prepared: {exc}"], speeds=speeds)
    return VideoPlan(source, entry, info, [t for t in todo if t not in pending], notes, speeds)


# --- the jobs


@dataclass
class Job:
    id: int
    source: str
    entry: Path
    task: Task
    info: MediaInfo
    estimate: float
    state: str = QUEUED
    done_seconds: float = 0.0  # how far into the video the job got
    error: str = ""
    started: float = 0.0
    finished: float = 0.0
    cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def progress(self) -> float:
        if self.state == DONE:
            return 1.0
        return min(1.0, self.done_seconds / self.info.duration) if self.info.duration > 0 else 0.0

    def to_dict(self, now: float) -> dict[str, Any]:
        left = None
        if self.state == RUNNING:
            took = now - self.started
            # The estimate until there is progress to go by, then the speed so far.
            p = self.progress
            left = self.estimate - took if p < 0.02 else took / p - took
            left = max(0.0, left)
        elif self.state == QUEUED:
            left = self.estimate
        return {
            "id": self.id,
            "source": media.shown(self.source),
            "name": media.shown(Path(self.source).name),
            "task": self.task.name,
            "kind": self.task.kind,
            "sub": self.task.sub,
            "state": self.state,
            "progress": round(self.progress, 4),
            "estimate": round(self.estimate, 1),
            "seconds_left": None if left is None else round(left, 1),
            "error": self.error,
            "started": self.started,
            "finished": self.finished,
        }


def _fresh(folder: Path) -> None:
    """An empty folder, whatever was there (a part left by a run that was cut off)."""
    if folder.is_symlink() or folder.is_file():
        folder.unlink()
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(mode=0o750, parents=True)


def _install(part: Path, final: Path) -> None:
    """Put a finished version in place of an older one."""
    if final.exists():
        shutil.rmtree(final)
    os.replace(part, final)


def ffmpeg_errors(output: str, lines: int = 12) -> str:
    """The lines of ffmpeg's output that are not -progress reports."""
    said = [x for x in output.splitlines() if x.strip() and not _is_progress(x)]
    return "\n".join(said[-lines:]) or "no error message"


# The keys of ffmpeg's -progress reports.
_PROGRESS = re.compile(
    r"(?:frame|fps|stream_\d+_\d+_q|bitrate|total_size|out_time(?:_us|_ms)?|dup_frames"
    r"|drop_frames|speed|progress)="
)


def _is_progress(line: str) -> bool:
    return _PROGRESS.match(line) is not None


class Preparer:
    """Runs prepare jobs one after the other in a thread of the daemon."""

    def __init__(
        self,
        cache_dir: Path,
        runner: Runner,
        log: Callable[[str], None] = print,
        clock: Callable[[], float] = time.time,
        settings: ffargs.EncodeSettings = ffargs.DEFAULT_SETTINGS,
        speeds: Speeds = DEFAULT_SPEEDS,
    ) -> None:
        self.cache_dir = cache_dir
        self.runner = runner
        self.log = log
        self.clock = clock
        self.settings = settings
        self.speeds = speeds
        # Job ids count from 1 again in a new daemon; `run` tells the two apart.
        self.run = secrets.token_hex(4)
        self._ids = itertools.count(1)
        self._queue: collections.deque[Job] = collections.deque()
        self._current: Job | None = None
        self._history: collections.deque[Job] = collections.deque(maxlen=HISTORY)
        self._cond = threading.Condition()
        self._stop = False
        self._thread: threading.Thread | None = None
        self._filters: set[str] | None = None
        self._swept = False

    # --- what the daemon asks

    def submit(self, source: str, entry: Path, info: MediaInfo, todo: list[Task]) -> list[Job]:
        """Queue these tasks of one video. One that is queued or running already is not
        queued again. Returns the jobs that were added."""
        added = []
        with self._cond:
            pending = {(j.entry, j.task) for j in self._active()}
            for task in todo:
                if (entry, task) in pending:
                    continue
                seconds = estimate(info, task, self.speeds)
                job = Job(next(self._ids), source, entry, task, info, seconds)
                self._queue.append(job)
                added.append(job)
            if added:
                self._start_thread()
                self._cond.notify_all()
        return added

    def cancel(self, path: str | None = None) -> int:
        """Cancel the jobs of one file or of the files in a folder, or every job. Returns
        how many."""
        count = 0
        with self._cond:
            keep: collections.deque[Job] = collections.deque()
            for job in self._queue:
                if path is None or under(job.source, path):
                    self._finish(job, CANCELLED)
                    count += 1
                else:
                    keep.append(job)
            self._queue = keep
            current = self._current
            if current is not None and (path is None or under(current.source, path)):
                current.cancel.set()
                count += 1
        return count

    def take(self, entry: Path) -> Path | None:
        """Move a video's folder out of the way so it can be removed, unless a job of it is
        queued or running. Held under the lock no job starts or is queued without."""
        with self._cond:
            if any(j.entry == entry for j in self._active()):
                return None
            return prepared.take(entry)

    def active(self, entry: Path | None = None) -> bool:
        """Whether any job, or any job of one video's folder, is queued or running."""
        with self._cond:
            return any(entry is None or j.entry == entry for j in self._active())

    def pending(self, entry: Path) -> set[Task]:
        """The tasks of a video that are queued or running."""
        with self._cond:
            return {j.task for j in self._active() if j.entry == entry}

    def job_for(self, entry: Path) -> dict[str, Any] | None:
        """The job that makes the base version of a video, while it is queued or running."""
        with self._cond:
            for job in self._active():
                if job.entry == entry and job.task.kind == BASE:
                    return job.to_dict(self.clock())
        return None

    def status(self) -> list[dict[str, Any]]:
        """Every job, the running one first, then the queued, then the finished (newest
        first)."""
        now = self.clock()
        with self._cond:
            jobs = [*self._active(), *reversed(self._history)]
            return [job.to_dict(now) for job in jobs]

    def shutdown(self) -> None:
        """Stop the running job and drop the queued ones; wait for the worker to end."""
        with self._cond:
            self._stop = True
            for job in self._queue:
                self._finish(job, CANCELLED)
            self._queue.clear()
            if self._current is not None:
                self._current.cancel.set()
            self._cond.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=15)

    def wait(self, timeout: float = 30.0) -> bool:
        """Wait until no job is queued or running (for tests and the command line)."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._active():
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(min(left, 0.1))
        return True

    # --- the worker

    def _active(self) -> list[Job]:
        return [*([self._current] if self._current else []), *self._queue]

    def _finish(self, job: Job, state: str, error: str = "") -> None:
        job.state, job.error, job.finished = state, error, self.clock()
        self._history.append(job)

    def _start_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop = False
            self._thread = threading.Thread(target=self._work, name="prepare", daemon=True)
            self._thread.start()

    def _work(self) -> None:
        if not self._swept:
            self._swept = True
            with contextlib.suppress(OSError):
                prepared.sweep(self.cache_dir)
        while True:
            with self._cond:
                while not self._queue and not self._stop:
                    self._cond.wait()
                if self._stop:
                    return
                job = self._current = self._queue.popleft()
                job.state, job.started = RUNNING, self.clock()
            name = Path(job.source).name
            self.log(f"prepare: {media.shown(name)}: {job.task.name} started")
            state, error = DONE, ""
            try:
                self._run(job)
            except Cancelled:
                state = CANCELLED
            except (PrepareError, subtitles.SubtitleError, ToolMissing, OSError) as exc:
                state, error = FAILED, str(exc)
            except Exception as exc:  # a job that fails in a way not foreseen must not
                state, error = FAILED, f"unexpected: {exc!r}"  # end the worker
            with self._cond:
                self._current = None
                self._finish(job, state, error)
                self._cond.notify_all()
            took = job.finished - job.started
            self.log(
                f"prepare: {media.shown(name)}: {job.task.name} {state} after {took:.0f} s"
                + (f": {error}" if error else "")
            )

    def _run(self, job: Job) -> None:
        if job.cancel.is_set():
            raise Cancelled
        try:
            changed = source_key(job.source) != job.entry.name
        except OSError as exc:
            raise PrepareError(f"the file cannot be read any more: {exc}") from exc
        if changed:
            raise PrepareError("the file was changed after it was queued; prepare it again")
        job.entry.mkdir(mode=0o750, parents=True, exist_ok=True)
        self._meta(job)  # first, so the folder is never listed without its source
        if job.task.kind == BASE:
            self._base(job)
        elif job.task.kind == SOFT:
            self._soft(job)
        else:
            self._burn(job)

    def _meta(self, job: Job, base: str | None = None) -> None:
        """Write what the player needs to know of the source: always with a new base
        version, else only when there is none yet."""
        old = prepared.read_meta(job.entry)
        if base is None and old is not None:
            return
        kind = base if base is not None else str((old or {}).get("base") or "")
        prepared.write_meta(job.entry, prepared.meta_of(job.info, kind))

    def _room(self, job: Job) -> None:
        need = output_bytes(job.info, job.task, self.settings)
        if not has_room(job.entry, need):
            raise PrepareError(
                f"not enough free space in {self.cache_dir} (about {need / 1e9:.1f} GB needed)"
            )

    def _has_filters(self, *names: str) -> bool:
        if self._filters is None:
            self._filters = ffmpeg_names(self.runner, "filters")
        return set(names) <= self._filters

    def _ffmpeg(self, job: Job, args: list[str]) -> None:
        program = require(self.runner, "ffmpeg", "prepare")

        def seen(line: str) -> None:
            at = ffargs.progress_seconds(line)
            if at is not None:
                job.done_seconds = at

        full = ffargs.with_progress([program, *args[1:]])
        code, output = run_cancellable([*low_priority_prefix(self.runner), *full], job.cancel, seen)
        if code != 0:
            raise PrepareError(f"ffmpeg failed (exit {code}): {ffmpeg_errors(output)}")

    def _base(self, job: Job) -> None:
        info, v = job.info, job.info.video[0]
        final = job.entry / prepared.BASE
        part = prepared.part_of(final)
        self._room(job)
        _fresh(part)
        try:
            args = ffargs.base_hls(
                job.source,
                part,
                copy=job.task.copy,
                video_codec=v.codec,
                audio=[a.language for a in info.audio],
                default_audio=tracks.default_audio(info.audio),
                hdr=v.hdr,
                tonemap=v.hdr and self._has_filters("zscale", "tonemap"),
                settings=self.settings,
            )
            self._ffmpeg(job, args)
            if not prepared.master(part).is_file():
                raise PrepareError("ffmpeg wrote no playlist")
            _install(part, final)
        finally:
            shutil.rmtree(part, ignore_errors=True)
        self._meta(job, prepared.COPY if job.task.copy else prepared.TRANSCODE)

    def _track(self, job: Job) -> SubtitleTrack:
        number = job.task.sub
        if not 0 < number <= len(job.info.subtitles):
            raise PrepareError(f"this file has no subtitle track {number}")
        return job.info.subtitles[number - 1]

    def _soft(self, job: Job) -> None:
        track = self._track(job)
        out = prepared.soft_file(job.entry, track.number)
        out.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        subtitles.extract_webvtt(self.runner, job.source, track, out)
        # mkvextract keeps the file's own times; ffmpeg starts its output, and the video
        # of the HLS version, at 0.
        if track.reader == MKVEXTRACT and job.info.start_time > 0:
            text = out.read_text(encoding="utf-8", errors="replace")
            write_atomic(out, subtitles.shift_webvtt(text, job.info.start_time))

    def _burn(self, job: Job) -> None:
        track = self._track(job)
        base = prepared.master(job.entry / prepared.BASE)
        if not base.is_file():
            raise PrepareError("the base version is not there; it is made first")
        image = track.kind == "image"
        if not image and not self._has_filters("subtitles"):
            raise PrepareError("this ffmpeg has no subtitles filter (libass); see vpstream doctor")
        v = job.info.video[0]
        final = prepared.burn_dir(job.entry, track.number)
        part = prepared.part_of(final)
        work = job.entry / f"{prepared.WORK}-{track.number}"
        self._room(job)
        _fresh(part)
        _fresh(work)
        try:
            link = subtitles.filter_source(work, Path(job.source))
            args = ffargs.burn_hls(
                job.source,
                part,
                sub_number=track.number,
                image=image,
                filter_src=link,
                hdr=v.hdr,
                tonemap=v.hdr and self._has_filters("zscale", "tonemap"),
                settings=self.settings,
            )
            self._ffmpeg(job, args)
            own = prepared.master(part)
            if not own.is_file():
                raise PrepareError("ffmpeg wrote no playlist")
            try:
                text = prepared.burn_master(base.read_text(), own.read_text())
            except ValueError as exc:
                raise PrepareError(str(exc)) from exc
            write_atomic(own, text)
            final.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
            _install(part, final)
        finally:
            shutil.rmtree(part, ignore_errors=True)
            shutil.rmtree(work, ignore_errors=True)
