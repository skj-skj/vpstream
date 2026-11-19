"""Downloads in parts, for the programs vpstream installs on Windows.

GitHub's release files come at one pace a connection, slow on some lines: measured on a
home line, 0.2 MB/s with one, 0.9 with four, 1.7 with eight. A server that
answers ranges (`206`, a `Content-Range` with the size) gets PARTS connections, each
writing its own part of the file; any other, or a small file, is fetched as ever in one.
The SHA-256 is of the whole file, after, as `UrlHttp.download` gives it. Each part asks
the first address again (not the one it was sent on to), since GitHub's signed addresses
do not last.

Each connection tries the server's addresses in a race (`first_connect`), as browsers do:
GitHub's download host has four, and one that does not answer from a line made every
request wait out Windows' 21 s before Python tried the next.
"""

from __future__ import annotations

import hashlib
import http.client
import queue
import re
import socket
import ssl
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vpstream import __version__
from vpstream.platform.common import SetupError, UrlHttp

PARTS = 8
SMALLEST = 8_000_000  # smaller files come in one: parts would gain nothing
RETRIES = 2  # a part that is cut off goes on from where it was, this many times
PIECE = 1 << 20
SMALL_PIECE = 64 << 10  # read at a time: a slow line still says often how far it is
TICK = 0.25  # how often the progress is said while the parts run
STAGGER = 0.25  # the next address is tried when the one before has not answered by then


def first_connect(
    address: tuple[str, int],
    timeout: float | None = None,
    source_address: tuple[str, int] | None = None,
    *,
    resolve: Callable[..., list[Any]] = socket.getaddrinfo,
) -> socket.socket:
    """`socket.create_connection`, but trying the addresses of the host in a race: the next
    one STAGGER after the last if it has not connected yet; the first to connect is used,
    the others are closed. One address that does not answer no longer holds up the rest."""
    host, port = address
    infos = resolve(host, port, type=socket.SOCK_STREAM)
    if not infos:
        raise OSError(f"no address for {host}")
    answers: queue.Queue[socket.socket | OSError] = queue.Queue()
    lock = threading.Lock()
    over = [False]  # the race is decided (or lost): a socket that connects now is closed

    def attempt(family: int, kind: int, proto: int, sockaddr: Any) -> None:
        sock = None
        try:
            sock = socket.socket(family, kind, proto)  # an address family the PC lacks fails
            sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
        except OSError as exc:
            if sock is not None:
                sock.close()
            answers.put(exc)
            return
        with lock:
            if over[0]:
                sock.close()
                return
            answers.put(sock)

    def end() -> None:
        """The race is over: close every socket that connected but was not taken."""
        with lock:
            over[0] = True
        while True:
            try:
                late = answers.get_nowait()
            except queue.Empty:
                return
            if isinstance(late, socket.socket):
                late.close()

    started, failed = 0, 0
    try:
        while True:
            if started < len(infos):
                family, kind, proto, _, sockaddr = infos[started]
                args = (family, kind, proto, sockaddr)
                threading.Thread(target=attempt, args=args, daemon=True).start()
                started += 1
            wait = STAGGER if started < len(infos) else timeout
            try:
                found = answers.get(timeout=wait)
            except queue.Empty:
                if started < len(infos):
                    continue
                raise TimeoutError(f"cannot connect to {host}: no address answered") from None
            if isinstance(found, socket.socket):
                return found
            failed += 1
            if failed == len(infos):
                raise found
    finally:
        end()


class _RacingHTTPS(http.client.HTTPSConnection):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = first_connect  # set by __init__ itself: after it


class _RacingHTTP(http.client.HTTPConnection):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = first_connect


class _RacingHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, context: ssl.SSLContext | None) -> None:
        super().__init__(context=context)
        self.racing_context = context

    def https_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(_RacingHTTPS, req, context=self.racing_context)


class _RacingHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(_RacingHTTP, req)


