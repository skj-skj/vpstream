from __future__ import annotations

import pytest

from vpstream import gate, media, net
from vpstream.gate import APPROVAL, LOCKED, OPEN, Devices


def visit(table: Devices, policy: str, ip: str, user_agent: str, now: float) -> bool:
    """One request of the player at `ip`: whether it gets in."""
    return table.admit(policy, ip, user_agent, now).ok


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
    assert visit(table, OPEN, "192.0.2.1", "VLC/3.0", 10.0)
    assert visit(table, OPEN, "192.0.2.1", "VLC/3.0 again", 12.0)
    (row,) = table.rows()
    assert row["requests"] == 2 and row["blocked"] == 0
    assert (row["first_seen"], row["last_seen"]) == (10.0, 12.0)
    assert row["user_agent"] == "VLC/3.0 again" and row["wl"] is False


def test_approval_puts_unknown_devices_on_the_waiting_list() -> None:
    table = Devices()
    assert not visit(table, APPROVAL, "192.0.2.1", "tv", 1.0)
    row = table.get("192.0.2.1")
    assert row is not None and row.wl and row.blocked == 1 and row.allowed is None
    table.decide("192.0.2.1", True, 2.0)
    assert visit(table, APPROVAL, "192.0.2.1", "tv", 3.0)
    assert row.wl  # the mark stays: it shows which devices came through the list


def test_a_denied_device_stays_out_under_every_policy() -> None:
    table = Devices()
    table.decide("192.0.2.9", False, 1.0)
    for policy in gate.POLICIES:
        assert not visit(table, policy, "192.0.2.9", "x", 2.0)
    assert not table.get("192.0.2.9").wl  # type: ignore[union-attr]  # denied, not waiting


def test_a_lock_blocks_without_marking_the_waiting_list() -> None:
    table = Devices()
    assert not visit(table, LOCKED, "192.0.2.1", "x", 1.0)
    row = table.get("192.0.2.1")
    assert row is not None and row.blocked == 1 and not row.wl


def test_keep_current_enables_what_gets_in_now() -> None:
    table = Devices()
    visit(table, OPEN, "192.0.2.1", "a", 1.0)
    table.decide("192.0.2.2", False, 1.0)
    table.keep_current(OPEN)
    assert table.get("192.0.2.1").allowed is True  # type: ignore[union-attr]
    assert table.get("192.0.2.2").allowed is False  # type: ignore[union-attr]

    waiting = Devices()
    visit(waiting, APPROVAL, "192.0.2.3", "a", 1.0)
    waiting.keep_current(APPROVAL)  # it did not get in, so the lock does not let it in
    assert waiting.get("192.0.2.3").allowed is None  # type: ignore[union-attr]


def test_decide_takes_any_spelling_of_an_address_and_refuses_the_rest() -> None:
    table = Devices()
    table.decide("::ffff:192.0.2.1", True, 1.0)
    assert visit(table, LOCKED, "192.0.2.1", "x", 2.0)
    for bad in ("", "example.com", "192.0.2.1/24", "1.2.3"):
        with pytest.raises(gate.GateError, match="not a device or an IP address"):
            table.decide(bad, True, 1.0)


def test_the_table_is_capped_and_never_drops_a_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "MAX_DEVICES", 3)
    table = Devices()
    table.decide("192.0.2.1", True, 0.0)
    visit(table, OPEN, "192.0.2.2", "old", 1.0)
    visit(table, OPEN, "192.0.2.3", "new", 2.0)
    assert visit(table, OPEN, "192.0.2.4", "newest", 3.0)
    assert [r["ip"] for r in table.rows()] == ["192.0.2.4", "192.0.2.3", "192.0.2.1"]

    for n in (3, 4):
        table.decide(f"192.0.2.{n}", False, 4.0)
    # Full of decisions: an unknown device could not be listed or disabled, so it stays out.
    assert not visit(table, OPEN, "192.0.2.5", "x", 5.0)
    assert not visit(table, LOCKED, "192.0.2.5", "x", 5.0)
    assert len(table) == 3 and table.get("192.0.2.5") is None
    with pytest.raises(gate.GateError, match="full"):
        table.decide("192.0.2.6", True, 6.0)


