from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from vpstream import config, state
from vpstream.control import Control, ControlError
from vpstream.stream import StreamError
from vpstream.tests.fakes import FakeFirewall, FakeServer


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Rig:
    """A Control with fakes around it, and the things a test looks at."""

    def __init__(self, root: Path, fw: FakeFirewall | None, server: FakeServer | None) -> None:
        self.fw, self.server, self.clock = fw, server, Clock()
        self.log: list[str] = []
        self.paths = config.Paths.under(root)
        self.film = root / "film.mkv"
        self.film.write_bytes(b"video")
        self.control = self.again()

    def again(self, boot: str = "boot-1") -> Control:
        """A new Control over the same files, as after a restart of the daemon."""
        self.control = Control(
            config.Config(stream_host="192.0.2.9"),
            self.paths,
            self.fw,
            self.server,
            self.log.append,
            self.clock,
            boot,
        )
        return self.control

    def start(self, **options: Any) -> dict[str, Any]:
        return self.control.start({"file": str(self.film), "alias": "aaa", **options})

    def recorded(self) -> dict[str, Any] | None:
        return state.load(self.paths.state_dir / state.FILE_NAME)[0].stream


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path, FakeFirewall(), FakeServer())


# --- start and stop


def test_start_runs_caddy_before_it_opens_the_port(rig: Rig) -> None:
    order: list[str] = []
    rig.server.start = lambda: order.append("caddy")  # type: ignore[method-assign,union-attr]
    rig.fw.open = lambda: order.append("port")  # type: ignore[method-assign,union-attr]
    info = rig.start()
    assert order == ["caddy", "port"]
    assert info["stream"]["url"] == "http://192.0.2.9:1337/aaa"
    assert info["stream_port_state"] == "open"
    assert rig.recorded()["alias"] == "aaa" and rig.recorded()["boot"] == "boot-1"  # type: ignore[index]
    assert rig.log == [f"stream /aaa started: {rig.film} (open)"]


def test_stop_closes_the_port_before_it_stops_caddy_and_forgets_the_devices(rig: Rig) -> None:
    rig.start()
    rig.control.set_device("192.0.2.1", True)
    info = rig.control.stop()
    assert rig.fw.calls == ["open", "close"] and rig.server.calls == ["start", "stop"]  # type: ignore[union-attr]
    assert info["stream"] is None and info["stream_port_state"] == "closed"
    assert rig.recorded() is None
    assert "stopped by request" in rig.log[-1] and "is closed" in rig.log[-1]
    assert rig.start()["stream"]["devices"] == []  # approvals do not outlive the stream


def test_only_one_stream_at_a_time(rig: Rig) -> None:
    rig.start()
    with pytest.raises(StreamError, match=r"already running .*/aaa"):
        rig.start(alias="bbb")
    assert rig.fw.calls == ["open"]  # type: ignore[union-attr]


def test_changes_need_a_running_stream(rig: Rig) -> None:
    for change in (
        rig.control.stop,
        rig.control.unlock,
        lambda: rig.control.set_alias("bbb"),
        lambda: rig.control.set_policy("locked"),
        lambda: rig.control.set_device("192.0.2.1", True),
    ):
        with pytest.raises(StreamError, match="no stream is running"):
            change()
    assert rig.fw.calls == [] and rig.server.calls == []  # type: ignore[union-attr]


def test_a_bad_request_touches_nothing(rig: Rig) -> None:
    with pytest.raises(StreamError, match="not a file"):
        rig.control.start({"file": "/no/such.mkv"})
    assert rig.fw.calls == [] and rig.server.calls == []  # type: ignore[union-attr]
    assert rig.control.stream is None


def test_when_caddy_does_not_start_the_port_stays_closed(tmp_path: Path) -> None:
    rig = Rig(tmp_path, FakeFirewall(), FakeServer(fail="exit 1: bad config"))
    with pytest.raises(ControlError, match="Caddy could not be started: exit 1"):
        rig.start()
    assert rig.fw.calls == [] and rig.control.stream is None and rig.recorded() is None  # type: ignore[union-attr]


def test_when_the_port_does_not_open_caddy_is_stopped_again(tmp_path: Path) -> None:
    rig = Rig(tmp_path, FakeFirewall(fail="sudo: a password is required"), FakeServer())
    with pytest.raises(ControlError, match="password is required"):
        rig.start()
    assert rig.server.calls == ["start", "stop"]  # type: ignore[union-attr]
    assert rig.control.stream is None and rig.recorded() is None
    assert "password" in rig.control.status()["firewall_problem"]


