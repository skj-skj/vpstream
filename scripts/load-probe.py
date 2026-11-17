#!/usr/bin/env python3
"""Load a running vpstream stream the way Android players do, and watch what it costs.

Opens `--parallel` connections at once, each a loop of range requests at random offsets
that reads `--read` bytes and then hangs up (a seek while the old request still runs), for
`--seconds`. With `--burst`, every `--burst-every` seconds that many extra seeks start at
once. Meanwhile the RSS and the threads of the processes in `--pid` (the daemon, Caddy) are
sampled from /proc. Standard library only; run it on the phone or on another device.

    scripts/load-probe.py http://192.168.1.69:1337/abc --parallel 8 --seconds 30 \\
        --pid $(pgrep -f '[v]pstream daemon') --pid $(pgrep -f '[c]addy run')
"""

from __future__ import annotations

import argparse
import collections
import http.client
import json
import random
import statistics
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any


def proc_status(pid: int) -> tuple[int, int] | None:
    """RSS in kB and the number of threads of a process, or None when it is gone."""
    try:
        text = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return None
    fields = dict(line.split(":", 1) for line in text.splitlines() if ":" in line)
    return int(fields["VmRSS"].split()[0]), int(fields["Threads"])


class Probe:
    def __init__(self, url: str, read: int, timeout: float) -> None:
        parts = urllib.parse.urlsplit(url)
        self.host, self.port = parts.hostname or "", parts.port or 80
        self.path = parts.path or "/"
        self.read, self.timeout = read, timeout
        self.size = 0
        self.lock = threading.Lock()
        self.latencies: list[float] = []  # seconds to the first byte of the answer
        self.outcomes: collections.Counter[str] = collections.Counter()
        self.bytes = 0

    def _note(self, outcome: str, latency: float | None = None, got: int = 0) -> None:
        with self.lock:
            self.outcomes[outcome] += 1
            self.bytes += got
            if latency is not None:
                self.latencies.append(latency)

    def find_size(self) -> None:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        conn.request("GET", self.path, headers={"Range": "bytes=0-0"})
        response = conn.getresponse()
        response.read()
        conn.close()
        rng = response.getheader("Content-Range") or ""
        if response.status != 206 or "/" not in rng:
            raise SystemExit(f"not a video that takes range requests: {response.status} {rng}")
        self.size = int(rng.rsplit("/", 1)[1])

    def one(self) -> None:
        start = random.randrange(0, max(1, self.size - self.read))
        began = time.monotonic()
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            conn.request("GET", self.path, headers={"Range": f"bytes={start}-"})
            response = conn.getresponse()
            latency = time.monotonic() - began
            if response.status != 206:
                response.read()
                self._note(f"http {response.status}", latency)
                return
            got = len(response.read(self.read))  # then hang up, as a seek does
            self._note("ok", latency, got)
        except (OSError, http.client.HTTPException) as exc:
            self._note(type(exc).__name__)
        finally:
            conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("url")
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--read", type=int, default=2 * 1024 * 1024, help="bytes per request")
    ap.add_argument("--burst", type=int, default=0, help="extra seeks started at once")
    ap.add_argument("--burst-every", type=float, default=5)
    ap.add_argument("--timeout", type=float, default=15)
    ap.add_argument("--pid", type=int, action="append", default=[], help="process to watch")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    args = ap.parse_args()

    probe = Probe(args.url, args.read, args.timeout)
    probe.find_size()
    end = time.monotonic() + args.seconds
    peaks: dict[int, list[int]] = {pid: [0, 0] for pid in args.pid}
    gone: set[int] = set()

    def worker() -> None:
        while time.monotonic() < end:
            probe.one()

    def watch() -> None:
        while time.monotonic() < end + 1:
            for pid in args.pid:
                found = proc_status(pid)
                if found is None:
                    gone.add(pid)
                    continue
                peaks[pid] = [max(peaks[pid][0], found[0]), max(peaks[pid][1], found[1])]
            time.sleep(0.2)

    threads = [threading.Thread(target=worker) for _ in range(args.parallel)]
    threads.append(threading.Thread(target=watch))
    for t in threads:
        t.start()
    while args.burst and time.monotonic() < end - args.burst_every:
        time.sleep(args.burst_every)
        burst = [threading.Thread(target=probe.one) for _ in range(args.burst)]
        for t in burst:
            t.start()
        threads += burst
    for t in threads:
        t.join()

    lat = sorted(probe.latencies) or [0.0]
    result: dict[str, Any] = {
        "parallel": args.parallel,
        "burst": args.burst,
        "seconds": args.seconds,
        "outcomes": dict(probe.outcomes),
        "requests_per_second": round(sum(probe.outcomes.values()) / args.seconds, 1),
        "mb_per_second": round(probe.bytes / args.seconds / 1e6, 1),
        "first_byte_ms": {
            "p50": round(1000 * statistics.median(lat)),
            "p95": round(1000 * lat[int(0.95 * (len(lat) - 1))]),
            "max": round(1000 * lat[-1]),
        },
        "peaks": {str(pid): {"rss_kb": p[0], "threads": p[1]} for pid, p in peaks.items()},
        "gone": sorted(gone),
    }
    if args.json:
        print(json.dumps(result))
    else:
        for key, value in result.items():
            print(f"{key:20} {value}")
    return 1 if gone or set(probe.outcomes) - {"ok"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
