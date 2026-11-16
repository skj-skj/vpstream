from __future__ import annotations

import pytest

from vpstream import net


@pytest.mark.parametrize(
    ("text", "ip"),
    [
        ("192.0.2.1", "192.0.2.1"),
        (" 192.0.2.1 ", "192.0.2.1"),
        ("::ffff:192.0.2.1", "192.0.2.1"),
        ("2001:DB8::1", "2001:db8::1"),
        ("fe80::1%eth0", "fe80::1"),
        ("", None),
        (None, None),
        ("192.0.2.1:80", None),
    ],
)
def test_normal_ip(text: str | None, ip: str | None) -> None:
    assert net.normal_ip(text) == ip


def test_outgoing_address_is_an_address_or_nothing() -> None:
    found = net.outgoing_address()
    assert found == "" or net.normal_ip(found) == found


@pytest.mark.parametrize(
    ("host", "shown"),
    [("0.0.0.0", "<server-ip>"), ("::", "<server-ip>"), ("::1", "[::1]"), ("10.0.0.2", "10.0.0.2")],
)
def test_url_host(host: str, shown: str) -> None:
    assert net.url_host(host) == shown