def test_a_long_user_agent_is_cut() -> None:
    table = Devices()
    visit(table, OPEN, "192.0.2.1", "x" * 5000, 1.0)
    assert len(table.rows()[0]["user_agent"]) == gate.MAX_USER_AGENT


def test_bytes_are_added_to_known_devices_only() -> None:
    table = Devices()
    visit(table, OPEN, "192.0.2.1", "x", 1.0)
    table.add_bytes("192.0.2.1", 500)
    table.add_bytes("::ffff:192.0.2.1", 500)
    table.add_bytes("192.0.2.2", 500)
    table.add_bytes("nonsense", 500)
    assert [(r["ip"], r["bytes"]) for r in table.rows()] == [("192.0.2.1", 1000)]


def test_rows_survive_a_round_trip_and_bad_rows_are_skipped() -> None:
    table = Devices()
    visit(table, APPROVAL, "192.0.2.1", "tv", 1.0)
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


# --- viewers that try addresses until one works


def test_misses_are_counted_until_the_viewer_is_a_guesser() -> None:
    table = Devices()
    for _ in range(gate.MAX_MISSES - 1):
        table.miss("192.0.2.1")
    assert not table.guesser("192.0.2.1") and table.guessers() == []
    table.miss("192.0.2.1")
    assert table.guesser("192.0.2.1") and table.guessers() == ["192.0.2.1"]
    assert not table.guesser("192.0.2.2")
    table.decide("192.0.2.1", False, 1.0)  # disabling does not let it back in
    assert table.guesser("192.0.2.1")
    table.decide("192.0.2.1", True, 1.0)  # enabling does
    assert not table.guesser("192.0.2.1") and table.guessers() == []


def test_the_guesser_count_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "MAX_GUESSERS", 2)
    table = Devices()
    for n in (1, 2, 3):
        for _ in range(gate.MAX_MISSES):
            table.miss(f"192.0.2.{n}")
    assert table.guessers() == ["192.0.2.1", "192.0.2.2"]


def test_passes_counts_nothing() -> None:
    table = Devices()
    assert not table.passes(OPEN, "192.0.2.1")  # not listed
    visit(table, OPEN, "192.0.2.1", "x", 1.0)
    assert table.passes(OPEN, "192.0.2.1") and not table.passes(LOCKED, "192.0.2.1")
    assert table.rows()[0]["requests"] == 1


def test_a_name_that_is_not_utf8_can_be_shown() -> None:
    import os

    name = os.fsdecode(b"caf\xe9.mkv")
    assert media.shown(name) == "caf\ufffd.mkv"
    media.shown(name).encode("utf-8")  # does not raise
    assert media.shown("plain é.mkv") == "plain é.mkv"
    assert media.is_video(name) and not media.is_video("notes.txt")


def test_a_device_that_waits_gets_a_code_it_keeps() -> None:
    table = Devices()
    visit(table, APPROVAL, "192.0.2.1", "TV", 1.0)
    code = table.get("192.0.2.1").code  # type: ignore[union-attr]
    assert len(code) == 4 and code.isdigit()
    visit(table, APPROVAL, "192.0.2.1", "TV", 2.0)
    assert table.get("192.0.2.1").code == code  # type: ignore[union-attr]
    again = Devices.from_rows(table.rows())
    assert again.get("192.0.2.1").code == code  # type: ignore[union-attr]


def test_only_the_waiting_list_hands_out_codes() -> None:
    table = Devices()
    visit(table, OPEN, "192.0.2.1", "VLC", 1.0)
    visit(table, LOCKED, "192.0.2.2", "VLC", 1.0)
    table.decide("192.0.2.3", True, 1.0)
    visit(table, APPROVAL, "192.0.2.3", "TV", 1.0)
    assert {r["ip"]: r["code"] for r in table.rows()} == {
        "192.0.2.1": "",
        "192.0.2.2": "",
        "192.0.2.3": "",
    }