def parts(total: int, count: int = PARTS) -> list[tuple[int, int]]:
    """`total` bytes as `count` ranges (first, last), the last byte included."""
    step = -(-total // count)
    return [(start, min(start + step, total) - 1) for start in range(0, total, step)]


class PartsHttp(UrlHttp):
    def __init__(self, context: ssl.SSLContext | None = None) -> None:
        self.context = context  # the certificates trusted (certifi's: windows.tls_context)
        self.opener = urllib.request.build_opener(
            _RacingHTTPSHandler(context), _RacingHTTPHandler()
        )

    def _urlopen(self, request: urllib.request.Request) -> Any:
        return self.opener.open(request, timeout=self.timeout)

    def _range(self, url: str, first: int, last: int) -> Any:
        headers = {"User-Agent": f"vpstream/{__version__}", "Range": f"bytes={first}-{last}"}
        request = urllib.request.Request(url, headers=headers)
        try:
            return self._urlopen(request)
        except urllib.error.HTTPError as exc:
            raise SetupError(f"{url}: HTTP {exc.code} {exc.reason}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise SetupError(f"cannot download {url}: {getattr(exc, 'reason', exc)}") from exc

    def size_in_parts(self, url: str) -> int:
        """The size of `url` when its server sends parts of it; 0 when it does not (or
        refuses to be asked so: some answer 416 or 403)."""
        try:
            with self._range(url, 0, 0) as resp:
                said = resp.headers.get("Content-Range") or ""
                found = re.fullmatch(r"bytes 0-0/(\d+)", said)
                return int(found[1]) if resp.status == 206 and found else 0
        except SetupError:
            return 0

    def download(self, url: str, dest: Path, progress: Callable[[int, int], None]) -> str:
        total = self.size_in_parts(url)
        if total >= SMALLEST:
            try:
                return self._in_parts(url, dest, progress, total)
            except NoRanges:
                pass  # a server that says it sends parts, then does not: in one, as ever
        return self._in_one(url, dest, progress)

    def _in_one(self, url: str, dest: Path, progress: Callable[[int, int], None]) -> str:
        """In one connection, in small pieces, so a slow line still says how far it is."""
        digest = hashlib.sha256()
        with self._open(url) as resp, dest.open("wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while chunk := resp.read(SMALL_PIECE):
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                progress(done, total)
        return digest.hexdigest()

    def _in_parts(
        self, url: str, dest: Path, progress: Callable[[int, int], None], total: int
    ) -> str:
        with dest.open("wb") as out:
            out.truncate(total)
        ranges = parts(total)
        got = [0] * len(ranges)
        problems: list[BaseException] = []
        stop = threading.Event()  # one part gave up: the others stop too

        def fetch(at: int, first: int, last: int) -> None:
            tries = 0
            try:
                while first + got[at] <= last and not stop.is_set():
                    start = first + got[at]
                    try:
                        with self._range(url, start, last) as resp, dest.open("r+b") as out:
                            said = resp.headers.get("Content-Range") or ""
                            if resp.status != 206 or not said.startswith(f"bytes {start}-"):
                                raise NoRanges(f"{url} did not send bytes {start}-{last}")
                            out.seek(start)
                            while not stop.is_set() and (chunk := resp.read(SMALL_PIECE)):
                                chunk = chunk[: last - (first + got[at]) + 1]
                                out.write(chunk)
                                got[at] += len(chunk)
                                tries = 0  # it moves: a cut later gets its tries again
                                if first + got[at] > last:
                                    break
                        if first + got[at] <= last and not stop.is_set():
                            raise SetupError(f"{url}: the connection ended early")
                    except NoRanges:
                        raise  # no use asking again: in one connection instead
                    except (SetupError, OSError, http.client.HTTPException):
                        tries += 1
                        if tries > RETRIES:
                            raise
            except BaseException as exc:  # whatever it is, the download hears it
                problems.append(exc)
                stop.set()

        threads = [
            threading.Thread(target=fetch, args=(at, *bounds), name=f"part-{at}", daemon=True)
            for at, bounds in enumerate(ranges)
        ]
        for thread in threads:
            thread.start()
        while any(thread.is_alive() for thread in threads):
            progress(sum(got), total)
            next((t for t in threads if t.is_alive()), threads[0]).join(TICK)
        progress(sum(got), total)
        if any(isinstance(p, NoRanges) for p in problems):
            raise next(p for p in problems if isinstance(p, NoRanges))
        if problems:
            raise SetupError(f"cannot download {url}: {problems[0]}")
        if sum(got) != total:
            raise SetupError(f"cannot download {url}: {sum(got)} of {total} bytes came")
        digest = hashlib.sha256()
        with dest.open("rb") as done:
            while chunk := done.read(PIECE):
                digest.update(chunk)
        return digest.hexdigest()


class NoRanges(SetupError):
    """The server sent a whole file, or another part, where a part was asked for."""
