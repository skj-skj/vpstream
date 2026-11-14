"""Starting, stopping and changing the stream: the one place that drives Caddy, the firewall
and the state file. The command line and the dashboard both end up here."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vpstream import (
    clients,
    gate,
    mkv,
    net,
    player,
    prepare,
    prepared,
    reindex,
    state,
    text,
    tracks,
)
from vpstream.caddy import CaddyError, Server, link_video, link_videos, unlink_videos
from vpstream.checks import is_whole
from vpstream.config import Config, Paths
from vpstream.files import under
from vpstream.firewall import Firewall, FirewallError
from vpstream.media import shown
from vpstream.stream import (
    BROWSER,
    DIRECT,
    MISSING,
    SAVE_NOW,
    Answer,
    NeedsConfirm,
    Stream,
    StreamError,
    check_alias,
    check_choice,
    source_of,
    videos_of,
)
from vpstream.tools import NO_PROGRAMS, Runner, ToolMissing
from vpstream.traffic import Traffic

BOOT_ID = "/proc/sys/kernel/random/boot_id"
# Where players are is written down at most this often; a new cookie or a used start
# position on the next tick, and what the owner changes at once.
SAVE_SECONDS = 10.0
NOW_SECONDS = 1.0  # what must be written at once is, at most this often


class ControlError(RuntimeError):
    """The host did not do what the stream needs (Caddy, the firewall)."""


def boot_id() -> str:
    """A name for this boot of the machine: a stream is never carried over a reboot."""
    try:
        with open(BOOT_ID, encoding="ascii") as fh:
            return fh.read().strip()
    except OSError:
        return ""


@dataclass
class Control:
    config: Config
    paths: Paths
    firewall: Firewall | None = None
    server: Server | None = None
    log: Callable[[str], None] = print
    clock: Callable[[], float] = time.time
    boot: str = field(default_factory=boot_id)
    stream: Stream | None = None
    port_state: str = "unknown"  # "closed", "open" or "unknown"
    firewall_problem: str | None = None
    # Two locks, so that a slow Caddy or firewall helper never holds up a viewer or a status
    # request: `_op` is held for a whole start, stop or resume; `_lock` only while the
    # stream record is read or changed.
    _op: threading.RLock = field(default_factory=threading.RLock)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    runner: Runner | None = None  # for `ss`, mkvmerge and ffprobe
    preparer: prepare.Preparer | None = None  # makes the versions browser mode plays
    _traffic: Traffic | None = None
    _indexer: threading.Thread | None = None
    _cancel: threading.Event = field(default_factory=threading.Event)
    _host: str = ""
    _save_by: float | None = None  # when what viewers changed must be written down
    _saved_at: float = 0.0

    # --- the pieces

    @property
    def _state_file(self) -> Path:
        return self.paths.state_dir / state.FILE_NAME

    def _save(self) -> None:
        recorded = None if self.stream is None else {**self.stream.to_dict(), "boot": self.boot}
        state.save(self._state_file, state.State(stream=recorded))
        self._save_by, self._saved_at = None, self.clock()

    def _save_due(self, how: str) -> None:
        """Note that viewers changed the stream. SAVE_NOW is written down at once, or on
        the next tick when the last write was less than a second ago (however many ask, it
        is at most once a second); SAVE_LATER within SAVE_SECONDS. Called with `_lock`
        held."""
        now = self.clock()
        by = now + (0.0 if how == SAVE_NOW else SAVE_SECONDS)
        self._save_by = by if self._save_by is None else min(self._save_by, by)
        if how == SAVE_NOW and now - self._saved_at >= NOW_SECONDS:
            self._save_soon()

    def _save_soon(self) -> None:
        """Write down what viewers changed, when it is due. Called with `_lock` held."""
        if self._save_by is None or self.clock() < self._save_by:
            return
        try:
            self._save()
        except OSError as exc:
            self._save_by = self.clock() + SAVE_SECONDS  # tried; not again at once
            self.log(f"WARNING: recording the stream failed: {exc}")

    def _port(self, action: str) -> None:
        """Open or close the stream port. Without a helper (a trial run) nothing is done."""
        if self.firewall is None:
            return
        try:
            self.firewall.open() if action == "open" else self.firewall.close()
        except FirewallError as exc:
            self.port_state = "unknown"
            self.firewall_problem = str(exc)
            raise
        self.port_state = "open" if action == "open" else "closed"
        self.firewall_problem = None

    def base_url(self) -> str:
        if not self._host:  # asked again while there is no route yet
            self._host = net.public_host(self.config.stream_host)
        return net.url("http", self._host, self.config.stream_port)

    def _running(self) -> Stream:
        if self.stream is None:
            raise StreamError("no stream is running")
        return self.stream

    def _serve(self, stream: Stream) -> None:
        """Start Caddy, then open the port: the port is never open with nothing behind it
        that asks the gate. Called with `_op` held; viewers get a 404 until it is done."""
        if self.server is None:
            raise ControlError("this daemon has no public server to start")
        if stream.mode == BROWSER:
            links: list[str | None] = list(stream.entries)  # folders; Caddy sends files in them
        else:
            links = [None if n in stream.indexing else v for n, v in enumerate(stream.served, 1)]
        try:
            link_videos(self.paths.videos_dir, links)
            self.server.start()
        except (CaddyError, ToolMissing, OSError) as exc:
            unlink_videos(self.paths.videos_dir)
            raise ControlError(f"Caddy could not be started: {exc}") from exc
        try:
            self._port("open")
            with self._lock:
                self.stream = stream
                self._traffic = Traffic(
                    self.runner or NO_PROGRAMS, self.config.stream_port, self.paths.access_log
                )
                self._save()
        except (FirewallError, OSError) as exc:
            # A helper that was cut off may have opened the port all the same.
            self._undo()
            raise ControlError(str(exc)) from exc

    def _undo(self) -> None:
        """Take back whatever a start or a stop left: nothing of it may fail the caller."""
        with self._lock:
            self.stream = self._traffic = None
        steps: tuple[tuple[str, Callable[[], None]], ...] = (
            ("closing the stream port", lambda: self._port("close")),
            ("stopping Caddy", self._stop_server),
            ("recording that no stream runs", self._forget),
        )
        for what, step in steps:
            try:
                step()
            except (FirewallError, OSError) as exc:
                self.log(f"WARNING: {what} failed: {exc}")

    def _stop_server(self) -> None:
        unlink_videos(self.paths.videos_dir)
        if self.server is not None:
            self.server.stop()

    def _forget(self) -> None:
        try:
            with self._lock:
                self._save()
        except OSError:
            # A record that stays would open the port again after a crash.
            self._state_file.unlink(missing_ok=True)
            raise

    def _end(self, why: str) -> None:
        """Forget the stream and its approvals, so the gate refuses at once; then close the
        port and stop Caddy. Called with `_op` held. Prepares go on: what they make is kept."""
        self._stop_indexing()
        self._undo()
        port = self.config.stream_port
        if self.firewall_problem:
            port_now = "may still be open"
        else:
            port_now = "is closed" if self.firewall else "was not opened (no firewall helper)"
        self.log(f"the stream is stopped ({why}); stream port {port} {port_now}")

    # --- the seek index of MKV files (see `mkv`)

    def _plan_index(self, stream: Stream, numbers: Any) -> None:
        """Serve a copy with a proper index for each of these videos that lacks one: the
        copy made before, or one made now (`indexing`). Without mkvmerge, the video itself."""
        can_fix = bool(self.runner and self.runner.which(reindex.TOOL))
        for n in numbers:
            source = stream.videos[n - 1]
            stream.indexing.discard(n)
            problem = mkv.index_problem(source)
            if problem is None:
                continue
            copy = reindex.ready(self.paths.cache_dir, source)
            if copy is not None:
                stream.served[n - 1] = str(copy.resolve())
            elif can_fix:
                stream.indexing.add(n)
                self.log(f"video {n}: {problem}; making a copy with a full index")
            else:
                stream.notes[n] = f"{problem}, so VLC resumes it slowly (install mkvtoolnix)"

    def _start_indexing(self, stream: Stream) -> None:
        if not stream.indexing or self.runner is None:
            return
        self._cancel = threading.Event()
        self._indexer = threading.Thread(
            target=self._index, args=(stream, self.runner, self._cancel), daemon=True
        )
        self._indexer.start()

    def _stop_indexing(self) -> None:
        self._cancel.set()
        if self._indexer is not None:
            self._indexer.join(timeout=15)
            self._indexer = None

    def _index(self, stream: Stream, runner: Runner, cancel: threading.Event) -> None:
        """Make the copies one after the other. Each is served as soon as it is ready."""
        for n in sorted(stream.indexing):
            source = stream.videos[n - 1]
            started = time.monotonic()
            note = None
            try:
                out = reindex.copy_path(self.paths.cache_dir, source)
                if not out.is_file():  # the same file may be in the folder twice
                    reindex.make(runner, source, out, cancel)
                served = str(out.resolve())
            except reindex.Cancelled:
                return
            except (reindex.ReindexError, ToolMissing, OSError) as exc:
                served, note = source, f"VLC resumes it slowly: the copy failed: {exc}"
            with self._lock:
                if self.stream is not stream or cancel.is_set():
                    return
                stream.served[n - 1] = served
                stream.indexing.discard(n)
                if note:
                    stream.notes[n] = note
                try:
                    link_video(self.paths.videos_dir, n, served)
                    self._save()
                except OSError as exc:
                    self.log(f"WARNING: video {n}: {exc}")
            took = time.monotonic() - started
            self.log(f"video {n}: {note or f'copy with a full index ready ({took:.0f} s)'}")

    # --- when the daemon starts and stops

    def reconcile(self) -> None:
        """Bring the host in line with the recorded state when the daemon starts.

        A stream that was running when the daemon crashed is taken up again, if its time is
        not over and the machine has not rebooted. Otherwise the stream port is closed: an
        open rule survives a crash or a reboot.
        """
        with self._op:
            recorded, problem = state.load(self._state_file)
            if problem:
                self.log(f"{problem}; treating it as no stream running")
            if recorded.stream is not None and self._resume(recorded.stream):
                return
            port = self.config.stream_port
            if recorded.stream is not None:
                self._forget()
            if self.firewall is None:
                self.log(f"no firewall helper; stream port {port} was not checked")
                return
            try:
                self._port("close")
            except FirewallError as exc:
                self.log(f"WARNING: {exc}")
                return
            self.log(f"stream port {port} is closed (no stream is running)")

    def _resume(self, recorded: dict[str, Any]) -> bool:
        try:
            stream = Stream.from_dict(recorded)
        except StreamError as exc:
            self.log(f"{exc}; treating it as no stream running")
            return False
        if not self.boot or recorded.get("boot") != self.boot:
            self.log("the recorded stream is from before a reboot; it is not started again")
            return False
        if stream.expires is not None and stream.expires <= self.clock():
            self.log("the recorded stream's time limit is over; it is not started again")
            return False
        if stream.mode == DIRECT:
            self._plan_index(stream, sorted(stream.indexing))
        try:
            plans = self._resumed_plans(stream)
            self._serve(stream)
        except (ControlError, StreamError) as exc:
            self.log(f"WARNING: the recorded stream could not be started again: {exc}")
            return False
        self._submit(plans)
        self._start_indexing(stream)
        self.log(f"the stream /{stream.alias} is running again after a restart of the daemon")
        return True

    def shutdown(self) -> None:
        """The daemon is told to stop: the stream and the prepares stop with it."""
        with self._op:
            if self.stream is not None:
                self._end("the daemon is stopping")
        if self.preparer is not None:
            self.preparer.shutdown()

    def tick(self) -> None:
        """Called every second or so: count the bytes sent, and stop the stream when its
        time is over or Caddy is gone."""
        with self._lock:
            stream = self.stream
            if stream is None:
                return
            if self._traffic is not None:
                for ip, size in self._traffic.read():
                    stream.devices.add_bytes(ip, size)
            self._save_soon()
            # The wall clock, because the time limit must hold across a restart of the
            # daemon; a clock that is set while a stream runs moves its end with it.
            if stream.expires is not None and stream.expires <= self.clock():
                why = "its time limit is over"
            elif self.server is not None and not self.server.running():
                why = "Caddy stopped unexpectedly"
            else:
                return
        with self._op:
            if self.stream is stream:  # not stopped by someone else in between
                self._end(why)

    # --- what the command line and the dashboard ask for

    def start(self, options: dict[str, Any]) -> dict[str, Any]:
        with self._op:
            if self.stream is not None:
                raise StreamError(
                    f"a stream is already running (/{self.stream.alias}); stop it first"
                )
            stream = Stream.create(options, self.clock())
            plans: list[prepare.VideoPlan] = []
            if stream.mode == BROWSER:
                plans = self._browser(stream, options.get("confirmed") is True)
            else:
                self._plan_index(stream, range(1, len(stream.videos) + 1))
            self._serve(stream)
            self.log(f"stream /{stream.alias} started: {stream.source} ({stream.policy})")
            jobs = self._submit(plans)
            self._start_indexing(stream)
            return {**self.status(), "jobs": jobs}

    def stop(self) -> dict[str, Any]:
        with self._op:
            self._running()
            self._end("stopped by request")
            return self.status()

    def _change(self, change: Callable[[Stream], Any]) -> dict[str, Any]:
        with self._lock:
            change(self._running())
            self._save()
            return self.status()

    def set_alias(self, alias: Any) -> dict[str, Any]:
        new = check_alias(alias)
        return self._change(lambda s: setattr(s, "alias", new))

    def set_policy(self, policy: Any) -> dict[str, Any]:
        return self._change(lambda s: s.set_policy(policy))

    def unlock(self) -> dict[str, Any]:
        return self._change(lambda s: s.unlock())

    def set_device(self, device: Any, allowed: Any) -> dict[str, Any]:
        if not isinstance(device, str) or not isinstance(allowed, bool):
            raise StreamError(
                "a device change needs a device (its id or IP address) and allowed (true or false)"
            )
        return self._change(lambda s: s.devices.decide(device, allowed, self.clock()))

    def set_clients(self, options: dict[str, Any]) -> dict[str, Any]:
        """Change the browsers of a browser stream: `devices` (ids) or `all`, and any of
        `audio`, `sub`, `seek` (commands to their players), `start_at` (where the next load
        of `video`, by default the one each is on, starts) and `name`."""
        known = {"devices", "all", "audio", "sub", "seek", "start_at", "video", "name"}
        if extra := sorted(set(options) - known):
            raise StreamError(f"unknown option: {', '.join(extra)}")
        try:
            values = {
                key: read(options[key])
                for key, read in _CLIENT_VALUES.items()
                if options.get(key) is not None
            }
        except clients.ClientError as exc:
            raise StreamError(str(exc)) from exc
        if not values:
            raise StreamError("say what to change: audio, sub, seek, start_at or name")
        if options.get("video") is not None and "start_at" not in values:
            raise StreamError("a video goes with start_at: it says which video starts there")

        def change(stream: Stream) -> None:
            if stream.mode != BROWSER and set(values) - {"name"}:
                raise StreamError("players are told what to do in browser mode only")
            video = options.get("video")
            if video is not None and not (is_whole(video) and 0 < video <= len(stream.videos)):
                raise StreamError(f"not a video of this stream: {video!r}")
            for device in _chosen(stream, options.get("devices"), options.get("all") is True):
                _apply(device, values, video, self.clock())

        return self._change(change)

    # --- what viewers ask for

    def visit(
        self,
        path: str,
        ip: str,
        user_agent: str,
        wants_html: bool,
        cookie: str | None = None,
        query: dict[str, str] | None = None,
    ) -> Answer:
        with self._lock:
            stream = self.stream
            if stream is None:
                return MISSING
            answer = stream.visit(path, ip, user_agent, wants_html, self.clock(), cookie, query)
            if answer.saves:
                self._save_due(answer.saves)
        if answer.kind == "notready" and self.preparer is not None:
            job = self.preparer.job_for(Path(stream.entries[answer.number - 1]))
            if job is not None:
                return Answer(503, "preparing", number=answer.number, data=job)
        return answer

    def status(self) -> dict[str, Any]:
        with self._lock:
            now = self.clock()
            stream = self.stream
            described = None if stream is None else stream.describe(self.base_url(), now)
            info = {
                "now": now,
                "stream": described,
                "stream_port": self.config.stream_port,
                "stream_port_state": self.port_state,
                "firewall_problem": self.firewall_problem,
                "prepare": [] if self.preparer is None else self.preparer.status(),
                "prepare_run": None if self.preparer is None else self.preparer.run,
            }
        if described is not None and stream is not None and stream.mode == BROWSER:
            used = _videos_in_use(stream)
            for video, entry in zip(described["videos"], stream.entries, strict=True):
                video["job"] = self.preparer.job_for(Path(entry)) if self.preparer else None
                # Read from the cache on every poll, so only for the videos browsers are on.
                video["menu"] = player.menu(Path(entry)) if video["number"] in used else None
        return info

    # --- browser mode and its prepared versions (see `prepare`)

    def _preparer(self) -> prepare.Preparer:
        if self.preparer is None or self.runner is None:
            raise ControlError("this daemon cannot prepare videos")
        return self.preparer

    def _plans(self, videos: list[str], spec: str | None) -> list[prepare.VideoPlan]:
        preparer = self._preparer()
        assert self.runner is not None
        plans = []
        for video in videos:
            try:
                entry = prepared.entry_dir(self.paths.cache_dir, video)
                plan = prepare.plan_video(
                    self.runner,
                    self.paths.cache_dir,
                    video,
                    spec,
                    preparer.pending(entry),
                    preparer.speeds,
                )
            except ToolMissing as exc:
                raise ControlError(str(exc)) from exc
            except OSError as exc:
                raise StreamError(f"cannot look at {shown(video)}: {exc}") from exc
            plans.append(plan)
        return plans

    def _submit(self, plans: list[prepare.VideoPlan]) -> list[int]:
        """Queue what the plans still need. Returns the ids of the new jobs."""
        ids = []
        for plan in plans:
            if plan.info is not None and plan.tasks:
                added = self._preparer().submit(plan.source, plan.entry, plan.info, plan.tasks)
                ids += [job.id for job in added]
        return ids

    def _missing_bases(self, stream: Stream) -> list[prepare.VideoPlan]:
        """Plans for the videos of a browser stream that have no base version yet: the base
        and every plain text subtitle track (seconds of work)."""
        if stream.mode != BROWSER:
            return []
        missing = [v for n, v in enumerate(stream.videos, 1) if not stream.ready(n)]
        return self._plans(missing, None) if missing else []

    def _place(self, stream: Stream) -> None:
        """Where each video's versions are: found again after a restart, because a video
        that was changed since has a new folder."""
        try:
            stream.entries = [
                str(prepared.entry_dir(self.paths.cache_dir, v)) for v in stream.videos
            ]
        except OSError as exc:
            raise StreamError(f"cannot look at the videos: {exc}") from exc

    def _resumed_plans(self, stream: Stream) -> list[prepare.VideoPlan]:
        """What a browser stream taken up after a restart still needs. The jobs were lost
        with the daemon; only copies are queued again, since a transcode may have been
        cancelled on purpose."""
        if stream.mode != BROWSER:
            return []
        self._place(stream)
        plans = self._missing_bases(stream)
        for plan in plans:
            if plan.transcodes:
                self.log(
                    f"{shown(Path(plan.source).name)} is not transcoded yet; make it with "
                    f"vpstream prepare --file {shown(plan.source)}"
                )
        return [p for p in plans if not p.transcodes]

    def _browser(self, stream: Stream, confirmed: bool) -> list[prepare.VideoPlan]:
        """Get a browser stream ready to start: where each video's versions go, and what is
        still to make. A copy is made by itself; a transcode only once it is confirmed."""
        self._place(stream)
        plans = self._missing_bases(stream)
        slow = [p for p in plans if p.transcodes]
        if slow and not confirmed:
            names = ", ".join(
                f"{shown(Path(p.source).name)} (about {text.duration(p.seconds)})" for p in slow[:5]
            )
            more = f" and {len(slow) - 5} more" if len(slow) > 5 else ""
            raise NeedsConfirm(
                f"{len(slow)} of the videos must be transcoded to H.264 before a browser can "
                f"play them, a full encode each: {names}{more}",
                [p.to_dict() for p in slow],
            )
        if stream.sub:
            self._check_sub(stream, plans)
        return plans

    def _check_sub(self, stream: Stream, plans: list[prepare.VideoPlan]) -> None:
        """The starting subtitles must be prepared, or about to be, for the video, or for at
        least one video of a folder (episodes differ; the others start without)."""
        choice = tracks.parse_choice(stream.sub or tracks.OFF)
        if choice is None:
            return
        planned = {p.source: p for p in plans}
        for video, folder in zip(stream.videos, stream.entries, strict=True):
            entry = Path(folder)
            have = prepared.versions(entry)
            plan = planned.get(video)
            soon = {t.sub for t in plan.tasks if t.kind == prepare.SOFT} if plan else set()
            made = set(have.soft) | set(have.burn) | soon
            info = plan.info if plan else None
            known: list[tracks.Numbered] = list(
                info.subtitles if info else prepared.meta_tracks(entry, "subtitles")
            )
            if any(t.number in made for t in tracks.matching(known, choice)):
                return
        which = "this video" if stream.kind == "file" else "any video in the folder"
        raise StreamError(
            f"subtitle {stream.sub} is not prepared for {which}. Prepare it first: "
            f"vpstream prepare --{stream.kind} {shown(stream.source)} --sub {stream.sub}"
        )

    def prepare(self, options: dict[str, Any]) -> dict[str, Any]:
        """Plan and queue the versions of a file or a folder's videos. A plan that takes
        longer than the film, or whose time cannot be told, is refused until it is sent again
        with `confirmed`. With `dry_run` it is only planned."""
        if extra := sorted(set(options) - {"file", "dir", "sub", "confirmed", "dry_run"}):
            raise StreamError(f"unknown option: {', '.join(extra)}")
        kind, source = source_of(options)
        spec = check_choice("sub", options.get("sub"), many=True)
        plans = self._plans(videos_of(kind, Path(source)), spec)
        shown_plans = [p.to_dict() for p in plans]
        if options.get("dry_run") is True:
            return {"plans": shown_plans, "jobs": []}
        long = [p for p in plans if p.needs_asking]
        if long and options.get("confirmed") is not True:
            raise NeedsConfirm(
                f"preparing takes longer than the film itself, or a time that cannot be "
                f"told, for {len(long)} of the videos",
                shown_plans,
            )
        return {"plans": shown_plans, "jobs": self._submit(plans)}

    def cancel_prepare(self, options: dict[str, Any]) -> dict[str, Any]:
        """Cancel the prepares of a file or of the files in a folder (`path`), or all."""
        return {"cancelled": self._preparer().cancel(_path_option(options))}

    def prepared(self) -> list[dict[str, Any]]:
        """Every video in the cache, and whether a stream or a prepare uses it now."""
        with self._lock:
            stream = self.stream
            used = set(stream.entries) if stream is not None else set()
        rows = []
        for entry in prepared.entries(self.paths.cache_dir):
            folder = prepared.root(self.paths.cache_dir) / entry.key
            busy = self.preparer is not None and self.preparer.active(folder)
            rows.append({**entry.to_dict(), "in_use": str(folder) in used, "busy": busy})
        return rows

    def remove_prepared(self, options: dict[str, Any]) -> dict[str, Any]:
        """Remove the prepared versions of a file or a folder's files (`path`), the stale
        ones (`stale`, made from files that are gone or changed) or all of them (`all`). A
        video the running stream plays, or one being prepared, is kept."""
        path = _path_option(options)
        stale, every = options.get("stale") is True, options.get("all") is True
        if (path is not None) + stale + every != 1:
            raise StreamError("give one of: a path, stale or all")
        removed, kept = 0, []
        # `_op` keeps a start from taking a folder up meanwhile, and `take` a prepare.
        with self._op:
            for row in self.prepared():
                wanted = every or (stale and row["stale"]) or (path and under(row["source"], path))
                if not wanted:
                    continue
                folder = prepared.root(self.paths.cache_dir) / row["key"]
                trash = None if row["in_use"] else self._take(folder)
                if trash is None:
                    kept.append(row["source"])
                    continue
                prepared.remove(trash)
                removed += 1
        return {"removed": removed, "kept": kept}

    def _take(self, folder: Path) -> Path | None:
        """Move a video's folder out of the way to remove it, unless a prepare uses it."""
        if self.preparer is not None:
            return self.preparer.take(folder)
        return prepared.take(folder)


