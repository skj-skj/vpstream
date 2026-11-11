from __future__ import annotations

import os
from pathlib import Path

import pytest

from vpstream import prepared
from vpstream.files import source_key
from vpstream.probe import parse_probe
from vpstream.tests.test_probe import SAMPLE


@pytest.fixture
def film(tmp_path: Path) -> Path:
    path = tmp_path / "media" / "film.mkv"
    path.parent.mkdir()
    path.write_bytes(b"video")
    return path


@pytest.fixture
def entry(tmp_path: Path, film: Path) -> Path:
    folder = prepared.entry_dir(tmp_path / "cache", str(film))
    (folder / "base").mkdir(parents=True)
    (folder / "base" / "master.m3u8").write_text("#EXTM3U\n")
    (folder / "base" / "video_00001.m4s").write_bytes(b"seg")
    (folder / "subs").mkdir()
    (folder / "subs" / "3.vtt").write_text("WEBVTT\n")
    prepared.write_meta(folder, prepared.meta_of(parse_probe(SAMPLE, str(film)), "copy"))
    return folder


# --- what a viewer may fetch


@pytest.mark.parametrize("rel", ["base/master.m3u8", "base/video_00001.m4s", "subs/3.vtt"])
def test_the_files_of_a_version_are_served(entry: Path, rel: str) -> None:
    assert prepared.served_file(entry, rel) == Path(os.path.realpath(entry / rel))


@pytest.mark.parametrize(
    "rel",
    [
        "meta.json",  # the record of the source names its path
        "base/../meta.json",
        "base/./master.m3u8",
        "base",
        "base/",
        "/etc/passwd",
        "base/master.m3u8/x",
        "subs/0.vtt",
        "subs/3.vtt.part",
        "subs/3.part.vtt",
        "burn/0/master.m3u8",
        "burn/x/master.m3u8",
        "base/master.M3U8",
        "base/missing.m4s",
        "base/a b.m4s",
        ".work-3/source.mkv",
    ],
)
def test_nothing_else_is_served(entry: Path, rel: str) -> None:
    assert prepared.served_file(entry, rel) is None


def test_a_link_out_of_the_folder_is_not_followed(entry: Path, film: Path) -> None:
    # A name the pattern allows, but a link to the source film.
    (entry / "base" / "video_00002.m4s").symlink_to(film)
    assert prepared.served_file(entry, "base/video_00002.m4s") is None
    # Also when the whole version folder is a link.
    (entry / "burn").mkdir()
    (entry / "burn" / "4").symlink_to(film.parent, target_is_directory=True)
    (film.parent / "master.m3u8").write_text("#EXTM3U\n")
    assert prepared.served_file(entry, "burn/4/master.m3u8") is None


def test_a_cache_reached_through_a_link_still_serves(tmp_path: Path, entry: Path) -> None:
    link = tmp_path / "elsewhere"
    link.symlink_to(entry, target_is_directory=True)
    assert prepared.served_file(link, "base/master.m3u8") == Path(
        os.path.realpath(entry / "base/master.m3u8")
    )


# --- what is made


def test_versions_count_only_whole_ones(entry: Path) -> None:
    (entry / "subs" / "4.part.vtt").write_text("half")
    (entry / "burn" / "7").mkdir(parents=True)  # no master: cut off before it was renamed
    (entry / "burn" / "8").mkdir()
    (entry / "burn" / "8" / "master.m3u8").write_text("#EXTM3U\n")
    (entry / "burn" / "9.part").mkdir()
    assert prepared.versions(entry) == prepared.Versions(True, [3], [8])
    assert prepared.versions(entry.parent / "nothing") == prepared.Versions()


def test_meta_records_the_source_and_names_its_tracks(entry: Path, film: Path) -> None:
    meta = prepared.read_meta(entry)
    assert meta is not None and meta["source"] == str(film) and meta["base"] == "copy"
    assert [t.label for t in prepared.meta_tracks(entry, "audio")] == ["English", "Japanese"]
    assert prepared.meta_tracks(entry, "audio")[0].default
    assert [t.number for t in prepared.meta_tracks(entry, "subtitles")] == [1, 2, 3, 4]


@pytest.mark.parametrize(
    "text",
    ["not json", "[]", '{"source": 3}', '{"audio": []}'],
)
def test_a_damaged_meta_is_none(entry: Path, text: str) -> None:
    (entry / "meta.json").write_text(text)
    assert prepared.read_meta(entry) is None
    assert prepared.meta_tracks(entry, "audio") == []


def test_track_entries_that_do_not_fit_are_left_out(entry: Path) -> None:
    (entry / "meta.json").write_text(
        '{"source": "/x", "audio": [{"number": 2, "label": "B"}, {"number": true}, '
        '{"number": 0}, {"number": "1"}, 5, {"number": 1}], "subtitles": "x"}'
    )
    found = prepared.meta_tracks(entry, "audio")
    assert [(t.number, t.label, t.language) for t in found] == [
        (1, "Track 1", "und"),
        (2, "B", "und"),
    ]
    assert prepared.meta_tracks(entry, "subtitles") == []


