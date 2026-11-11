from __future__ import annotations

import pytest

from vpstream import tracks
from vpstream.probe import AudioTrack, MediaInfo, SubtitleTrack, VideoTrack, parse_probe
from vpstream.tests.test_probe import SAMPLE


def sub(number: int, codec: str, language: str = "eng", title: str = "") -> SubtitleTrack:
    return SubtitleTrack(number, number + 1, codec, language, title, False)


SUBS = [
    sub(1, "subrip"),
    sub(2, "ass", title="Signs"),
    sub(3, "hdmv_pgs_subtitle", "jpn"),
    sub(4, "mov_text", "jpn"),
    sub(5, "eia_608", "eng"),  # neither soft nor burnable
]


@pytest.mark.parametrize(
    ("spec", "choices"),
    [
        ("1,3", [1, 3]),
        (" 2 , ENG ", [2, "eng"]),
        ("en", ["eng"]),  # two letters, as most people type them
        ("fra,deu", ["fre", "ger"]),  # the other three-letter spelling
        ("none", []),
        ("NONE", []),
        ("9999", [9999]),
    ],
)
def test_choices_are_numbers_or_language_codes(spec: str, choices: list[object]) -> None:
    assert tracks.parse_choices(spec) == choices


@pytest.mark.parametrize(
    "spec", ["", "1,,3", "1,", "0", "01", "10000", "-1", "english", "e", "1,none", "none,1", "1;3"]
)
def test_a_choice_that_cannot_be_read_says_how_to_write_it(spec: str) -> None:
    with pytest.raises(tracks.SelectionError, match="track-info"):
        tracks.parse_choices(spec)


def test_without_a_choice_every_plain_text_track_is_soft_and_nothing_is_burned() -> None:
    chosen, notes = tracks.select_subtitles(SUBS, None)
    assert [t.number for t in chosen] == [1, 4] and notes == []


def test_a_language_picks_every_track_in_it_in_file_order() -> None:
    chosen, notes = tracks.select_subtitles(SUBS, "jpn,1")
    assert [t.number for t in chosen] == [1, 3, 4] and notes == []


def test_a_track_named_twice_is_prepared_once() -> None:
    chosen, _ = tracks.select_subtitles(SUBS, "1,eng,1")
    assert [t.number for t in chosen] == [1, 2]  # 5 is English too, but cannot be prepared


def test_choices_that_name_nothing_or_what_cannot_be_prepared_are_noted() -> None:
    chosen, notes = tracks.select_subtitles(SUBS, "7,spa,5")
    assert chosen == []
    assert notes == [
        "subtitle 7: not in this file",
        "spa subtitles: not in this file",
        "subtitle 5 (eia_608): unsupported",
    ]


def test_none_prepares_no_subtitles() -> None:
    assert tracks.select_subtitles(SUBS, "none") == ([], [])


@pytest.mark.parametrize(("spec", "choice"), [("off", None), ("OFF", None), ("none", None),
                                               ("2", 2), ("en", "eng")])  # fmt: skip
def test_one_choice(spec: str, choice: object) -> None:
    assert tracks.parse_choice(spec) == choice


@pytest.mark.parametrize("spec", ["1,2", "", "x1"])
def test_one_choice_only(spec: str) -> None:
    with pytest.raises(tracks.SelectionError):
        tracks.parse_choice(spec)


def test_pick_finds_the_first_track_a_choice_names() -> None:
    assert tracks.pick(SUBS, "jpn") is SUBS[2]
    assert tracks.pick(SUBS, 4) is SUBS[3]
    assert tracks.pick(SUBS, "ger") is None and tracks.pick(SUBS, None) is None


def test_a_track_tagged_with_a_two_letter_code_matches_the_three_letter_one() -> None:
    assert tracks.pick([sub(1, "subrip", "EN")], "eng") is not None


def test_labels_fall_back_from_title_to_language_to_number() -> None:
    assert tracks.label(sub(1, "ass", "eng", "  Signs ")) == "Signs"
    assert tracks.label(sub(1, "ass", "fra")) == "French"
    assert tracks.label(sub(1, "ass", "xyz")) == "xyz"
    assert tracks.label(sub(3, "ass", "und")) == "Track 3"
    assert tracks.label(sub(3, "ass", "")) == "Track 3"


def test_the_default_audio_is_the_one_marked_else_the_first() -> None:
    a = [AudioTrack(n, n, "aac", "eng", "", 2, n == 2) for n in (1, 2, 3)]
    assert tracks.default_audio(a) == 1
    assert tracks.default_audio([AudioTrack(1, 1, "aac", "eng", "", 2, False)]) == 0


def test_track_info_numbers_each_kind_from_one() -> None:
    info = parse_probe(SAMPLE, "/m/movie.mkv")
    lines = tracks.format_track_info(info).splitlines()
    assert lines[0] == "movie.mkv · 2h 03m · matroska · 8.2 GB"
    assert lines[3].split()[:4] == ["1", "hevc", "Main", "10"] and "HDR" in lines[3]
    assert "browser mode: copied as it is" in lines[4]
    audio = lines[lines.index("AUDIO") + 1 :][:2]
    assert audio[0].split() == ["1", "eng", "English", "eac3", "5.1", "default"]
    assert audio[1].split() == ["2", "jpn", "Japanese", "aac", "2.0"]
    subs = lines[lines.index("SUBTITLES") + 1 :]
    assert [line.split()[0] for line in subs] == ["1", "2", "3", "4"]
    assert subs[0].split()[-2:] == ["text", "soft"]
    assert subs[2].split()[-3:] == ["image", "burned", "in"]
    assert subs[3].split()[-1] == "unsupported"


def test_track_info_says_when_browser_mode_must_transcode() -> None:
    v = VideoTrack(1, 0, "h264", "High 10", 1920, 1080, 24.0, "yuv420p10le", 10, False, None)
    info = MediaInfo("/m/x.mkv", "matroska", 60.0, 0, None, video=[v])
    text = tracks.format_track_info(info)
    assert "transcoded to H.264" in text and "10-bit" in text
    assert "· 0 B" not in text.splitlines()[0]  # no size known: none shown
    nothing = tracks.format_track_info(MediaInfo("/m/a.mka", "matroska", 1.0, 1, None))
    assert "no video track" in nothing
