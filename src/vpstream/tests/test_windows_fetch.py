"""Downloads in parts (`platform.windows.fetch`), from a server on 127.0.0.1: parts when the
server sends ranges, one connection when it does not, a part cut off goes on from where it
was, the SHA-256 is of the whole file."""

from __future__ import annotations

import hashlib
import http.server
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

from vpstream.platform.common import SetupError
from vpstream.platform.windows import fetch

DATA = bytes(range(256)) * 400  # 102,400 bytes


class Files(http.server.BaseHTTPRequestHandler):
    ranges = True  # whether it sends parts
    cut: ClassVar[list[int]] = []  # starts of the ranges whose first answer stops half way
    dead: tuple[int, int] | None = None  # every answer starting in it sends nothing
    asked: ClassVar[list[str]] = []

    def log_message(self, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        wanted = re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers.get("Range") or "")
        self.asked.append(self.headers.get("Range") or "all")
        if not (wanted and self.ranges):
            self.send_response(200)
            self.send_header("Content-Length", str(len(DATA)))
            self.end_headers()
            self.wfile.write(DATA)
            return
        first, last = int(wanted[1]), min(int(wanted[2]), len(DATA) - 1)
        body = DATA[first : last + 1]
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {first}-{last}/{len(DATA)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.dead and self.dead[0] <= first <= self.dead[1]:
            self.close_connection = True  # the headers, then nothing
            return
        if first in self.cut:
            self.cut.remove(first)
            self.wfile.write(body[: len(body) // 2])  # and the connection ends
            self.close_connection = True
            return
        self.wfile.write(body)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setattr(fetch, "SMALLEST", 1000)
    monkeypatch.setattr(fetch, "PIECE", 4096)
    Files.ranges, Files.cut, Files.asked, Files.dead = True, [], [], None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Files)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/f.zip"
    httpd.shutdown()
    httpd.server_close()


def test_a_file_is_split_into_ranges() -> None:
    assert fetch.parts(10, 4) == [(0, 2), (3, 5), (6, 8), (9, 9)]
    assert fetch.parts(8, 4) == [(0, 1), (2, 3), (4, 5), (6, 7)]
    assert fetch.parts(3, 4) == [(0, 0), (1, 1), (2, 2)]


def test_a_server_that_sends_ranges_gives_the_file_in_parts(server: str, tmp_path: Path) -> None:
    said: list[tuple[int, int]] = []
    dest = tmp_path / "f.zip"
    digest = fetch.PartsHttp().download(
        server, dest, lambda done, total: said.append((done, total))
    )
    assert dest.read_bytes() == DATA and digest == hashlib.sha256(DATA).hexdigest()
    assert Files.asked[0] == "bytes=0-0"  # the size first
    assert sorted(Files.asked[1:]) == sorted(
        f"bytes={a}-{b}" for a, b in fetch.parts(len(DATA))
    )  # then the parts
    assert said[-1] == (len(DATA), len(DATA))


def test_a_server_without_ranges_gives_it_in_one(server: str, tmp_path: Path) -> None:
    Files.ranges = False
    dest = tmp_path / "f.zip"
    digest = fetch.PartsHttp().download(server, dest, lambda done, total: None)
    assert dest.read_bytes() == DATA and digest == hashlib.sha256(DATA).hexdigest()
    assert Files.asked == ["bytes=0-0", "all"]


def test_a_part_cut_off_goes_on_from_where_it_was(server: str, tmp_path: Path) -> None:
    second = fetch.parts(len(DATA))[1][0]
    Files.cut = [second]
    dest = tmp_path / "f.zip"
    digest = fetch.PartsHttp().download(server, dest, lambda done, total: None)
    assert dest.read_bytes() == DATA and digest == hashlib.sha256(DATA).hexdigest()
    again = [a for a in Files.asked if a.startswith("bytes=") and a != "bytes=0-0"]
    assert len(again) == fetch.PARTS + 1  # each, and the rest of the one cut off
    assert any(
        int(a[6:].split("-")[0]) > second
        for a in again
        if a.endswith(str(fetch.parts(len(DATA))[1][1]))
    )


def test_a_part_that_keeps_failing_says_so(server: str, tmp_path: Path) -> None:
    first, last = fetch.parts(len(DATA))[1]
    Files.dead = (first, last)  # every answer for it sends nothing
    with pytest.raises(SetupError, match=r"cannot download .*ended early"):
        fetch.PartsHttp().download(server, tmp_path / "f.zip", lambda done, total: None)
    within = [
        a
        for a in Files.asked
        if a.startswith("bytes=") and first <= int(a[6:].split("-")[0]) <= last
    ]
    assert len(within) == 1 + fetch.RETRIES  # tried, then again twice, then no more


def test_a_server_that_stops_sending_parts_gets_one_connection(
    server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = Files.do_GET

    def parts_then_whole(self: Files) -> None:
        if (self.headers.get("Range") or "") != "bytes=0-0":
            Files.ranges = False  # the probe said yes; the parts get the whole file
        real(self)

    monkeypatch.setattr(Files, "do_GET", parts_then_whole)
    dest = tmp_path / "f.zip"
    digest = fetch.PartsHttp().download(server, dest, lambda done, total: None)
    assert dest.read_bytes() == DATA and digest == hashlib.sha256(DATA).hexdigest()
    assert Files.asked[-1] == "all"  # in one, as ever


def test_a_server_that_refuses_the_probe_gets_one_connection(
    server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = Files.do_GET

    def refuse_ranges(self: Files) -> None:
        if self.headers.get("Range"):
            self.send_response(416)
            self.end_headers()
            return
        real(self)

    monkeypatch.setattr(Files, "do_GET", refuse_ranges)
    dest = tmp_path / "f.zip"
    assert fetch.PartsHttp().download(server, dest, lambda d, t: None) == (
        hashlib.sha256(DATA).hexdigest()
    )


# --- a host's addresses in a race


class Sock:
    """A socket whose connect to "dead" hangs (as an address that does not answer), to
    "refused" fails at once, and to any other succeeds."""

    made: ClassVar[list[Sock]] = []

    def __init__(self, family: int, kind: int, proto: int) -> None:
        if family == 10:  # AF_INET6 on a PC without it
            raise OSError(97, "Address family not supported by protocol")
        self.closed, self.addr = False, None
        Sock.made.append(self)

    def settimeout(self, value: float | None) -> None:
        self.timeout = value

    def connect(self, addr: tuple[str, int]) -> None:
        self.addr = addr
        if addr[0] == "dead":
            time.sleep(min(self.timeout or 30, 2.0))
            raise TimeoutError("timed out")
        if addr[0] == "refused":
            raise ConnectionRefusedError("refused")
        if addr[0] == "late":
            time.sleep(0.6)  # connects, but after the winner

    def close(self) -> None:
        self.closed = True


def addresses(*names: str) -> Any:
    return lambda host, port, type=0: [(2, 1, 6, "", (name, port)) for name in names]


@pytest.fixture
def socks(monkeypatch: pytest.MonkeyPatch) -> list[Sock]:
    Sock.made = []
    monkeypatch.setattr(fetch.socket, "socket", Sock)
    return Sock.made


def test_an_address_that_does_not_answer_does_not_hold_up_the_rest(socks: list[Sock]) -> None:
    started = time.monotonic()
    sock = fetch.first_connect(("h", 443), 30, resolve=addresses("dead", "good", "other"))
    assert sock.addr == ("good", 443) and time.monotonic() - started < 1.0  # not 21 s
    assert [s.addr[0] for s in socks if s.addr] == ["dead", "good"]  # the third not needed


def test_a_refused_address_goes_on_to_the_next_at_once(socks: list[Sock]) -> None:
    sock = fetch.first_connect(("h", 443), 30, resolve=addresses("refused", "good"))
    assert sock.addr == ("good", 443)


def test_when_no_address_answers_the_last_error_is_raised(socks: list[Sock]) -> None:
    with pytest.raises(ConnectionRefusedError):
        fetch.first_connect(("h", 443), 30, resolve=addresses("refused", "refused"))
    with pytest.raises(OSError, match="no address"):
        fetch.first_connect(("h", 443), 30, resolve=addresses())


def test_the_downloads_connect_through_the_race(
    server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raced: list[tuple[str, int]] = []
    real = fetch.first_connect

    def counted(address: tuple[str, int], *args: Any, **kw: Any) -> Any:
        raced.append(address)
        return real(address, *args, **kw)

    monkeypatch.setattr(fetch, "first_connect", counted)
    fetch.PartsHttp().download(server, tmp_path / "f.zip", lambda done, total: None)
    assert len(raced) == 1 + fetch.PARTS  # the probe and each part


def test_a_socket_that_connects_after_the_winner_is_closed(socks: list[Sock]) -> None:
    sock = fetch.first_connect(("h", 443), 30, resolve=addresses("late", "good"))
    assert sock.addr == ("good", 443)
    time.sleep(0.8)  # the late one connects meanwhile
    late = next(s for s in socks if s.addr == ("late", 443))
    assert late.closed and not sock.closed


def test_an_address_family_the_pc_lacks_fails_at_once(socks: list[Sock]) -> None:
    def v6(host: str, port: int, type: int = 0) -> list[Any]:
        return [(10, 1, 6, "", ("::1", port)), (2, 1, 6, "", ("refused", port))]

    started = time.monotonic()
    with pytest.raises(OSError):
        fetch.first_connect(("h", 443), 30, resolve=v6)
    assert time.monotonic() - started < 1.0  # not the 30 s timeout
