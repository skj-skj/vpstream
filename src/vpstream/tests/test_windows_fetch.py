"""Downloads in parts (`platform.windows.fetch`), from a server on 127.0.0.1: parts when the
server sends ranges, one connection when it does not, a part cut off goes on from where it
was, the SHA-256 is of the whole file."""

from __future__ import annotations

import hashlib
import http.server
import re
import threading
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