def test_a_port_that_does_not_close_is_reported_and_the_stream_still_stops(rig: Rig) -> None:
    rig.start()
    rig.fw.fail = "ufw failed"  # type: ignore[union-attr]
    info = rig.control.stop()
    assert rig.server.calls == ["start", "stop"]  # type: ignore[union-attr]
    assert info["stream"] is None and info["firewall_problem"] == "ufw failed"
    assert info["stream_port_state"] == "unknown"
    assert "may still be open" in rig.log[-1]
    rig.fw.fail = ""  # type: ignore[union-attr]
    rig.start()
    assert rig.control.status()["firewall_problem"] is None  # a success clears the warning


def test_without_a_public_server_nothing_starts(tmp_path: Path) -> None:
    rig = Rig(tmp_path, FakeFirewall(), None)
    with pytest.raises(ControlError, match="no public server"):
        rig.start()
    assert rig.fw.calls == []  # type: ignore[union-attr]


def test_a_trial_run_without_a_firewall_helper_still_streams(tmp_path: Path) -> None:
    rig = Rig(tmp_path, None, FakeServer())
    assert rig.start()["stream_port_state"] == "unknown"
    rig.control.stop()
    assert "no firewall helper" in rig.log[-1]


# --- changes while it runs


def test_alias_change_applies_at_once_and_is_recorded(rig: Rig) -> None:
    rig.start()
    assert rig.control.visit("aaa", "192.0.2.1", "x", False).status == 200
    info = rig.control.set_alias("Movie-Night")
    assert info["stream"]["url"] == "http://192.0.2.9:1337/movie-night"
    assert rig.control.visit("aaa", "192.0.2.1", "x", False).status == 404
    assert rig.control.visit("movie-night", "192.0.2.1", "x", False).status == 200
    assert rig.recorded()["alias"] == "movie-night"  # type: ignore[index]
    with pytest.raises(StreamError, match="reserved"):
        rig.control.set_alias("caps")
    assert rig.control.status()["stream"]["alias"] == "movie-night"


def test_lock_unlock_and_devices(rig: Rig) -> None:
    rig.start()
    rig.control.visit("aaa", "192.0.2.1", "vlc", False)
    assert rig.control.set_policy("locked")["stream"]["policy"] == "locked"
    assert rig.control.visit("aaa", "192.0.2.2", "x", False).status == 403
    rig.control.set_device("192.0.2.2", True)
    assert rig.control.visit("aaa", "192.0.2.2", "x", False).status == 200
    rig.control.set_device("192.0.2.1", False)
    assert rig.control.visit("aaa", "192.0.2.1", "vlc", False).status == 403
    assert rig.control.unlock()["stream"]["policy"] == "open"
    assert {d["ip"]: d["allowed"] for d in rig.recorded()["devices"]} == {  # type: ignore[index]
        "192.0.2.1": False,
        "192.0.2.2": True,
    }


@pytest.mark.parametrize(
    ("ip", "allowed", "message"),
    [
        ("192.0.2.1", "yes", "needs an ip"),
        (None, True, "needs an ip"),
        ("not-an-ip", True, "not an IP address"),
    ],
)
def test_bad_device_changes(rig: Rig, ip: Any, allowed: Any, message: str) -> None:
    rig.start()
    with pytest.raises(ValueError, match=message):
        rig.control.set_device(ip, allowed)


def test_no_stream_means_nothing_is_found(rig: Rig) -> None:
    assert rig.control.visit("aaa", "192.0.2.1", "x", True).status == 404


def test_the_address_falls_back_to_a_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vpstream import net

    monkeypatch.setattr(net, "outgoing_address", lambda: "")
    control = Control(config.Config(), config.Paths.under(tmp_path))
    assert control.base_url() == "http://<server-ip>:1337"
    monkeypatch.setattr(net, "outgoing_address", lambda: "2001:db8::5")
    control = Control(config.Config(stream_port=4000), config.Paths.under(tmp_path))
    assert control.base_url() == "http://[2001:db8::5]:4000"


# --- the clock


def test_the_time_limit_stops_the_stream(rig: Rig) -> None:
    rig.start(ttl=10)
    rig.clock.now += 599
    rig.control.tick()
    assert rig.control.status()["stream"]["seconds_left"] == 1.0
    rig.clock.now += 1
    rig.control.tick()
    assert rig.control.stream is None and rig.fw.calls == ["open", "close"]  # type: ignore[union-attr]
    assert "time limit is over" in rig.log[-1]
    rig.control.tick()  # nothing left to do
    assert rig.fw.calls == ["open", "close"]  # type: ignore[union-attr]


