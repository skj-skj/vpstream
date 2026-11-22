"""What a stream may take of the host: bounded on Termux, as before on Linux."""

from __future__ import annotations

from pathlib import Path

import pytest

from vpstream import caddy, config, daemon, ffargs
from vpstream.tests.fakes import FakeRunner


def _proxy(cfg: dict) -> dict:  # type: ignore[type-arg]
    route = cfg["apps"]["http"]["servers"]["vpstream"]["routes"][-1]
    return route["handle"][0]  # type: ignore[no-any-return]


def test_termux_has_bounds_by_default(on_termux: None) -> None:
    cfg = config.parse("")
    assert cfg.gate_connections == 8 and cfg.caddy_cpus == 2
    assert cfg.caddy_memory_mb == 192 and cfg.prepare_threads == 4
    mine = config.parse("[limits]\ngate_connections = 4\nprepare_threads = 0\n")
    assert (mine.gate_connections, mine.prepare_threads, mine.caddy_cpus) == (4, 0, 2)
    with pytest.raises(config.ConfigError, match=r"from 0 .no bound. to 4096"):
        config.parse("[limits]\ncaddy_cpus = -1\n")


def test_linux_bounds_only_the_gate_and_has_no_limits_section() -> None:
    cfg = config.parse("")
    # The gate's socket takes 16 waiting dials; Caddy keeps to that, the others wait.
    assert (cfg.gate_connections, cfg.caddy_cpus, cfg.caddy_memory_mb, cfg.prepare_threads) == (
        16,
        0,
        0,
        0,
    )
    with pytest.raises(config.ConfigError, match=r"unknown setting 'limits\.caddy_cpus'"):
        config.parse("[limits]\ncaddy_cpus = 2\n")


def test_caddy_asks_the_gate_at_most_so_often(tmp_path: Path) -> None:
    paths = config.Paths.under(tmp_path)
    server = caddy.CaddyServer(FakeRunner(), 1337, paths)
    assert "transport" not in _proxy(server.config())  # Linux: as before
    server.gate_connections = 8
    transport = _proxy(server.config())["transport"]
    assert transport == {
        "protocol": "http",
        "max_conns_per_host": 8,
        "keep_alive": {"max_idle_conns_per_host": 8},
    }
    # The waits of players watching together are held long: they never count against it.
    sync = server.config()["apps"]["http"]["servers"]["vpstream"]["routes"][1]
    assert sync["match"] == [{"path": ["/*/*/sync"]}]
    assert "transport" not in sync["handle"][0]


def test_caddy_gets_go_bounds_only_when_set(tmp_path: Path) -> None:
    server = caddy.CaddyServer(FakeRunner(), 1337, config.Paths.under(tmp_path))
    assert server.limits_env() == {}
    server.cpus, server.memory_mb = 2, 192
    assert server.limits_env() == {"GOMAXPROCS": "2", "GOMEMLIMIT": "192MiB"}


def test_ffmpeg_threads_only_when_set() -> None:
    assert "-threads" not in ffargs.h264_video_args(ffargs.DEFAULT_SETTINGS)
    args = ffargs.h264_video_args(ffargs.EncodeSettings(threads=4))
    # Among the output's options, where ffmpeg 7 and later read -filter_threads.
    assert args[-4:] == ["-threads", "4", "-filter_threads", "4"]


def test_the_daemon_passes_the_bounds_on(tmp_path: Path) -> None:
    paths = config.Paths.under(tmp_path)
    linux = daemon.Daemon(config.Config(), paths, FakeRunner())
    assert linux.control.preparer is not None
    assert linux.control.preparer.settings is ffargs.DEFAULT_SETTINGS
    bounded = daemon.Daemon(config.Config(prepare_threads=4), paths, FakeRunner())
    assert bounded.control.preparer is not None
    assert bounded.control.preparer.settings.threads == 4


def test_the_bound_holds_the_decoder_and_the_filters_too() -> None:
    """`-threads` after the input bounds the encoder alone; the decoder and the filter
    graph (the HDR tone mapping) start a thread per core unless told before it."""
    assert ffargs.thread_args(ffargs.DEFAULT_SETTINGS) == []
    assert ffargs.thread_args(ffargs.EncodeSettings(threads=4)) == [
        "-threads", "4", "-filter_complex_threads", "4",
    ]  # fmt: skip


def test_a_prepare_runs_ffmpeg_with_the_bound_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vpstream import prepare

    ran: list[list[str]] = []
    monkeypatch.setattr(
        prepare, "run_cancellable", lambda args, *a, **k: (ran.append(args), (0, ""))[1]
    )
    runner = FakeRunner(("ffmpeg",))
    for threads in (0, 4):
        settings = ffargs.EncodeSettings(threads=threads)
        preparer = prepare.Preparer(tmp_path, runner, lambda m: None, settings=settings)
        job = type("Job", (), {"cancel": None, "done_seconds": 0})()
        preparer._ffmpeg(job, ["ffmpeg", "-i", "in.mkv", "out"])  # type: ignore[arg-type]
    plain, bounded = ran
    assert "-threads" not in plain  # as before, where there is no bound
    assert (
        bounded.index("-threads") < bounded.index("-filter_complex_threads") < bounded.index("-i")
    )
