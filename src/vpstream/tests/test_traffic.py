from __future__ import annotations

import json
from pathlib import Path

import pytest

from vpstream import traffic
from vpstream.tests.fakes import FakeRunner


def ss(*conns: tuple[str, int, int | None]) -> str:
    """What `ss -tinH state established` prints for these (peer, port, bytes acked)."""
    out = []
    for peer, port, acked in conns:
        out.append(f"0      0      203.0.113.9:1337     {peer}:{port}")
        detail = "cubic wscale:7,7 rto:204 rtt:80/5 mss:1448 cwnd:10"
        out.append(f"\t {detail}" + (f" bytes_sent:{acked} bytes_acked:{acked}" if acked else ""))
    return "\n".join(out) + "\n"


def test_parse() -> None:
    text = ss(("[::ffff:192.0.2.1]", 2908, 470_000_000), ("192.0.2.2", 5, None))
    text += ss(("[2001:db8::7]", 443, 12))
    assert traffic.parse(text) == {
        ("192.0.2.1", "2908"): 470_000_000,
        ("192.0.2.2", "5"): 0,
        ("2001:db8::7", "443"): 12,
    }
    assert traffic.parse("") == {}
    assert traffic.parse("garbage\n\t bytes_acked:5\nnot:an:address x\n") == {}
    lone = "0 0 203.0.113.9:1337 192.0.2.1:5\n"  # no detail line after it
    assert traffic.parse(lone) == {}


def test_ss_is_asked_about_the_stream_port_only() -> None:
    assert traffic.ss_args(1337)[-1] == "( sport = :1337 )"
    assert traffic.ss_args(1337)[:6] == ["ss", "-t", "-i", "-n", "-H", "state"]


class Rig:
    def __init__(self, tmp_path: Path, programs: tuple[str, ...] = ("ss",)) -> None:
        self.runner = FakeRunner(programs)
        self.log = tmp_path / "access.log"
        self.now = 0.0
        self.traffic = traffic.Traffic(self.runner, 1337, self.log, clock=lambda: self.now)

    def kernel(self, *conns: tuple[str, int, int]) -> None:
        self.runner.on("ss", stdout=ss(*conns))

    def logged(self, ip: str, port: int, size: int) -> None:
        with self.log.open("a") as fh:
            line = {"request": {"remote_ip": ip, "remote_port": str(port)}, "size": size}
            fh.write(json.dumps(line) + "\n")

    def read(self) -> dict[str, int]:
        self.now += 1
        out: dict[str, int] = {}
        for ip, size in self.traffic.read():
            out[ip] = out.get(ip, 0) + size
        return out


def test_a_long_transfer_is_counted_by_the_kernel_even_when_the_log_says_512(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rig.kernel(("192.0.2.1", 2898, 1_000_000))
    assert rig.read() == {"192.0.2.1": 1_000_000}  # what was sent before the first look counts
    rig.kernel(("192.0.2.1", 2898, 5_000_000))
    assert rig.read() == {"192.0.2.1": 4_000_000}
    rig.kernel()  # the player hung up
    rig.logged("192.0.2.1", 2898, 512)  # what Caddy 2.6.2 writes for it
    assert rig.read() == {}
    assert rig.runner.calls[0] == traffic.ss_args(1337)


def test_a_connection_too_short_to_be_seen_is_counted_from_the_log(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.kernel()
    rig.logged("192.0.2.1", 4000, 600_000)
    assert rig.read() == {"192.0.2.1": 600_000}


def test_a_request_logged_before_its_connection_was_seen_is_not_counted_twice(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rig.kernel()
    rig.logged("192.0.2.1", 4000, 600_000)  # done before the first look; kept open after
    assert rig.read() == {"192.0.2.1": 600_000}
    rig.kernel(("192.0.2.1", 4000, 2_600_000))  # the next request on the same connection
    assert rig.read() == {"192.0.2.1": 2_000_000}
    rig.logged("192.0.2.1", 4000, 2_000_000)  # its log line, counted by the kernel already
    rig.kernel()
    assert rig.read() == {}


def test_a_port_used_again_by_a_new_connection_starts_from_zero(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.kernel(("192.0.2.1", 2894, 9_000_000))
    rig.read()
    rig.kernel(("192.0.2.1", 2894, 300))  # the old one closed, a new one got the same port
    assert rig.read() == {"192.0.2.1": 300}


def test_several_viewers_and_connections(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.kernel(("192.0.2.1", 1, 100), ("192.0.2.1", 2, 200), ("[::ffff:192.0.2.2]", 3, 50))
    assert rig.read() == {"192.0.2.1": 300, "192.0.2.2": 50}


def test_without_ss_the_log_counts_everything(tmp_path: Path) -> None:
    rig = Rig(tmp_path, programs=())
    rig.logged("192.0.2.1", 5, 23_475_788)  # Caddy 2.11 counts a cut-off transfer right
    assert rig.read() == {"192.0.2.1": 23_475_788}
    assert rig.runner.calls == [] and not rig.traffic.kernel


def test_an_ss_that_fails_is_not_asked_again(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.runner.on("ss", returncode=1, stderr="Cannot open netlink socket: Permission denied")
    rig.logged("192.0.2.1", 5, 10)
    assert rig.read() == {"192.0.2.1": 10}
    rig.read()
    assert len(rig.runner.calls) == 1 and not rig.traffic.kernel


def test_seen_connections_are_forgotten_after_a_while(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(traffic, "SEEN_SECONDS", 3)
    rig = Rig(tmp_path)
    rig.kernel(("192.0.2.1", 7, 10))
    rig.read()
    rig.kernel()
    for _ in range(4):
        rig.read()
    rig.logged("192.0.2.1", 7, 99)  # long after: a new connection on that port
    assert rig.read() == {"192.0.2.1": 99}
