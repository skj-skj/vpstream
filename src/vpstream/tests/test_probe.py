from __future__ import annotations

import json
from typing import Any

import pytest

from vpstream.probe import ProbeError, parse_probe, probe
from vpstream.tests.fakes import FakeRunner

SAMPLE: dict[str, Any] = {
    "format": {
        "format_name": "matroska,webm",
        "duration": "7380.5",
        "size": "8200000000",
        "bit_rate": "8888000",
    },
    "streams": [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "hevc",
            "profile": "Main 10",
            "width": 3840,
            "height": 2160,
            "pix_fmt": "yuv420p10le",
            "avg_frame_rate": "24000/1001",
            "color_transfer": "smpte2084",
            "tags": {"BPS": "40000000"},
        },
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "eac3",
            "channels": 6,
            "tags": {"language": "eng", "title": "English"},
            "disposition": {"default": 1},
        },
        {
            "index": 2,
            "codec_type": "audio",
            "codec_name": "aac",
            "channels": 2,
            "tags": {"language": "jpn"},
        },
        {"index": 3, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}},
        {
            "index": 4,
            "codec_type": "subtitle",
            "codec_name": "ass",
            "tags": {"language": "eng", "title": "Styled"},
        },
        {
            "index": 5,
            "codec_type": "subtitle",
            "codec_name": "hdmv_pgs_subtitle",
            "tags": {"language": "jpn"},
        },
        {"index": 6, "codec_type": "subtitle", "codec_name": "eia_608"},
        {"index": 7, "codec_type": "attachment", "codec_name": "ttf"},
        {
            "index": 8,
            "codec_type": "video",
            "codec_name": "mjpeg",
            "disposition": {"attached_pic": 1},
        },
    ],
}


def test_parse_counts_and_numbers() -> None:
    info = parse_probe(SAMPLE, "x.mkv")
    assert info.duration == pytest.approx(7380.5)
    assert info.size == 8_200_000_000
    assert [a.number for a in info.audio] == [1, 2]
    assert [s.number for s in info.subtitles] == [1, 2, 3, 4]
    assert len(info.video) == 1  # cover art is not a video track
    assert info.attachments == 2


def test_video_details() -> None:
    v = parse_probe(SAMPLE).video[0]
    assert (v.codec, v.width, v.height) == ("hevc", 3840, 2160)
    assert v.bit_depth == 10
    assert v.hdr is True
    assert v.fps == pytest.approx(23.976)
    assert v.bitrate == 40_000_000


def test_audio_language_and_default() -> None:
    a = parse_probe(SAMPLE).audio
    assert (a[0].language, a[0].title, a[0].default, a[0].channels) == ("eng", "English", True, 6)
    assert a[1].language == "jpn" and a[1].default is False


def test_subtitle_treatment() -> None:
    subs = parse_probe(SAMPLE).subtitles
    assert [(s.codec, s.kind, s.treatment) for s in subs] == [
        ("subrip", "text", "soft"),
        ("ass", "text", "burned in"),
        ("hdmv_pgs_subtitle", "image", "burned in"),
        ("eia_608", "text", "unsupported"),
    ]
    assert subs[3].language == "und"


def test_missing_fields_do_not_crash() -> None:
    info = parse_probe({"streams": [{"codec_type": "video"}]})
    assert info.duration == 0.0
    assert info.video[0].bit_depth == 8
    assert info.video[0].hdr is False
    assert info.video[0].fps == 0.0


def test_probe_uses_runner() -> None:
    r = FakeRunner()
    r.on("ffprobe", stdout=json.dumps(SAMPLE))
    info = probe(r, "/media/x.mkv")
    assert info.path == "/media/x.mkv"
    assert r.calls[0][-1] == "/media/x.mkv"


def test_probe_failure() -> None:
    r = FakeRunner()
    r.on("ffprobe", returncode=1, stderr="No such file")
    with pytest.raises(ProbeError, match="No such file"):
        probe(r, "missing.mkv")


WEBVTT_DUMP = "\n00000000: 5745 4256 5454                           WEBVTT\n"


def test_extradata_bytes_and_identify_codec() -> None:
    from vpstream.probe import extradata_bytes, identify_codec

    assert extradata_bytes(WEBVTT_DUMP) == b"WEBVTT"
    # ffprobe pads the hex column to 40 characters, so the text column never mixes in.
    two_lines = (
        "00000000: 5745 4256 5454 0a0a 5354 594c 450a 3a3a  WEBVTT..STYLE.::\n"
        f"00000010: {'6361 6665':<40} cafe\n"
    )
    assert extradata_bytes(two_lines) == b"WEBVTT\n\nSTYLE\n::cafe"
    assert identify_codec(b"\xef\xbb\xbfWEBVTT\n") == "webvtt"
    assert identify_codec(b"[Script Info]") == ""


REPROBE_2 = ("ffprobe", "-v", "error", "-print_format", "json", "-select_streams", "2")


def _mkv_with_unnamed_subtitle() -> dict[str, Any]:
    return {
        "format": {"format_name": "matroska,webm", "duration": "30"},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264"},
            {"index": 1, "codec_type": "subtitle", "codec_name": "subrip"},
            {"index": 2, "codec_type": "subtitle", "tags": {"language": "spa"}},
        ],
    }


def test_unnamed_matroska_subtitle_is_identified_as_webvtt() -> None:
    r = FakeRunner()
    r.on("ffprobe", stdout=json.dumps(_mkv_with_unnamed_subtitle()))
    r.on(*REPROBE_2, stdout=json.dumps({"streams": [{"index": 2, "extradata": WEBVTT_DUMP}]}))
    info = probe(r, "/m/a.mkv")
    srt, vtt = info.subtitles
    assert (srt.codec, srt.reader, srt.treatment) == ("subrip", "ffmpeg", "soft")
    assert (vtt.codec, vtt.reader, vtt.treatment) == ("webvtt", "mkvextract", "soft")
    # Only the unnamed stream is probed again.
    assert sum(1 for c in r.calls if "-show_data" in c) == 1


def test_unidentified_subtitle_stays_unsupported() -> None:
    r = FakeRunner()
    r.on("ffprobe", stdout=json.dumps(_mkv_with_unnamed_subtitle()))
    vtt = probe(r, "/m/a.mkv").subtitles[1]
    assert (vtt.codec, vtt.treatment) == ("", "unsupported")


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (1, json.dumps({"streams": [{"extradata": WEBVTT_DUMP}]})),
        (0, "not json"),
        (0, json.dumps({"streams": []})),
        (0, json.dumps(["not", "an", "object"])),
    ],
)
def test_failed_reprobe_leaves_the_subtitle_unsupported(returncode: int, stdout: str) -> None:
    r = FakeRunner()
    r.on("ffprobe", stdout=json.dumps(_mkv_with_unnamed_subtitle()))
    r.on(*REPROBE_2, returncode=returncode, stdout=stdout)
    vtt = probe(r, "/m/a.mkv").subtitles[1]
    assert (vtt.codec, vtt.reader, vtt.treatment) == ("", "ffmpeg", "unsupported")


def test_other_containers_are_not_probed_again() -> None:
    data = _mkv_with_unnamed_subtitle()
    data["format"]["format_name"] = "mov,mp4,m4a"
    r = FakeRunner()
    r.on("ffprobe", stdout=json.dumps(data))
    probe(r, "/m/a.mp4")
    assert len(r.calls) == 1