_CLIENT_VALUES: dict[str, Callable[[Any], Any]] = {
    "audio": clients.audio_value,
    "sub": clients.sub_value,
    "seek": clients.seconds_value,
    "start_at": clients.seconds_value,
    "name": clients.name_value,
}


def _videos_in_use(stream: Stream) -> set[int]:
    """The videos the dashboard offers tracks of: those browsers are on or will start,
    and the first, for a browser that has not played anything yet."""
    used = {1}
    for device in stream.devices.browsers():
        if device.client is not None:
            used.add(device.client.video or 1)
            if device.client.start_at is not None:
                used.add(device.client.start_at[0])
    return used


def _chosen(stream: Stream, ids: Any, every: bool) -> list[gate.Device]:
    """The browsers a client change is for: those named, or with `every` all that get in."""
    if every:
        found = [d for d in stream.devices.browsers() if gate.allows(stream.policy, d.allowed)]
        if not found:
            raise StreamError("no browser watches this stream yet")
        return found
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise StreamError("name the devices to change (their ids), or give all")
    chosen = []
    for key in dict.fromkeys(ids):
        device = stream.devices.get(key)
        if device is None or device.kind != gate.BROWSER:
            raise StreamError(f"not a browser of this stream: {key!r}")
        chosen.append(device)
    return chosen


def _apply(device: gate.Device, values: dict[str, Any], video: int | None, now: float) -> None:
    """One client change on one browser."""
    if "name" in values:
        device.name = values["name"]
    if not set(values) - {"name"}:
        return
    client = device.client = device.client or clients.Client()
    for kind in clients.COMMANDS:
        if kind in values:
            client.command(kind, values[kind], now)
    if "start_at" in values:
        client.set_start(video or client.video or 1, values["start_at"])


def _path_option(options: dict[str, Any]) -> str | None:
    """The `path` of a request about prepares, which must be a full path, or None."""
    path = options.get("path")
    if path is not None and not (isinstance(path, str) and path.startswith("/")):
        raise StreamError(f"give the full path of a file or a folder: {path!r}")
    return path