# --- the master playlist of a burned-in version

BASE = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="group_aud",NAME="audio_1",DEFAULT=YES,LANGUAGE="eng",URI="a1.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="group_aud",NAME="audio_2",DEFAULT=NO,LANGUAGE="jpn",URI="a2.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=3129883,RESOLUTION=1920x1080,CODECS="hvc1.2.4.L120.90,mp4a.40.2",AUDIO="group_aud"
video.m3u8
"""
BURN = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-STREAM-INF:BANDWIDTH=10290276,AVERAGE-BANDWIDTH=8415262,RESOLUTION=1920x1080,CODECS="avc1.640028"
video.m3u8
"""


def test_a_burned_in_version_plays_with_the_audio_of_the_base() -> None:
    lines = prepared.burn_master(BASE, BURN).splitlines()
    assert lines[:2] == ["#EXTM3U", "#EXT-X-VERSION:7"]
    assert lines[2].endswith(',URI="../../base/a1.m3u8"') and 'LANGUAGE="eng"' in lines[2]
    assert lines[3].endswith(',URI="../../base/a2.m3u8"')
    inf = prepared.attributes(lines[4])
    # Its own video codec and size, the audio codec and group of the base.
    assert inf["CODECS"] == '"avc1.640028,mp4a.40.2"' and inf["AUDIO"] == '"group_aud"'
    assert inf["BANDWIDTH"] == "10290276" and inf["RESOLUTION"] == "1920x1080"
    assert lines[5] == "video.m3u8" and len(lines) == 6


def test_a_burned_in_version_of_a_video_without_audio_is_its_own_master() -> None:
    base = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,CODECS="avc1.64001f"\nvideo.m3u8\n'
    lines = prepared.burn_master(base, BURN).splitlines()
    assert not any(line.startswith("#EXT-X-MEDIA") for line in lines)
    assert prepared.attributes(lines[2])["CODECS"] == '"avc1.640028"'
    assert "AUDIO" not in prepared.attributes(lines[2])


def test_attributes_keep_commas_inside_quotes() -> None:
    attrs = prepared.attributes('#EXT-X-STREAM-INF:A=1,CODECS="x,y",B="q"')
    assert attrs == {"A": "1", "CODECS": '"x,y"', "B": '"q"'}


@pytest.mark.parametrize("burn", ["#EXTM3U\n", "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1"])
def test_a_burn_playlist_without_a_video_is_refused(burn: str) -> None:
    with pytest.raises(ValueError, match="names no video"):
        prepared.burn_master(BASE, burn)


# --- the whole cache


def test_entries_say_which_are_stale(tmp_path: Path, entry: Path, film: Path) -> None:
    cache = tmp_path / "cache"
    (prepared.root(cache) / "not-a-key").mkdir()
    (prepared.root(cache) / ("0" * 32)).mkdir()  # no meta: no source to tell
    rows = {e.key: e for e in prepared.entries(cache)}
    assert set(rows) == {entry.name, "0" * 32}
    mine = rows[entry.name]
    assert (mine.source, mine.stale, mine.base, mine.soft) == (str(film), False, "copy", [3])
    assert mine.bytes > 0 and rows["0" * 32].stale
    film.write_bytes(b"a changed film")
    assert prepared.entries(cache)[-1].stale or prepared.entries(cache)[0].stale
    assert source_key(film) != entry.name
    film.unlink()
    assert {e.key: e.stale for e in prepared.entries(cache)}[entry.name]
    assert prepared.entries(tmp_path / "no-cache") == []


def test_a_base_that_is_not_whole_is_not_listed_as_made(tmp_path: Path, entry: Path) -> None:
    (entry / "base" / "master.m3u8").unlink()
    assert prepared.entries(tmp_path / "cache")[0].base == ""


def test_sweep_removes_what_a_cut_off_prepare_left(tmp_path: Path, entry: Path) -> None:
    (entry / "base.part").mkdir()
    (entry / "burn" / "4.part").mkdir(parents=True)
    (entry / "subs" / "1.part.vtt").write_text("half")
    (entry / ".work-4").mkdir()
    (entry / ".work-4" / "source.mkv").symlink_to(entry / "meta.json")
    assert prepared.sweep(tmp_path / "cache") == 4
    assert sorted(p.name for p in entry.iterdir()) == ["base", "burn", "meta.json", "subs"]
    assert (entry / "meta.json").is_file()  # the link went, not what it named
    assert prepared.versions(entry).base
    assert prepared.sweep(tmp_path / "cache") == 0


def test_take_moves_a_folder_aside_in_one_step(tmp_path: Path, entry: Path) -> None:
    trash = prepared.take(entry)
    assert not entry.exists() and trash.is_dir() and (trash / "meta.json").is_file()
    assert prepared.entries(tmp_path / "cache") == []  # never listed as half there
    assert prepared.take(entry) == entry  # gone already: nothing to move
    assert prepared.sweep(tmp_path / "cache") == 1 and not trash.exists()  # a cut-off remove
