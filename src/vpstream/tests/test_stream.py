from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from vpstream import gate, stream
from vpstream.stream import Stream, StreamError


@pytest.fixture
def film(tmp_path: Path) -> Path:
    path = tmp_path / "a film.mkv"
    path.write_bytes(b"video")
    return path


@pytest.fixture
def show(tmp_path: Path) -> Path:
    folder = tmp_path / "show"
    folder.mkdir()
    for name in ("e10.mkv", "e2.mkv", "e1.mkv", "cover.jpg"):
        (folder / name).write_bytes(b"x")
    return folder


def make(now: float = 1000.0, **options: Any) -> Stream:
    return Stream.create(options, now)


def visit(s: Stream, path: str, ip: str = "192.0.2.1", html: bool = False) -> stream.Answer:
    return s.visit(path, ip, "test", html, 1001.0)


# --- starting


def test_defaults(film: Path) -> None:
    s = make(file=str(film))
    assert s.kind == "file" and s.videos == [str(film)] and s.mode == "direct"
    assert s.policy == gate.OPEN and s.unlocked_policy == gate.OPEN
    assert s.expires == 1000.0 + 180 * 60
    assert len(s.alias) == 3 and s.alias.isalpha() and s.alias.islower()


def test_a_folder_is_numbered_in_natural_order(show: Path) -> None:
    s = make(dir=str(show), alias="Show")
    assert s.alias == "show"
    assert s.names() == ["e1.mkv", "e2.mkv", "e10.mkv"]


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({}, "exactly one"),
        ({"file": "/a", "dir": "/b"}, "exactly one"),
        ({"file": 5}, "not a path"),
        ({"file": "relative.mkv"}, "full path"),
        ({"file": "/no/such/file.mkv"}, "not a file"),
        ({"dir": "/no/such/folder"}, "not a folder"),
        ({"file": "/x", "speed": 2}, "unknown option: speed"),
    ],
)
def test_bad_sources(options: dict[str, Any], message: str) -> None:
    with pytest.raises(StreamError, match=message):
        make(**options)


def test_a_folder_is_not_a_file_and_the_other_way_round(film: Path, show: Path) -> None:
    with pytest.raises(StreamError, match="not a file"):
        make(file=str(show))
    with pytest.raises(StreamError, match="not a folder"):
        make(dir=str(film))


def test_a_folder_without_videos_is_refused(tmp_path: Path) -> None:
    (tmp_path / "readme.txt").write_text("x")
    with pytest.raises(StreamError, match="no videos"):
        make(dir=str(tmp_path))


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_a_file_the_service_cannot_read_is_refused_with_the_reason(film: Path) -> None:
    film.chmod(0)
    with pytest.raises(StreamError, match=r"cannot read .*a film\.mkv.*its own user"):
        make(file=str(film))


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"mode": "browser"}, "not a mode"),
        ({"policy": "everyone"}, "not a policy"),
        ({"alias": "caps"}, "reserved"),
        ({"alias": "a/b"}, "not a usable alias"),
        ({"alias": "-a"}, "not a usable alias"),
        ({"alias": "x" * 33}, "not a usable alias"),
        ({"alias": 7}, "not a usable alias"),
        ({"ttl": -1}, "time limit"),
        ({"ttl": "60"}, "time limit"),
        ({"ttl": True}, "time limit"),
        ({"ttl": float("nan")}, "time limit"),
        ({"ttl": float("inf")}, "at most"),
    ],
)
def test_bad_options(film: Path, options: dict[str, Any], message: str) -> None:
    with pytest.raises(StreamError, match=message):
        make(file=str(film), **options)


def test_ttl(film: Path) -> None:
    assert make(file=str(film), ttl=0).expires is None
    assert make(file=str(film), ttl=1.5).expires == 1090.0


def test_random_aliases_are_never_reserved(monkeypatch: pytest.MonkeyPatch) -> None:
    letters = iter("api" + "xyz")
    monkeypatch.setattr(stream.secrets, "choice", lambda _: next(letters))
    monkeypatch.setattr(stream, "RESERVED", frozenset({"api"}))
    assert stream.random_alias() == "xyz"


# --- the addresses


def test_file_stream_addresses(film: Path) -> None:
    s = make(file=str(film), alias="aaa")
    for path in ("aaa", "/aaa", "aaa/", "AAA", "aaa/1"):
        answer = visit(s, path)
        assert (answer.status, answer.kind, answer.file) == (200, "video", str(film)), path
    for path in ("aaa/2", "aaa/0", "aaa/01", "aaa/playlist.m3u", "aaa/1/2", "aaa/x", "aaa/-1"):
        assert visit(s, path).status == 404, path