@pytest.mark.parametrize("code", ["123", "12345", "abcd", 1234, None, "12 4"])
def test_a_recorded_code_that_does_not_fit_is_dropped(code: object) -> None:
    table = Devices.from_rows([{"ip": "192.0.2.1", "code": code}])
    assert table.get("192.0.2.1").code == ""  # type: ignore[union-attr]


# --- browsers, known by their cookie


def browser_visit(
    table: Devices, ip: str, cookie: str | None = None, policy: str = APPROVAL, now: float = 1.0
) -> gate.Admission:
    return table.admit(policy, ip, "JioSphere", now, cookie, browser=True)


def test_a_new_browser_gets_a_cookie_and_waits_with_a_code() -> None:
    table = Devices()
    first = browser_visit(table, "192.0.2.1")
    assert not first.ok and first.cookie and first.device is not None
    tv = first.device
    assert tv.kind == gate.BROWSER and tv.id.startswith("b-") and tv.wl and tv.code
    assert not tv.confirmed
    again = browser_visit(table, "192.0.2.1", first.cookie)
    assert again.device is tv and again.cookie == "" and tv.confirmed
    assert len(table) == 1


def test_an_approved_browser_stays_approved_from_another_address() -> None:
    table = Devices()
    cookie = browser_visit(table, "192.0.2.1").cookie
    tv = table.browser(cookie)
    assert tv is not None
    table.decide(tv.id, True, 2.0)
    # Its segments carry the cookie too, and need no page.
    moved = table.admit(APPROVAL, "198.51.100.7", "JioSphere", 3.0, cookie)
    assert moved.ok and moved.device is tv and tv.ip == "198.51.100.7"


def test_a_stranger_behind_the_same_address_is_another_device() -> None:
    table = Devices()
    cookie = browser_visit(table, "192.0.2.1").cookie
    table.decide(table.browser(cookie).id, True, 2.0)  # type: ignore[union-attr]
    stranger = browser_visit(table, "192.0.2.1")
    assert not stranger.ok and stranger.device is not table.browser(cookie)
    # Without a cookie and not asking for a page, it is the player at the address: not let in.
    assert not table.admit(APPROVAL, "192.0.2.1", "curl", 3.0, None).ok
    assert table.admit(APPROVAL, "192.0.2.1", "JioSphere", 3.0, cookie).ok


def test_a_player_without_cookies_stays_matched_by_address() -> None:
    table = Devices()
    table.decide("192.0.2.1", True, 1.0)  # VLC at home
    assert visit(table, APPROVAL, "192.0.2.1", "VLC/3", 2.0)
    # Letting the player in lets no browser in behind the same address: it waits...
    behind = browser_visit(table, "192.0.2.1")
    assert not behind.ok and behind.device is not None
    assert behind.device.allowed is None and behind.device.wl and behind.device.code
    # ...but shutting the address out shuts out the browsers that come from it.
    table.decide("192.0.2.9", False, 3.0)
    shut = browser_visit(table, "192.0.2.9", policy=OPEN)
    assert not shut.ok and shut.device is not None and shut.device.allowed is False


@pytest.mark.parametrize(
    "cookie",
    ["", "x", "A" * 43, "A" * 44, "../../etc", "a b", "é" * 43],
)
def test_a_cookie_the_daemon_did_not_give_is_no_cookie(cookie: str) -> None:
    table = Devices()
    real = browser_visit(table, "192.0.2.1").cookie
    table.decide(table.browser(real).id, True, 2.0)  # type: ignore[union-attr]
    assert table.browser(cookie) is None
    forged = browser_visit(table, "192.0.2.1", cookie)
    assert not forged.ok and forged.cookie, "a new browser, which waits"


def test_cookies_are_long_and_random_and_only_their_hash_is_kept() -> None:
    table = Devices()
    cookies = {browser_visit(table, f"192.0.2.{n}").cookie for n in range(1, 50)}
    assert len(cookies) == 49 and all(len(c) == 43 for c in cookies)
    recorded = table.rows(private=True)
    shown = table.rows()
    for cookie in cookies:
        assert cookie not in repr(recorded) and cookie not in repr(shown)
    assert all("token" not in row for row in shown)
    assert all(len(row["token"]) == 64 for row in recorded)


