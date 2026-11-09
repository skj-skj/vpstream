from __future__ import annotations

import pytest

from vpstream import ffargs
from vpstream.ffargs import EncodeSettings, Window


def _after(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def test_input_window_before_input() -> None:
    args = ffargs.input_args("in.mkv", Window(30.0, 120.0))
    assert args == ["-ss", "30.000", "-t", "120.000", "-i", "in.mkv"]
    assert ffargs.input_args("in.mkv", Window()) == ["-i", "in.mkv"]
    assert "-copyts" in ffargs.input_args("in.mkv", Window(5, 1), copyts=True)


def test_copy_hls_hevc_gets_hvc1_tag_and_fmp4() -> None:
    args = ffargs.copy_hls("in.mkv", "/out", video_codec="hevc")
    assert _after(args, "-c:v") == "copy"
    assert _after(args, "-tag:v") == "hvc1"
    assert _after(args, "-hls_segment_type") == "fmp4"
    assert _after(args, "-c:a") == "aac"
    assert args[-1] == "/out/index.m3u8"
    assert "-sn" in args


def test_copy_hls_h264_has_no_tag() -> None:
    assert "-tag:v" not in ffargs.copy_hls("in.mkv", "/out", video_codec="h264")


def test_transcode_settings() -> None:
    args = ffargs.transcode_hls("in.mkv", "/out", settings=EncodeSettings(preset="medium"))
    assert _after(args, "-c:v") == "libx264"
    assert _after(args, "-preset") == "medium"
    assert _after(args, "-maxrate") == "8M"
    assert _after(args, "-pix_fmt") == "yuv420p"
    assert _after(args, "-hls_time") == "6"
    assert "expr:gte(t,n_forced*6)" in args
    assert "-vf" not in args  # SDR: no tone mapping, no scaling


def test_transcode_hdr_tonemaps_only_when_allowed() -> None:
    args = ffargs.transcode_hls("in.mkv", "/out", hdr=True)
    assert "tonemap=tonemap=hable" in _after(args, "-vf")
    assert "-vf" not in ffargs.transcode_hls("in.mkv", "/out", hdr=True, tonemap=False)


def test_burn_ass_uses_subtitles_filter_with_zero_based_index() -> None:
    graph = ffargs.burn_filter(
        sub_number=2, image=False, filter_src="/tmp/x/source.mkv", hdr=False, tonemap=True
    )
    assert graph == (
        "[0:v:0]subtitles=filename=/tmp/x/source.mkv:si=1,setpts=PTS-STARTPTS,format=yuv420p[v]"
    )


def test_burn_image_is_scaled_to_the_video_then_overlaid() -> None:
    graph = ffargs.burn_filter(sub_number=1, image=True, filter_src="", hdr=False, tonemap=True)
    assert graph.startswith("[0:v:0]null[base];[0:s:0][base]scale2ref=w=rw:h=rh[sub][ref];")
    assert "[ref][sub]overlay=eof_action=pass" in graph and graph.endswith("[v]")
    hdr = ffargs.burn_filter(sub_number=3, image=True, filter_src="", hdr=True, tonemap=True)
    assert hdr.startswith("[0:v:0]zscale") and "[0:s:2][base]scale2ref" in hdr


def test_hls_output_args_options() -> None:
    args = ffargs.hls_output_args(
        "/o", fmp4=False, seconds=2, segment="s_%v", playlist="v_%v.m3u8", extra=("-x", "y")
    )
    assert _after(args, "-hls_time") == "2"
    assert _after(args, "-hls_segment_type") == "mpegts"
    assert args[-3:] == ["-hls_segment_filename", "/o/s_%v.ts", "/o/v_%v.m3u8"]
    assert args.index("-x") < args.index("-hls_segment_filename")


def test_burn_args_keep_timestamps_and_drop_audio() -> None:
    args = ffargs.burn_hls(
        "in.mkv", "/out", sub_number=1, image=True, filter_src="", window=Window(60, 30)
    )
    assert "-copyts" in args and args.index("-copyts") < args.index("-i")
    assert "-an" in args
    assert _after(args, "-map") == "[v]"


@pytest.mark.parametrize("bad", ["/a b/x.mkv", "/a:b.mkv", "it's.mkv", "/x[1].mkv", "a,b"])
def test_filter_path_rejects_special_characters(bad: str) -> None:
    with pytest.raises(ValueError):
        ffargs.escape_filter_path(bad)


def test_parse_ffmpeg_list() -> None:
    text = """Encoders:
 V..... = Video
 ------
 V....D libx264              libx264 H.264
 A....D aac                  AAC
"""
    names = ffargs.parse_ffmpeg_list(text)
    assert {"libx264", "aac"} <= names
    filters = " TS overlay          VV->V      Overlay\n .. subtitles  V->V  Render\n"
    assert ffargs.parse_ffmpeg_list(filters) == {"overlay", "subtitles"}


def test_image_burn_after_a_seek_reads_subtitles_from_an_earlier_input() -> None:
    args = ffargs.burn_hls(
        "in.mkv", "/out", sub_number=2, image=True, filter_src="", window=Window(100, 60)
    )
    assert args.count("-i") == 2
    first = args.index("-i")
    # No -t: once this input ends, the overlay drops cues it has not drawn yet. No
    # -fix_sub_duration, which drops the first cue the input reads.
    assert args[first + 2 : first + 6] == ["-ss", "70.000", "-i", "in.mkv"]
    assert "-fix_sub_duration" not in args
    assert "[1:s:1][base]scale2ref" in _after(args, "-filter_complex")


def test_image_burn_preroll_stops_at_the_file_start() -> None:
    args = ffargs.burn_hls(
        "in.mkv", "/out", sub_number=1, image=True, filter_src="", window=Window(10, 60)
    )
    first = args.index("-i")
    assert args[first + 2 : first + 4] == ["-i", "in.mkv"]


def test_burn_from_the_start_or_with_text_uses_one_input() -> None:
    whole = ffargs.burn_hls("in.mkv", "/out", sub_number=1, image=True, filter_src="")
    assert whole.count("-i") == 1 and "[0:s:0][base]" in _after(whole, "-filter_complex")
    text = ffargs.burn_hls(
        "in.mkv", "/out", sub_number=1, image=False, filter_src="/t/s.mkv", window=Window(100)
    )
    assert text.count("-i") == 1  # libass reads the whole file by name