def test_folder_stream_addresses(show: Path) -> None:
    s = make(dir=str(show), alias="aaa")
    index = visit(s, "aaa", html=True)
    assert (index.kind, index.alias, index.names) == ("index", "aaa", s.names())
    assert visit(s, "aaa").kind == "playlist"
    assert visit(s, "aaa/playlist.m3u", html=True).kind == "playlist"
    assert visit(s, "aaa/3").file == str(show / "e10.mkv")
    assert visit(s, "aaa/1").file == str(show / "e1.mkv")
    for path in ("aaa/4", "aaa/0", "aaa/1234567", "aaa/\u0661"):
        assert visit(s, path).status == 404, path


def test_other_paths_are_not_found_and_are_not_devices(film: Path) -> None:
    s = make(file=str(film), alias="aaa", policy="approval")
    for path in ("", "/", "aaaa", "aa", "bbb/1", "../aaa", "favicon.ico"):
        assert visit(s, path).status == 404, path
    assert len(s.devices) == 0  # a scanner does not show up on the waiting list


def test_a_refused_device_cannot_tell_what_exists(film: Path) -> None:
    s = make(file=str(film), alias="aaa", policy="approval")
    assert {visit(s, p).status for p in ("aaa", "aaa/1", "aaa/99", "aaa/x")} == {403}
    assert s.devices.rows()[0]["wl"] is True


# --- policies


def test_lock_keeps_the_connected_devices_and_unlock_goes_back(film: Path) -> None:
    s = make(file=str(film), alias="aaa")
    assert visit(s, "aaa", ip="192.0.2.1").status == 200
    s.devices.decide("192.0.2.2", False, 1.0)
    s.set_policy("locked")
    assert visit(s, "aaa", ip="192.0.2.1").status == 200
    assert visit(s, "aaa", ip="192.0.2.2").status == 403
    assert visit(s, "aaa", ip="192.0.2.3").status == 403
    s.set_policy("locked")  # locking twice does not let the blocked one in
    assert visit(s, "aaa", ip="192.0.2.3").status == 403
    s.unlock()
    assert s.policy == "open"
    assert visit(s, "aaa", ip="192.0.2.3").status == 200
    assert visit(s, "aaa", ip="192.0.2.2").status == 403


def test_unlock_goes_back_to_approval_when_that_was_the_policy(film: Path) -> None:
    s = make(file=str(film), alias="aaa", policy="approval")
    visit(s, "aaa", ip="192.0.2.1")
    s.set_policy("locked")
    assert visit(s, "aaa", ip="192.0.2.1").status == 403  # waiting is not connected
    s.unlock()
    assert s.policy == "approval"
    with pytest.raises(StreamError, match="not a policy"):
        s.set_policy("unlock")


def test_a_stream_can_start_locked(film: Path) -> None:
    s = make(file=str(film), policy="locked")
    assert s.policy == "locked" and s.unlocked_policy == "open"


# --- as data


def test_round_trip(show: Path) -> None:
    s = make(dir=str(show), alias="aaa", policy="approval", ttl=0)
    visit(s, "aaa/1")
    s.set_policy("locked")
    again = Stream.from_dict(s.to_dict())
    assert again.to_dict() == s.to_dict()
    assert again.unlocked_policy == "approval" and again.expires is None


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "tape"},
        {"videos": []},
        {"videos": ["relative.mkv"]},
        {"videos": "x"},
        {"alias": "caps"},
        {"policy": "x"},
        {"unlocked_policy": "locked"},
        {"mode": "browser"},
        {"expires": "soon"},
        {"started": None},
    ],
)
def test_a_damaged_record_is_refused(film: Path, change: dict[str, Any]) -> None:
    data = {**make(file=str(film)).to_dict(), **change}
    with pytest.raises(StreamError, match="not usable"):
        Stream.from_dict(data)
    with pytest.raises(StreamError, match="not usable"):
        Stream.from_dict({})


def test_describe(show: Path) -> None:
    s = make(dir=str(show), alias="aaa", ttl=10)
    visit(s, "aaa")
    s.devices.decide("192.0.2.5", False, 1.0)
    info = s.describe("http://192.0.2.9:1337", 1060.0)
    assert info["url"] == "http://192.0.2.9:1337/aaa" and info["seconds_left"] == 540.0
    assert info["videos"][2] == {
        "number": 3,
        "name": "e10.mkv",
        "url": "http://192.0.2.9:1337/aaa/3",
    }
    assert {d["ip"]: d["passes"] for d in info["devices"]} == {
        "192.0.2.1": True,
        "192.0.2.5": False,
    }
    assert s.describe("x", 99999.0)["seconds_left"] == 0.0
