"""A stream over HTTPS: the certificate comes before Caddy, and lasts the whole stream."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from vpstream import certificate, config, state
from vpstream.certificate import Certificates
from vpstream.control import Control, ControlError
from vpstream.stream import MAX_HTTPS_TTL_MINUTES, Stream, StreamError
from vpstream.tests.fakes import FakeFirewall, FakeIssuer, FakeServer, store_certificate

HOST = "192.0.2.9"
NOW = 1_800_000_000.0
HOUR = 3600.0


class Rig:
    def __init__(self, root: Path, https: bool = True, fail: str = "") -> None:
        self.now = NOW
        self.paths = config.Paths.under(root)
        self.cfg = config.Config(
            stream_host=HOST, stream_https=https, acme_ca="https://ca.test/dir"
        )
        self.fw, self.acme_fw, self.server = FakeFirewall(), FakeFirewall(), FakeServer()
        self.log: list[str] = []
        self.certs = Certificates(
            self.cfg,
            self.paths,
            None,
            self.acme_fw,
            self.log.append,
            lambda: self.now,  # type: ignore[arg-type]
        )
        self.issuer = FakeIssuer(self.certs.store, self.cfg.acme_ca, lambda: self.now, fail=fail)
        self.certs.issuer = self.issuer
        self.film = root / "film.mkv"
        self.film.write_bytes(b"video")
        self.control = self.again()

    def again(self, boot: str = "boot-1") -> Control:
        self.control = Control(
            self.cfg,
            self.paths,
            self.fw,
            self.server,
            self.log.append,
            lambda: self.now,
            boot,
            certificates=self.certs,
        )
        return self.control

    def start(self, **options: Any) -> dict[str, Any]:
        return self.control.start({"file": str(self.film), "alias": "aaa", **options})

    def keep(self, not_after: float) -> None:
        store_certificate(self.certs.store, self.cfg.acme_ca, HOST, NOW - HOUR, not_after)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_an_https_stream_has_its_certificate_before_caddy_starts(rig: Rig) -> None:
    info = rig.start(ttl=60)
    assert rig.issuer.asked == [HOST]
    [served] = rig.server.started_with
    assert served is not None and served.host == HOST
    assert info["stream"]["url"] == f"https://{HOST}:1337/aaa"
    assert info["stream"]["https"] is True
    assert rig.acme_fw.calls == ["open", "close"], "port 80 only while it was asked"
    assert rig.fw.calls == ["open"]


def test_the_next_stream_uses_the_kept_certificate(rig: Rig) -> None:
    rig.start(ttl=60)
    rig.control.stop()
    rig.now += 100 * HOUR
    rig.start(ttl=180)
    assert rig.issuer.asked == [HOST], "it lasts: nothing is asked"
    rig.control.stop()
    rig.now += 50 * HOUR  # 9 hours left of it: not enough for a stream of 10
    rig.start(ttl=600)
    assert rig.issuer.asked == [HOST, HOST]


def test_a_failed_request_starts_nothing_and_says_how_to_go_plain(rig: Rig) -> None:
    rig.issuer.fail = "Timeout during connect (likely firewall problem)"
    with pytest.raises(ControlError, match=r"firewall problem.*vpstream start --http"):
        rig.start(ttl=60)
    assert rig.server.calls == [] and rig.fw.calls == []
    assert rig.control.stream is None
    assert state.load(rig.paths.state_dir / state.FILE_NAME)[0].stream is None


def test_http_starts_one_stream_on_plain_http(rig: Rig) -> None:
    info = rig.start(http=True, ttl=0)  # no time limit is fine without a certificate
    assert rig.issuer.asked == [] and rig.server.started_with == [None]
    assert info["stream"]["url"] == f"http://{HOST}:1337/aaa"
    assert info["stream"]["https"] is False


def test_a_plain_host_never_asks(tmp_path: Path) -> None:
    rig = Rig(tmp_path, https=False)
    info = rig.start()
    assert rig.issuer.asked == [] and rig.server.started_with == [None]
    assert info["stream"]["url"].startswith("http://")
    assert rig.control.certificate_status() is None


@pytest.mark.parametrize("ttl", [0, MAX_HTTPS_TTL_MINUTES + 1, 7 * 24 * 60])
def test_an_https_stream_must_end_before_a_new_certificate_would(rig: Rig, ttl: float) -> None:
    with pytest.raises(StreamError, match=r"at most 9360 minutes.*--http"):
        rig.start(ttl=ttl)
    assert rig.issuer.asked == []


def test_the_longest_https_stream_is_allowed(rig: Rig) -> None:
    rig.start(ttl=MAX_HTTPS_TTL_MINUTES)
    assert rig.issuer.asked == [HOST]


def test_http_must_be_true_or_false(rig: Rig) -> None:
    with pytest.raises(StreamError, match="http must be true or false"):
        rig.start(http="yes")


def test_a_restarted_daemon_serves_the_stream_with_the_kept_certificate(rig: Rig) -> None:
    rig.start(ttl=60)
    rig.now += HOUR / 2
    rig.again().reconcile()  # what `init` does to a running stream: restart the service
    assert rig.control.stream is not None and rig.control.stream.https
    assert rig.issuer.asked == [HOST]
    assert rig.server.started_with[-1] is not None


def test_status_tells_the_kept_certificate(rig: Rig) -> None:
    assert rig.control.certificate_status() is None
    rig.keep(NOW + 10 * HOUR)
    assert rig.control.certificate_status() == {
        "host": HOST,
        "not_before": NOW - HOUR,
        "expires": NOW + 10 * HOUR,
    }
    assert rig.control.base_url() == f"https://{HOST}:1337"


def test_a_daemon_without_certificates_refuses_an_https_stream(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.control.certificates = None
    with pytest.raises(ControlError, match="cannot serve a stream over HTTPS"):
        rig.start(ttl=60)


# --- the record


def _record(**over: Any) -> dict[str, Any]:
    stream = Stream.create({"file": "/m/f.mkv", "alias": "aaa", "ttl": 60}, NOW, https=True)
    return {**stream.to_dict(), **over}


def test_the_record_keeps_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vpstream.stream.videos_of", lambda *a: ["/m/f.mkv"])
    assert Stream.from_dict(_record()).https is True
    old = _record()
    del old["https"]  # recorded before HTTPS existed
    assert Stream.from_dict(old).https is False


@pytest.mark.parametrize(
    "over",
    [
        {"https": "yes"},
        {"expires": None},
        {"expires": NOW + certificate.LONGEST_STREAM_SECONDS + 1},
    ],
)
def test_a_record_that_could_outlive_its_certificate_is_not_used(
    monkeypatch: pytest.MonkeyPatch, over: dict[str, Any]
) -> None:
    monkeypatch.setattr("vpstream.stream.videos_of", lambda *a: ["/m/f.mkv"])
    with pytest.raises(StreamError, match="not usable"):
        Stream.from_dict(_record(**over))
