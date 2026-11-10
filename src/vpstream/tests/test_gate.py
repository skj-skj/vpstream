from __future__ import annotations

import pytest

from vpstream import gate, media, net
from vpstream.gate import APPROVAL, LOCKED, OPEN, Devices


@pytest.mark.parametrize(
    ("policy", "allowed", "gets_in"),
    [
        (OPEN, None, True),
        (OPEN, True, True),
        (OPEN, False, False),
        (APPROVAL, None, False),
        (APPROVAL, True, True),
        (APPROVAL, False, False),
        (LOCKED, None, False),
        (LOCKED, True, True),
        (LOCKED, False, False),
    ],
)
def test_policies(policy: str, allowed: bool | None, gets_in: bool) -> None:
    assert gate.allows(policy, allowed) is gets_in


def test_a_visit_is_counted_and_the_player_remembered() -> None:
    table = Devices()
    assert table.visit(OPEN, "192.0.2.1", "VLC/3.0", 10.0)
    assert table.visit(OPEN, "192.0.2.1", "VLC/3.0 again", 12.0)
    (row,) = table.rows()
    assert row["requests"] == 2 and row["blocked"] == 0
    assert (row["first_seen"], row["last_seen"]) == (10.0, 12.0)
    assert row["user_agent"] == "VLC/3.0 again" and row["wl"] is False


def test_approval_puts_unknown_devices_on_the_waiting_list() -> None:
    table = Devices()
    assert not table.visit(APPROVAL, "192.0.2.1", "tv", 1.0)
    row = table.get("192.0.2.1")
    assert row is not None and row.wl and row.blocked == 1 and row.allowed is None
    table.decide("192.0.2.1", True, 2.0)
    assert table.visit(APPROVAL, "192.0.2.1", "tv", 3.0)
    assert row.wl  # the mark stays: it shows which devices came through the list


def test_a_denied_device_stays_out_under_every_policy() -> None:
    table = Devices()
    table.decide("192.0.2.9", False, 1.0)
    for policy in gate.POLICIES:
        assert not table.visit(policy, "192.0.2.9", "x", 2.0)
    assert not table.get("192.0.2.9").wl  # type: ignore[union-attr]  # denied, not waiting


def test_a_lock_blocks_without_marking_the_waiting_list() -> None:
    table = Devices()
    assert not table.visit(LOCKED, "192.0.2.1", "x", 1.0)
    row = table.get("192.0.2.1")
    assert row is not None and row.blocked == 1 and not row.wl


def test_keep_current_enables_what_gets_in_now() -> None:
    table = Devices()
    table.visit(OPEN, "192.0.2.1", "a", 1.0)
    table.decide("192.0.2.2", False, 1.0)
    table.keep_current(OPEN)
    assert table.get("192.0.2.1").allowed is True  # type: ignore[union-attr]
    assert table.get("192.0.2.2").allowed is False  # type: ignore[union-attr]

    waiting = Devices()
    waiting.visit(APPROVAL, "192.0.2.3", "a", 1.0)
    waiting.keep_current(APPROVAL)  # it did not get in, so the lock does not let it in
    assert waiting.get("192.0.2.3").allowed is None  # type: ignore[union-attr]


def test_decide_takes_any_spelling_of_an_address_and_refuses_the_rest() -> None:
    table = Devices()
    table.decide("::ffff:192.0.2.1", True, 1.0)
    assert table.visit(LOCKED, "192.0.2.1", "x", 2.0)
    for bad in ("", "example.com", "192.0.2.1/24", "1.2.3"):
        with pytest.raises(gate.GateError, match="not an IP address"):
            table.decide(bad, True, 1.0)


def test_the_table_is_capped_and_never_drops_a_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "MAX_DEVICES", 3)
    table = Devices()
    table.decide("192.0.2.1", True, 0.0)
    table.visit(OPEN, "192.0.2.2", "old", 1.0)
    table.visit(OPEN, "192.0.2.3", "new", 2.0)
    assert table.visit(OPEN, "192.0.2.4", "newest", 3.0)
    assert [r["ip"] for r in table.rows()] == ["192.0.2.4", "192.0.2.3", "192.0.2.1"]

    for n in (3, 4):
        table.decide(f"192.0.2.{n}", False, 4.0)
    # Full of decisions: an unknown device is judged, but not written down.
    assert table.visit(OPEN, "192.0.2.5", "x", 5.0)
    assert not table.visit(LOCKED, "192.0.2.5", "x", 5.0)
    assert len(table) == 3 and table.get("192.0.2.5") is None
    with pytest.raises(gate.GateError, match="full"):
        table.decide("192.0.2.6", True, 6.0)


def test_a_long_user_agent_is_cut() -> None:
    table = Devices()
    table.visit(OPEN, "192.0.2.1", "x" * 5000, 1.0)
    assert len(table.rows()[0]["user_agent"]) == gate.MAX_USER_AGENT


def test_bytes_are_added_to_known_devices_only() -> None:
    table = Devices()
    table.visit(OPEN, "192.0.2.1", "x", 1.0)
    table.add_bytes("192.0.2.1", 500)
    table.add_bytes("::ffff:192.0.2.1", 500)
    table.add_bytes("192.0.2.2", 500)
    table.add_bytes("nonsense", 500)
    assert [(r["ip"], r["bytes"]) for r in table.rows()] == [("192.0.2.1", 1000)]


def test_rows_survive_a_round_trip_and_bad_rows_are_skipped() -> None:
    table = Devices()
    table.visit(APPROVAL, "192.0.2.1", "tv", 1.0)
    table.decide("2001:db8::1", True, 2.0)
    again = Devices.from_rows(table.rows())
    assert again.rows() == table.rows()

    odd = [None, {"ip": "nope"}, {"ip": "192.0.2.7", "allowed": "yes", "requests": "many"}, 5]
    (row,) = Devices.from_rows(odd).rows()
    assert row["ip"] == "192.0.2.7" and row["allowed"] is None and row["requests"] == 0
    assert Devices.from_rows("not a list").rows() == []


# --- addresses and files


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


def test_natural_order(tmp_path) -> None:  # type: ignore[no-untyped-def]
    names = ["ep10.mkv", "ep2.mkv", "Ep1.MKV", "ep²x.mp4", "notes.txt", ".hidden.mkv", "ep3.webm"]
    for name in names:
        (tmp_path / name).write_text("x")
    (tmp_path / "sub.mkv").mkdir()
    (tmp_path / "sub.mkv" / "inside.mkv").write_text("x")
    found = [p.name for p in media.scan(tmp_path)]
    assert found == ["Ep1.MKV", "ep2.mkv", "ep3.webm", "ep10.mkv", "ep²x.mp4"]


@pytest.mark.parametrize(
    ("name", "ctype"),
    [
        ("a.mkv", "video/x-matroska"),
        ("A.MP4", "video/mp4"),
        ("a.b.webm", "video/webm"),
        ("noext", "application/octet-stream"),
        ("a.zzzunknown", "application/octet-stream"),
    ],
)
def test_content_type(name: str, ctype: str) -> None:
    assert media.content_type(f"/srv/{name}") == ctype
