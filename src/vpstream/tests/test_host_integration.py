"""Tests against the real host: Tailscale, and the installed service when there is one.

They change nothing: no root, a temporary folder, a free port. Parts the host does not have
are skipped.
"""

from __future__ import annotations

import json
import shutil
import socket
import threading
import urllib.request
from pathlib import Path

import pytest

from vpstream import __version__, client, config, daemon, doctor, service, tailscale
from vpstream.tools import SystemRunner

pytestmark = pytest.mark.integration


@pytest.fixture
def tailnet_ip() -> str:
    if not shutil.which("tailscale"):
        pytest.skip("tailscale is not installed on this host")
    return tailscale.ipv4(SystemRunner())


def test_doctor_runs_every_check_on_this_host() -> None:
    checks = doctor.run_checks(SystemRunner())
    names = [c.name for c in checks]
    assert {"python", "ffmpeg", "caddy", "ufw", "tailscale", "install", "config"} <= set(names)
    assert doctor.format_report(checks)


def test_tailscale_address_belongs_to_this_host(tailnet_ip: str) -> None:
    with socket.socket() as probe:
        probe.bind((tailnet_ip, 0))  # only an address of this host can be bound


def test_dashboard_is_reachable_on_the_tailscale_address_only(
    short_tmp: Path, tailnet_ip: str
) -> None:
    with socket.socket() as probe:
        probe.bind((tailnet_ip, 0))
        port = int(probe.getsockname()[1])
    log: list[str] = []
    d = daemon.Daemon(
        config.Config(dashboard_port=port),
        config.Paths.under(short_tmp),
        SystemRunner(),
        log=log.append,
        retry_seconds=0.2,
    )
    stop = threading.Event()
    thread = threading.Thread(target=d.serve, args=(stop,), daemon=True)
    thread.start()
    try:
        assert d.ready.wait(10)
        for _ in range(100):
            if d.dashboard_address:
                break
            stop.wait(0.1)
        assert d.dashboard_address == f"{tailnet_ip}:{port}", log
        with urllib.request.urlopen(f"http://{tailnet_ip}:{port}/api/v1/status", timeout=5) as r:
            assert json.load(r)["version"] == __version__
        with socket.socket() as other, pytest.raises(OSError):
            other.settimeout(2)
            other.connect(("127.0.0.1", port))
        assert client.call(d.paths.socket, "GET", "status")["dashboard"]["address"]
    finally:
        stop.set()
        thread.join(10)


def test_the_installed_service_answers_and_keeps_the_stream_port_closed() -> None:
    layout = service.Layout()
    if not layout.unit_file.exists():
        pytest.skip("the service is not set up on this host (sudo vpstream init)")
    info = client.call(layout.paths.socket, "GET", "status")
    assert info["version"] == __version__, "restart the service after an upgrade"
    assert info["stream"] is None
    assert info["firewall_problem"] is None
    assert info["stream_port_state"] == "closed"
    if shutil.which("tailscale"):
        expected = f"{tailscale.ipv4(SystemRunner())}:{info['dashboard']['port']}"
        assert info["dashboard"]["address"] == expected