def test_a_browser_that_keeps_no_cookie_does_not_fill_the_list() -> None:
    table = Devices()
    for n in range(10):
        browser_visit(table, "192.0.2.1", now=float(n))  # the waiting page reloads by itself
    assert len(table) == gate.MAX_UNCONFIRMED
    # One whose cookie came back is never dropped for that.
    kept = browser_visit(table, "192.0.2.2")
    browser_visit(table, "192.0.2.2", kept.cookie)
    for n in range(10):
        browser_visit(table, "192.0.2.2", now=float(n))
    assert table.browser(kept.cookie) is not None


def test_a_full_table_drops_unconfirmed_browsers_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "MAX_DEVICES", 3)
    table = Devices()
    visit(table, OPEN, "192.0.2.1", "VLC", 1.0)  # oldest, but a player that came back
    tv = browser_visit(table, "192.0.2.2", now=2.0)
    browser_visit(table, "192.0.2.2", tv.cookie, now=3.0)
    browser_visit(table, "192.0.2.3", now=4.0)  # cookie never came back
    browser_visit(table, "192.0.2.4", now=5.0)
    assert table.get("192.0.2.1") is not None and table.browser(tv.cookie) is not None
    assert len(table) == 3


def test_browsers_survive_a_restart_with_their_cookie_and_record() -> None:
    from vpstream import clients

    table = Devices()
    tv = browser_visit(table, "192.0.2.1")
    device = table.browser(tv.cookie)
    assert device is not None
    table.decide(device.id, True, 2.0)
    device.name = "Living-room TV"
    device.client = clients.Client()
    device.client.report(1, {"t": "600", "d": "5400"}, 3.0)
    again = Devices.from_rows(table.rows(private=True), videos=1)
    back = again.browser(tv.cookie)
    assert back is not None and back.id == device.id and back.allowed is True
    assert back.name == "Living-room TV" and back.client == device.client
    # What the dashboard shows cannot be turned into a working table.
    assert Devices.from_rows(table.rows(), videos=1).browser(tv.cookie) is None


def test_recorded_browsers_that_do_not_fit_are_skipped() -> None:
    good = {"kind": "browser", "id": "b-0a1b2c", "ip": "192.0.2.1", "token": "a" * 64}
    rows = [
        {**good, "id": "192.0.2.1"},  # a browser named like an address
        {**good, "id": "b-xyz"},
        {**good, "token": "short"},
        {**good, "token": None},
        {"kind": "player", "id": "b-0a1b2c", "ip": "192.0.2.1"},  # a player under another name
        good,
        {**good, "token": "b" * 64},  # the same id twice: the first one stays
    ]
    table = Devices.from_rows(rows, videos=1)
    assert [r["id"] for r in table.rows(private=True)] == ["b-0a1b2c"]
    assert table.rows(private=True)[0]["token"] == "a" * 64


def test_a_lock_keeps_the_browsers_let_in_and_shuts_out_new_ones() -> None:
    table = Devices()
    tv = browser_visit(table, "192.0.2.1", policy=OPEN)
    assert tv.ok and browser_visit(table, "192.0.2.1", tv.cookie, policy=OPEN).ok
    junk = [browser_visit(table, "203.0.113.9", policy=OPEN) for _ in range(3)]  # previews
    table.keep_current(OPEN)
    assert table.admit(LOCKED, "192.0.2.1", "JioSphere", 2.0, tv.cookie).ok
    assert not browser_visit(table, "192.0.2.1", policy=LOCKED).ok
    # A cookie that never came back is not kept by the lock, so it can still be dropped.
    assert all(table.browser(j.cookie).allowed is None for j in junk)  # type: ignore[union-attr]


def test_bytes_go_to_the_player_at_an_address_else_the_browser_seen_there_last() -> None:
    table = Devices()
    old = browser_visit(table, "192.0.2.1", now=1.0)
    new = browser_visit(table, "192.0.2.1", now=2.0)
    table.add_bytes("192.0.2.1", 100)
    assert table.browser(new.cookie).bytes == 100  # type: ignore[union-attr]
    assert table.browser(old.cookie).bytes == 0  # type: ignore[union-attr]
    visit(table, OPEN, "192.0.2.1", "VLC", 3.0)
    table.add_bytes("::ffff:192.0.2.1", 5)
    assert table.get("192.0.2.1").bytes == 5  # type: ignore[union-attr]