def test_no_time_limit(rig: Rig) -> None:
    rig.start(ttl=0)
    rig.clock.now += 10**9
    rig.control.tick()
    assert rig.control.stream is not None


def test_a_caddy_that_died_ends_the_stream_and_closes_the_port(rig: Rig) -> None:
    rig.start()
    rig.server.up = False  # type: ignore[union-attr]
    rig.control.tick()
    assert rig.control.stream is None and rig.fw.calls == ["open", "close"]  # type: ignore[union-attr]
    assert "Caddy stopped unexpectedly" in rig.log[-1]


def test_tick_counts_what_the_kernel_says_was_sent(tmp_path: Path) -> None:
    from vpstream.tests.fakes import FakeRunner
    from vpstream.tests.test_traffic import ss

    rig = Rig(tmp_path, FakeFirewall(), FakeServer())
    runner = FakeRunner(("ss",))
    rig.control.runner = runner
    rig.start()
    rig.control.visit("aaa", "192.0.2.1", "vlc", False)
    runner.on("ss", stdout=ss(("[::ffff:192.0.2.1]", 2898, 470_000_000)))
    rig.control.tick()
    (device,) = rig.control.status()["stream"]["devices"]
    assert device["bytes"] == 470_000_000


def test_tick_counts_the_bytes_from_the_access_log(rig: Rig) -> None:
    rig.start()
    rig.control.visit("aaa", "192.0.2.1", "vlc", False)
    rig.paths.caddy_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        {"request": {"remote_ip": "192.0.2.1"}, "size": 700},
        {"request": {"remote_ip": "192.0.2.77"}, "size": 5},  # never asked for the alias
        {"request": {"remote_ip": "192.0.2.1"}, "size": 300},
    ]
    rig.paths.access_log.write_text("".join(json.dumps(line) + "\n" for line in lines))
    rig.control.tick()
    rig.control.tick()
    (device,) = rig.control.status()["stream"]["devices"]
    assert device["bytes"] == 1000


# --- when the daemon starts and stops


def test_a_stream_is_taken_up_again_after_a_crash(rig: Rig) -> None:
    rig.start(ttl=60)
    rig.control.set_device("192.0.2.1", False)
    rig.fw.calls.clear(), rig.server.calls.clear()  # type: ignore[union-attr]
    rig.clock.now += 100
    control = rig.again()
    control.reconcile()
    assert rig.server.calls == ["start"] and rig.fw.calls == ["open"]  # type: ignore[union-attr]
    info = control.status()
    assert info["stream"]["alias"] == "aaa" and info["stream"]["seconds_left"] == 3500.0
    assert info["stream_port_state"] == "open"
    assert control.visit("aaa", "192.0.2.1", "x", False).status == 403  # still disabled
    assert "running again" in rig.log[-1]


def test_a_stream_is_not_carried_over_a_reboot(rig: Rig) -> None:
    rig.start()
    rig.fw.calls.clear(), rig.server.calls.clear()  # type: ignore[union-attr]
    control = rig.again(boot="boot-2")
    control.reconcile()
    assert rig.fw.calls == ["close"] and rig.server.calls == []  # type: ignore[union-attr]
    assert control.stream is None and rig.recorded() is None
    assert "before a reboot" in rig.log[-2]


def test_a_stream_whose_time_ran_out_while_the_daemon_was_down_stays_stopped(rig: Rig) -> None:
    rig.start(ttl=1)
    rig.fw.calls.clear()  # type: ignore[union-attr]
    rig.clock.now += 60
    control = rig.again()
    control.reconcile()
    assert rig.fw.calls == ["close"] and control.stream is None and rig.recorded() is None  # type: ignore[union-attr]


def test_a_stream_that_cannot_be_taken_up_again_leaves_the_port_closed(rig: Rig) -> None:
    rig.start()
    rig.fw.calls.clear()  # type: ignore[union-attr]
    rig.server.fail = "port 1337 is already used by another program"  # type: ignore[union-attr]
    control = rig.again()
    control.reconcile()
    assert rig.fw.calls == ["close"] and control.stream is None and rig.recorded() is None  # type: ignore[union-attr]
    assert any("could not be started again" in line for line in rig.log)


def test_shutdown_stops_the_stream(rig: Rig) -> None:
    rig.control.shutdown()
    assert rig.fw.calls == []  # type: ignore[union-attr]  # nothing ran: the service closes the port
    rig.start()
    rig.control.shutdown()
    assert rig.fw.calls == ["open", "close"] and rig.server.calls == ["start", "stop"]  # type: ignore[union-attr]
    assert rig.recorded() is None and "the daemon is stopping" in rig.log[-1]


