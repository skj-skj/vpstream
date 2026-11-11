"""Browser mode: the routes of a stream, the player's settings and its page."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from vpstream import gate, player, prepared
from vpstream.probe import parse_probe
from vpstream.stream import Stream, StreamError
from vpstream.tests.test_probe import SAMPLE


def make_entry(
    cache: Path, video: Path, soft: tuple[int, ...] = (1,), burn: tuple[int, ...] = ()
) -> Path:
    """A folder of prepared versions for `video`, as a prepare leaves it."""
    entry = prepared.entry_dir(cache, str(video))
    (entry / "base").mkdir(parents=True)
    for name in ("master.m3u8", "video.m3u8", "a1.m3u8", "video_00000.m4s"):
        (entry / "base" / name).write_text("#EXTM3U\n")
    (entry / "subs").mkdir()
    for n in soft:
        (entry / "subs" / f"{n}.vtt").write_text("WEBVTT\n")
    for n in burn:
        (entry / "burn" / str(n)).mkdir(parents=True)
        (entry / "burn" / str(n) / "master.m3u8").write_text("#EXTM3U\n")
    prepared.write_meta(entry, prepared.meta_of(parse_probe(SAMPLE, str(video)), "copy"))
    return entry


@pytest.fixture
def show(tmp_path: Path) -> Path:
    folder = tmp_path / "show"
    folder.mkdir()
    for name in ("e1.mkv", "e2.mkv", "e3.mkv"):
        (folder / name).write_bytes(name.encode())
    return folder


def browser(source: Path, cache: Path, **options: Any) -> Stream:
    kind = "dir" if source.is_dir() else "file"
    s = Stream.create({kind: str(source), "alias": "aaa", "mode": "browser", **options}, 1000.0)
    s.entries = [str(prepared.entry_dir(cache, v)) for v in s.videos]
    return s


def get(s: Stream, path: str, html: bool = False, ip: str = "192.0.2.1") -> Any:
    return s.visit(path, ip, "TV", html, 1001.0)


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    return tmp_path / "cache"


@pytest.fixture
def live(show: Path, cache: Path) -> Stream:
    """A folder stream, open to everyone, whose first two episodes are prepared."""
    make_entry(cache, show / "e1.mkv", soft=(1,), burn=(3,))
    make_entry(cache, show / "e2.mkv")
    return browser(show, cache, policy="open")


# --- the routes


def test_browser_mode_waits_for_approval_by_default(show: Path, cache: Path) -> None:
    assert browser(show, cache).policy == gate.APPROVAL


def test_a_browser_gets_the_player_and_another_player_the_playlist(live: Stream) -> None:
    page = get(live, "aaa/1", html=True)
    assert (page.status, page.kind) == (200, "player")
    assert page.data["master"] == "/aaa/1/hls/base/master.m3u8"
    vlc = get(live, "aaa/2")
    assert (vlc.status, vlc.kind, vlc.location) == (302, "redirect", "/aaa/2/hls/base/master.m3u8")


def test_a_video_that_is_not_prepared_is_not_ready(live: Stream) -> None:
    assert get(live, "aaa/3", html=True).kind == "notready"
    assert get(live, "aaa/3").status == 503
    index = get(live, "aaa", html=True)
    assert index.kind == "index" and index.ready == [True, True, False]
    assert [v["ready"] for v in live.describe("http://h", 1000)["videos"]] == [True, True, False]


def test_the_files_of_the_versions(live: Stream, cache: Path, show: Path) -> None:
    answer = get(live, "aaa/1/hls/base/video_00000.m4s")
    entry = prepared.entry_dir(cache, str(show / "e1.mkv"))
    assert (answer.kind, answer.rel) == ("file", "1/base/video_00000.m4s")
    assert answer.file == os.path.realpath(entry / "base" / "video_00000.m4s")
    assert get(live, "aaa/1/hls/burn/3/master.m3u8").rel == "1/burn/3/master.m3u8"
    for path in (
        "aaa/1/hls/meta.json",
        "aaa/1/hls/base/../meta.json",
        "aaa/1/hls/subs/2.vtt",
        "aaa/3/hls/base/master.m3u8",  # not prepared
        "aaa/4/hls/base/master.m3u8",  # no such video
        "aaa/1/HLS/base/master.m3u8",
        "aaa/1/x/base/master.m3u8",
        "aaa/1/hls",
    ):
        assert get(live, path).kind == "missing", path


def test_the_players_own_files_and_nothing_else(live: Stream) -> None:
    for name in player.ASSETS:
        assert (get(live, f"aaa/assets/{name}").kind, get(live, f"aaa/assets/{name}").asset) == (
            "asset",
            name,
        )
    for path in ("aaa/assets/dashboard.js", "aaa/assets/../api", "aaa/assets", "aaa/assets/x/y"):
        assert get(live, path).kind == "missing", path


def test_direct_mode_has_none_of_the_browser_routes(show: Path, cache: Path) -> None:
    make_entry(cache, show / "e1.mkv")
    s = Stream.create({"dir": str(show), "alias": "aaa"}, 1000.0)
    assert get(s, "aaa/assets/player.js").kind == "missing"
    assert get(s, "aaa/1/hls/base/master.m3u8").kind == "missing"
    assert get(s, "aaa/1", html=True).kind == "video"


def test_a_single_film_is_played_at_the_alias(tmp_path: Path, cache: Path) -> None:
    film = tmp_path / "film.mkv"
    film.write_bytes(b"x")
    make_entry(cache, film)
    s = browser(film, cache, policy="open")
    assert get(s, "aaa", html=True).kind == "player"
    assert get(s, "aaa").location == "/aaa/1/hls/base/master.m3u8"
    assert get(s, "aaa/2", html=True).kind == "missing"


def test_a_waiting_browser_gets_its_code_and_a_player_only_a_refusal(live: Stream) -> None:
    live.set_policy(gate.APPROVAL)
    waiting = get(live, "aaa/1", html=True)
    assert (waiting.status, waiting.kind) == (403, "waiting") and len(waiting.data["code"]) == 4
    assert get(live, "aaa/1/hls/base/master.m3u8").kind == "denied"  # no code for files
    live.devices.decide("192.0.2.1", False, 1.0)
    assert get(live, "aaa/1", html=True).kind == "denied"  # denied: no more waiting
    live.set_policy(gate.LOCKED)
    assert get(live, "aaa/1", html=True, ip="192.0.2.7").kind == "denied"


def test_a_device_that_waited_is_not_told_to_wait_once_the_stream_is_locked(live: Stream) -> None:
    # Found in review: the code stayed on its row, and the page said "waiting" for good.
    live.set_policy(gate.APPROVAL)
    assert get(live, "aaa/1", html=True).kind == "waiting"
    live.set_policy(gate.LOCKED)
    assert get(live, "aaa/1", html=True).kind == "denied"
    live.unlock()
    assert get(live, "aaa/1", html=True).kind == "waiting"


def test_a_browser_stream_survives_a_restart(live: Stream) -> None:
    live.audio, live.sub = "jpn", "1"
    again = Stream.from_dict(json.loads(json.dumps(live.to_dict())))
    assert (again.entries, again.audio, again.sub, again.mode) == (
        live.entries,
        "jpn",
        "1",
        "browser",
    )


@pytest.mark.parametrize(
    "change",
    [
        {"entries": []},  # browser mode without its folders
        {"entries": ["relative/path"]},
        {"entries": [3, 4, 5]},
        {"audio": "english"},
        {"sub": 2},
    ],
)
def test_a_damaged_browser_record_is_refused(live: Stream, change: dict[str, Any]) -> None:
    with pytest.raises(StreamError, match="not usable"):
        Stream.from_dict({**live.to_dict(), **change})


def test_a_direct_record_with_folders_is_refused(show: Path) -> None:
    s = Stream.create({"dir": str(show), "alias": "aaa"}, 1000.0)
    with pytest.raises(StreamError):
        Stream.from_dict({**s.to_dict(), "entries": ["/a", "/b", "/c"]})


# --- the player's settings


def settings(entry: Path, audio: str | None = None, sub: str | None = None, **kw: Any) -> Any:
    options: dict[str, Any] = {"alias": "aaa", "number": 1, "names": ["e1.mkv", "e2.mkv"],
                               "folder": True, "audio": audio, "sub": sub, **kw}  # fmt: skip
    return player.settings(entry, **options)


def test_the_player_is_told_the_tracks_and_what_is_prepared(cache: Path, show: Path) -> None:
    entry = make_entry(cache, show / "e1.mkv", soft=(1,), burn=(2, 3))
    s = settings(entry)
    assert [a["label"] for a in s["audio"]] == ["English", "Japanese"]
    assert s["start_audio"] == 0  # marked default
    assert [(x["number"], x["kind"]) for x in s["subs"]] == [(1, "soft"), (2, "burn"), (3, "burn")]
    assert s["subs"][0]["url"] == "/aaa/1/hls/subs/1.vtt"
    assert s["subs"][1]["url"] == "/aaa/1/hls/burn/2/master.m3u8"
    assert s["start_sub"] == -1 and s["next"] == "/aaa/2" and s["index"] == "/aaa"
    assert s["assets"] == "/aaa/assets/"


@pytest.mark.parametrize(
    ("audio", "sub", "start_audio", "start_sub"),
    [
        ("2", "3", 1, 1),  # subtitles 1 (soft) and 3 (burned in) are prepared
        ("jpn", "eng", 1, 0),  # the first English subtitles that are prepared
        ("ja", "jpn", 1, 1),
        ("9", "9", 0, -1),  # not in the file: the default, and none
        ("off", "off", 0, -1),
        ("bad,", "2,3", 0, -1),  # unreadable: as if not given
    ],
)
def test_the_starting_choices(
    cache: Path, show: Path, audio: str, sub: str, start_audio: int, start_sub: int
) -> None:
    entry = make_entry(cache, show / "e1.mkv", soft=(1,), burn=(3,))
    s = settings(entry, audio, sub)
    assert (s["start_audio"], s["start_sub"]) == (start_audio, start_sub)


def test_a_track_that_is_both_soft_and_burned_in_starts_soft(cache: Path, show: Path) -> None:
    entry = make_entry(cache, show / "e1.mkv", soft=(1,), burn=(1,))
    s = settings(entry, sub="1")
    assert s["start_sub"] == 0 and [x["kind"] for x in s["subs"]] == ["soft", "burn"]


def test_the_last_episode_has_no_next_and_a_film_no_episodes(cache: Path, show: Path) -> None:
    entry = make_entry(cache, show / "e1.mkv")
    assert settings(entry, number=2)["next"] is None
    assert settings(entry, folder=False)["next"] is None


def test_without_meta_the_player_still_starts(cache: Path, show: Path) -> None:
    entry = make_entry(cache, show / "e1.mkv")
    (entry / "meta.json").unlink()
    s = settings(entry, "1", "1")
    assert (s["audio"], s["subs"], s["start_audio"], s["start_sub"]) == ([], [], -1, -1)


def test_the_page_cannot_be_broken_out_of_by_a_file_name() -> None:
    title = '</script><script>alert(1)</script> {{SETTINGS}} & "x"'
    page = player.page({"title": title, "assets": "/aaa/assets/", "subs": []})
    assert "<script>alert(1)" not in page
    # Every placeholder is filled; the title's own "{{SETTINGS}}" stays text, twice.
    assert "{{TITLE}}" not in page and "{{ASSETS}}" not in page
    assert page.count("{{SETTINGS}}") == 2
    data = page.split('type="application/json">')[1].split("</script>")[0]
    assert json.loads(data)["title"] == title
    head = page.split("<title>")[1].split("</title>")[0]
    assert "&lt;/script&gt;" in head and "{{SETTINGS}}" in head
    assert '<script src="/aaa/assets/player.js"></script>' in page