def test_a_browser_is_decided_by_its_id_and_a_player_by_its_address() -> None:
    table = Devices()
    tv = browser_visit(table, "192.0.2.1")
    device = table.browser(tv.cookie)
    assert device is not None
    assert table.find(device.id, 1.0) is device
    player = table.find("192.0.2.1", 1.0)  # the address names the player there, made now
    assert player is not device and player.kind == gate.PLAYER
    with pytest.raises(gate.GateError, match="not a device or an IP address"):
        table.find("b-ffffff", 1.0)


def test_a_disabled_browser_cannot_come_back_by_dropping_its_cookie() -> None:
    table = Devices()
    tv = browser_visit(table, "192.0.2.1", policy=OPEN)
    table.decide(tv.device.id, False, 2.0)  # type: ignore[union-attr]
    again = browser_visit(table, "192.0.2.1", policy=OPEN)  # cookies cleared, private window
    assert not again.ok and again.device is not None and again.device.allowed is False
    assert browser_visit(table, "192.0.2.2", policy=OPEN).ok, "other addresses are not touched"


def test_one_address_cannot_push_other_viewers_out(monkeypatch: pytest.MonkeyPatch) -> None:
    table = Devices()
    tv = browser_visit(table, "192.0.2.50", now=0.5)
    for n in range(3):  # it waits, and its page reloads
        browser_visit(table, "192.0.2.50", tv.cookie, now=1.0 + n)
    for n in range(gate.MAX_DEVICES + 5):  # get a page, send the cookie back, again and again
        flood = browser_visit(table, "198.51.100.1", now=10.0 + n)
        browser_visit(table, "198.51.100.1", flood.cookie, now=10.0 + n)
    assert table.browser(tv.cookie) is not None
    assert len([d for d in table.browsers() if d.ip == "198.51.100.1"]) == gate.MAX_UNDECIDED
    # On one address, the browser that did most stays.
    busy = browser_visit(table, "198.51.100.2", now=1.0)
    for n in range(10):
        browser_visit(table, "198.51.100.2", busy.cookie, now=2.0 + n)
    for n in range(20):
        other = browser_visit(table, "198.51.100.2", now=50.0 + n)
        browser_visit(table, "198.51.100.2", other.cookie, now=50.0 + n)
    assert table.browser(busy.cookie) is not None


def test_an_address_decides_the_browsers_that_come_from_it_too() -> None:
    table = Devices()
    waiting = browser_visit(table, "192.0.2.1")
    denied = browser_visit(table, "192.0.2.1")
    table.decide(denied.device.id, False, 2.0)  # type: ignore[union-attr]
    table.decide("192.0.2.1", True, 3.0)  # vpstream devices enable 192.0.2.1
    assert table.browser(waiting.cookie).allowed is True  # type: ignore[union-attr]
    assert table.browser(denied.cookie).allowed is False, "a decision is not undone"  # type: ignore[union-attr]
    table.decide("192.0.2.1", False, 4.0)
    assert table.browser(waiting.cookie).allowed is False  # type: ignore[union-attr]
    other = browser_visit(table, "192.0.2.9")
    table.decide("192.0.2.1", True, 5.0)
    assert table.browser(other.cookie).allowed is None, "only the browsers at that address"  # type: ignore[union-attr]


def test_a_recorded_infinity_or_nan_is_read_as_nothing() -> None:
    # Python's json reads and writes these; int(inf) would stop the daemon taking the
    # stream up again.
    rows = [
        {"ip": "192.0.2.1", "bytes": float("inf"), "last_seen": float("nan"), "requests": 1e400}
    ]
    (row,) = Devices.from_rows(rows).rows()
    assert (row["bytes"], row["last_seen"], row["requests"]) == (0, 0.0, 0)