# --- found in the review of milestone 2


def test_status_and_viewers_do_not_wait_for_a_slow_firewall_or_caddy(rig: Rig) -> None:
    import threading

    entered, release = threading.Event(), threading.Event()

    def slow_open() -> None:
        entered.set()
        assert release.wait(10)

    rig.fw.open = slow_open  # type: ignore[method-assign,union-attr]
    starting = threading.Thread(target=rig.start, daemon=True)
    starting.start()
    assert entered.wait(5)
    answered = threading.Event()

    def ask() -> None:
        assert rig.control.status()["stream"] is None  # not running until the port is open
        assert rig.control.visit("aaa", "192.0.2.1", "x", False).status == 404
        rig.control.tick()
        answered.set()

    threading.Thread(target=ask, daemon=True).start()
    try:
        assert answered.wait(2), "status waited for the firewall helper"
    finally:
        release.set()
        starting.join(5)
    assert rig.control.status()["stream"]["alias"] == "aaa"


def test_a_second_start_waits_its_turn_and_is_refused(rig: Rig) -> None:
    import threading

    entered, release = threading.Event(), threading.Event()
    rig.fw.open = lambda: (entered.set(), release.wait(10))  # type: ignore[method-assign,union-attr]
    first = threading.Thread(target=rig.start, daemon=True)
    first.start()
    assert entered.wait(5)
    errors: list[Exception] = []

    def again() -> None:
        try:
            rig.start(alias="bbb")
        except StreamError as exc:
            errors.append(exc)

    second = threading.Thread(target=again, daemon=True)
    second.start()
    release.set()
    first.join(5), second.join(5)
    assert len(errors) == 1 and "already running" in str(errors[0])
    assert rig.server.calls == ["start"]  # type: ignore[union-attr]


def test_a_caddy_that_cannot_be_stopped_does_not_keep_the_stream_or_loop(rig: Rig) -> None:
    rig.start()
    rig.server.up = False  # type: ignore[union-attr]

    def broken() -> None:
        raise OSError("cannot remove the log")

    rig.server.stop = broken  # type: ignore[method-assign,union-attr]
    for _ in range(3):
        rig.control.tick()
    assert rig.control.stream is None and rig.recorded() is None
    assert rig.fw.calls == ["open", "close"]  # type: ignore[union-attr]  # once, not every second
    assert any("stopping Caddy failed" in line for line in rig.log)


def test_a_record_that_cannot_be_rewritten_is_removed_so_no_crash_reopens_the_port(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.start()

    def full(path: Any, value: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(state, "save", full)
    info = rig.control.stop()
    assert info["stream"] is None and rig.fw.calls == ["open", "close"]  # type: ignore[union-attr]
    assert not (rig.paths.state_dir / state.FILE_NAME).exists()
    assert any("recording that no stream runs failed" in line for line in rig.log)
    monkeypatch.undo()
    control = rig.again()
    control.reconcile()
    assert control.stream is None


def test_a_start_that_cannot_be_recorded_is_taken_back(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full(path: Any, value: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(state, "save", full)
    with pytest.raises(ControlError, match="disk full"):
        rig.start()
    assert rig.control.stream is None
    assert rig.fw.calls == ["open", "close"] and rig.server.calls == ["start", "stop"]  # type: ignore[union-attr]


def test_a_port_that_may_have_opened_is_closed_again(tmp_path: Path) -> None:
    rig = Rig(tmp_path, FakeFirewall(fail="stopped: no result after 60 seconds"), FakeServer())
    with pytest.raises(ControlError, match="no result"):
        rig.start()
    assert rig.fw.calls == ["open", "close"]  # type: ignore[union-attr]


def test_without_a_boot_id_a_stream_is_never_taken_up_again(rig: Rig) -> None:
    rig.again(boot="")
    rig.start()
    rig.fw.calls.clear()  # type: ignore[union-attr]
    control = rig.again(boot="")
    control.reconcile()
    assert control.stream is None and rig.fw.calls == ["close"]  # type: ignore[union-attr]


def test_the_address_is_looked_up_again_until_there_is_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vpstream import net

    monkeypatch.setattr(net, "outgoing_address", lambda: "")
    control = Control(config.Config(), config.Paths.under(tmp_path))
    assert control.base_url() == "http://<server-ip>:1337"
    monkeypatch.setattr(net, "outgoing_address", lambda: "192.0.2.5")
    assert control.base_url() == "http://192.0.2.5:1337"
