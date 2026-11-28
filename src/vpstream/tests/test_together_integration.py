"""Watch together through the real Caddy, as a Linux server has it: the waits the daemon
holds (control.HOLD_SECONDS) over plain HTTP, and over HTTPS with HTTP/2, many of them on
one connection; every held wait answered at once while videos are fetched; and two players'
pages, a press of one and the wait of the other, over HTTPS with a certificate from Pebble.
The daemon runs in this process, so a test may shorten the cooldown."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from vpstream import control, together
from vpstream.tests.test_https_integration import (  # noqa: F401  (fixtures)
    HOST,
    Pebble,
    _issued,
    kept,
    pebble,
)
from vpstream.tests.test_stream_integration import (  # noqa: F401  (a fixture)
    HTML,
    Live,
    _cookie,
    _wait_prepared,
    live,
)

pytestmark = pytest.mark.integration

PLAYER = {"X-Vpstream-Player": "1"}  # as player.js sends a report or a wait


@pytest.fixture(autouse=True)
def _tools() -> None:
    for tool in ("caddy", "ffmpeg"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} is not installed on this host")


def _start(run: Live, sample: Path) -> None:
    run.api("prepare", {"file": str(sample), "confirmed": True})
    _wait_prepared(run)
    start = {"file": str(sample), "alias": "aaa", "mode": "browser", "together": True}
    run.api("stream/start", start)


def _let_in(run: Live) -> dict[str, str]:
    """A new browser, approved by the owner: the cookie it sends from then on."""
    status, headers, _ = run.get("/aaa", headers=HTML)
    assert status == 403
    cookie = {"Cookie": _cookie(headers)}
    (waiting,) = [d for d in run.api("status")["stream"]["devices"] if d["allowed"] is None]
    run.api("devices", {"device": waiting["id"], "allowed": True})
    return cookie


def _page(run: Live, browser: dict[str, str]) -> dict[str, Any]:
    """The player page's settings, as the browser gets them."""
    status, _, body = run.get("/aaa", headers={**HTML, **browser})
    assert status == 200
    settings = body.decode().split('type="application/json">')[1].split("</script>")[0]
    return dict(json.loads(settings))


def _ask(run: Live, browser: dict[str, str], target: str) -> dict[str, Any]:
    wait = control.HOLD_SECONDS + 10  # a wait is held that long
    status, _, body = run.get(target, headers={**browser, **PLAYER}, timeout=wait)
    assert status == 200, (target, status, body)
    return dict(json.loads(body))


def _rev(run: Live, browser: dict[str, str]) -> int:
    return int(_ask(run, browser, "/aaa/1/client?pg=look0000&t=0")["together"]["rev"])


def test_held_waits_last_their_whole_hold_over_plain_http(
    live: Live,  # noqa: F811
    sample: Path,
) -> None:
    """Each wait on a connection of its own, as browsers over HTTP/1.1 have them."""
    _start(live, sample)
    browsers = [_let_in(live) for _ in range(control.MAX_HOLDS // control.HOLDS_PER_DEVICE)]
    rev = _rev(live, browsers[0])

    def wait(n: int) -> tuple[float, dict[str, Any]]:
        began = time.monotonic()
        browser = browsers[n // control.HOLDS_PER_DEVICE]
        answer = _ask(live, browser, f"/aaa/1/sync?pg=wait{n:04d}&rev={rev}")
        return time.monotonic() - began, answer

    with ThreadPoolExecutor(control.MAX_HOLDS) as pool:
        answers = list(pool.map(wait, range(control.MAX_HOLDS)))
    for took, answer in answers:
        assert answer["waited"] >= control.HOLD_SECONDS - 0.5
        assert took < control.HOLD_SECONDS + 5 and answer["together"]["rev"] == rev


def _curl_h2(run: Live, transfers: list[tuple[dict[str, str], str]]) -> list[dict[str, Any]]:
    """Requests at once with curl over HTTP/2, which multiplexes them on one connection."""
    args = ["curl", "-sS", "--parallel", "--parallel-max", "64"]
    for n, (headers, target) in enumerate(transfers):
        if n:
            args.append("--next")
        args += ["--http2", "--cacert", str(run.cafile)]
        for name, value in headers.items():
            args += ["-H", f"{name}: {value}"]
        args += ["-w", "\\n@@%{http_code} %{http_version} %{num_connects}\\n"]
        args.append(f"https://{HOST}:{run.cfg.stream_port}{target}")
    res = subprocess.run(args, capture_output=True, text=True, timeout=90, check=False)
    answers = []
    for chunk in ("\n" + res.stdout).split("\n@@")[1:]:
        line, _, _ = chunk.partition("\n")
        code, version, connects = line.split()
        answers.append({"status": int(code), "version": version, "connects": int(connects)})
    assert len(answers) == len(transfers), res.stdout + res.stderr
    return answers


def test_held_waits_share_one_http2_connection_and_last_their_whole_hold(
    kept: Live,  # noqa: F811
    sample: Path,
) -> None:
    """Over HTTPS a browser has one HTTP/2 connection for all its requests: many held waits
    on it at once. Caddy's timeouts (read_header_timeout, idle_timeout) cut none of them."""
    if not shutil.which("curl"):
        pytest.skip("curl is not installed on this host")
    _start(kept, sample)
    browsers = [_let_in(kept) for _ in range(control.MAX_HOLDS // control.HOLDS_PER_DEVICE)]
    rev = _rev(kept, browsers[0])
    waits = [
        (
            {**browsers[n // control.HOLDS_PER_DEVICE], **PLAYER},
            f"/aaa/1/sync?pg=w{n:04d}&rev={rev}",
        )
        for n in range(control.MAX_HOLDS)
    ]
    began = time.monotonic()
    answers = _curl_h2(kept, waits)
    took = time.monotonic() - began
    assert {a["status"] for a in answers} == {200}, answers
    assert {a["version"] for a in answers} == {"2"}
    assert sum(a["connects"] for a in answers) == 1, "all of them on one connection"
    assert control.HOLD_SECONDS - 0.5 <= took < 2 * control.HOLD_SECONDS, "held, and only once"


def test_every_wait_answered_at_once_while_videos_are_fetched_fails_none(
    live: Live,  # noqa: F811
    sample: Path,
) -> None:
    """A full group: each change of the owner's answers every held wait at once, and every
    page waits again at once, while other requests fetch the video. The gate's socket must
    take Caddy's dials for both: past its backlog a viewer got a 502, waits and pieces."""
    _start(live, sample)
    browsers = [_let_in(live) for _ in range(control.MAX_HOLDS // control.HOLDS_PER_DEVICE)]
    stop = threading.Event()
    seen: Counter[str] = Counter()
    lock = threading.Lock()

    def page(n: int) -> None:
        browser, rev = browsers[n // control.HOLDS_PER_DEVICE], 0
        while not stop.is_set():
            target = f"/aaa/1/sync?pg=pg{n:04d}&rev={rev}"
            status, _, body = live.get(target, headers={**browser, **PLAYER}, timeout=40)
            with lock:
                seen[f"wait {status}"] += 1
            if status != 200:
                time.sleep(1)
                continue
            rev = json.loads(body)["together"]["rev"]

    def fetch(n: int) -> None:
        browser = browsers[n % len(browsers)]
        while not stop.is_set():
            status, _, _ = live.get("/aaa/1/hls/base/master.m3u8", headers=browser)
            with lock:
                seen[f"video {status}"] += 1

    threads = [threading.Thread(target=page, args=(n,)) for n in range(control.MAX_HOLDS)]
    threads += [threading.Thread(target=fetch, args=(n,)) for n in range(32)]
    for thread in threads:
        thread.start()
    try:
        time.sleep(2)
        for n in range(6):
            live.api("together", {"action": "seek", "value": 1 + n})
            time.sleep(1.5)
    finally:
        stop.set()
        live.api("together", {"action": "pause"})  # the last held waits end now
        for thread in threads:
            thread.join(30)
    assert set(seen) == {"wait 200", "video 200"}, seen
    assert seen["wait 200"] >= 6 * control.MAX_HOLDS


@pytest.fixture
def issued(pebble: Pebble, short_tmp: Path) -> Iterator[Live]:  # noqa: F811
    run = _issued(pebble, short_tmp)
    yield run
    run.stop.set()
    run.thread.join(15)


def _together_run(run: Live, sample: Path) -> None:
    """Two players' pages, as player.js has them: each is let in, loads the page, says it is
    at the group's place and waits out its cooldown. One page waits for the group to change
    while the other presses play: the wait is answered on the change, not when its hold
    ends, and both pages see the group playing."""
    _start(run, sample)
    tv, phone = _let_in(run), _let_in(run)
    settings = _page(run, tv)
    group = settings["together"]
    assert group["sync"] == "/aaa/1/sync" and group["state"]["paused"] is True
    _page(run, phone)
    report = settings["report"]
    for browser, pg in ((tv, "tv000001"), (phone, "ph000001")):
        answer = _ask(run, browser, f"{report}?pg={pg}&t=0&d=12&p=1&in=1")
        assert answer["cooldown"] == together.COOLDOWN_SECONDS
    time.sleep(together.COOLDOWN_SECONDS)
    rev = _ask(run, tv, f"{report}?pg=tv000001&t=0&d=12&p=1&in=1")["together"]["rev"]

    held: dict[str, Any] = {}

    def wait() -> None:
        began = time.monotonic()
        held["answer"] = _ask(run, tv, f"{group['sync']}?pg=tv000001&rev={rev}")
        held["took"] = time.monotonic() - began

    waiting = threading.Thread(target=wait)
    waiting.start()
    time.sleep(1)  # the wait is held by now
    pressed = _ask(run, phone, f"{report}?pg=ph000001&t=0&d=12&p=1&in=1&act=play&id=ph000001-1")
    waiting.join(control.HOLD_SECONDS)
    assert pressed["outcome"] == together.APPLIED and pressed["together"]["paused"] is False
    answer = held["answer"]
    assert answer["together"]["rev"] == rev + 1 and answer["together"]["paused"] is False
    assert 0.5 < answer["waited"] < 5 and held["took"] < 5, "answered on the change"
    shown = run.api("together", {"action": "status"})["stream"]["together"]
    assert shown["paused"] is False and len(shown["pages"]) == 2


def test_two_pages_watch_together_over_https_with_a_certificate_from_pebble(
    issued: Live, sample: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(together, "COOLDOWN_SECONDS", 1.0)
    _together_run(issued, sample)
    assert issued.api("status")["https"]["certificate"], "served with Pebble's"


def _post(run: Live, browser: dict[str, str], body: Any, raw: bytes = b"") -> tuple[int, Any]:
    headers = {**browser, **PLAYER, "Content-Type": "application/json"}
    data = raw or json.dumps(body).encode()
    status, _, answer = run.get("/aaa/1/call", "POST", headers, body=data)
    return status, json.loads(answer) if status == 200 else answer


def test_a_call_is_set_up_through_caddy_over_https(
    kept: Live,  # noqa: F811
    sample: Path,
) -> None:
    """Two pages, as call.js has them: one starts a call, the other is rung and joins, and
    a message from one comes to the other with its next wait. Caddy takes only a small
    body, and no other POST."""
    from vpstream import caddy

    _start(kept, sample)
    tv, phone = _let_in(kept), _let_in(kept)
    assert _page(kept, tv)["together"]["call"] == "/aaa/1/call"
    for browser, pg in ((tv, "tv000001"), (phone, "ph000001")):
        _ask(kept, browser, f"/aaa/1/client?pg={pg}&t=0&d=12&p=1&in=1")
    status, started = _post(kept, tv, {"pg": "tv000001", "do": "start"})
    assert status == 200 and started["outcome"] == together.STARTED
    rung = _ask(kept, phone, "/aaa/1/sync?pg=ph000001&rev=0&cv=0")
    assert rung["call"]["ring"] is True
    assert _post(kept, phone, {"pg": "ph000001", "do": "start"})[1]["outcome"] == "joined"
    offer = {"pg": "tv000001", "do": "send", "to": "ph000001", "kind": "offer", "data": "v=0"}
    assert _post(kept, tv, offer)[1]["outcome"] == together.SENT
    answer = _ask(kept, phone, "/aaa/1/client?pg=ph000001&t=0&d=12&p=1&in=1&cm=0")
    assert [m["data"] for m in answer["call"]["mail"]] == ["v=0"]
    big = json.dumps({"pg": "tv000001", "data": "x" * caddy.CALL_BYTES}).encode()
    assert _post(kept, tv, None, raw=big)[0] == 413
    status, _, _ = kept.get("/aaa/1/client", "POST", {**tv, **PLAYER}, body=b"{}")
    assert status == 405, "nothing else is posted"
