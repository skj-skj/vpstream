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


def test_a_copy_keeps_the_video_and_tags_hevc_for_browsers() -> None:
    args = ffargs.base_hls("in.mkv", "/out", copy=True, video_codec="hevc", audio=["eng"])
    assert _after(args, "-c:v") == "copy"
    assert _after(args, "-tag:v") == "hvc1"
    assert _after(args, "-hls_segment_type") == "fmp4"
    assert _after(args, "-c:a") == "aac"
    assert args[-1] == "/out/%v.m3u8" and _after(args, "-master_pl_name") == "master.m3u8"
    assert "-sn" in args
    assert "-tag:v" not in ffargs.base_hls("in.mkv", "/o", copy=True, video_codec="h264")


def test_every_audio_track_becomes_its_own_alternate() -> None:
    args = ffargs.base_hls(
        "in.mkv", "/o", copy=True, video_codec="h264", audio=["eng", "jpn", ""], default_audio=1
    )
    maps = [args[i + 1] for i, a in enumerate(args) if a == "-map"]
    assert maps == ["0:V:0", "0:a:0", "0:a:1", "0:a:2"]
    assert _after(args, "-var_stream_map") == (
        "v:0,agroup:aud,name:video "
        "a:0,agroup:aud,language:eng,name:a1 "
        "a:1,agroup:aud,language:jpn,name:a2,default:yes "
        "a:2,agroup:aud,language:und,name:a3"
    )


def test_a_video_without_audio_has_no_audio_group_or_encoder() -> None:
    args = ffargs.base_hls("in.mkv", "/o", copy=True, video_codec="h264")
    assert _after(args, "-var_stream_map") == "v:0,name:video"
    assert "-c:a" not in args and maps_only_video(args)


def maps_only_video(args: list[str]) -> bool:
    return [args[i + 1] for i, a in enumerate(args) if a == "-map"] == ["0:V:0"]


@pytest.mark.parametrize(
    ("language", "code"),
    [
        ("ENG", "eng"),
        ("en", "en"),
        ("und", "und"),
        ("", "und"),
        ("e n", "und"),
        ("x,y", "und"),
        ("pt-BR", "und"),
        ("english", "und"),
    ],
)
def test_languages_that_could_break_the_stream_map_become_und(language: str, code: str) -> None:
    assert ffargs.hls_language(language) == code


def test_transcode_settings() -> None:
    args = ffargs.base_hls(
        "in.mkv", "/out", copy=False, video_codec="hevc", settings=EncodeSettings(preset="medium")
    )
    assert _after(args, "-c:v") == "libx264"
    assert _after(args, "-preset") == "medium"
    assert _after(args, "-maxrate") == "8M"
    assert _after(args, "-pix_fmt") == "yuv420p"
    assert _after(args, "-hls_time") == "6"
    assert "expr:gte(t,n_forced*6)" in args
    assert "-vf" not in args and "-tag:v" not in args  # SDR: no tone mapping, no scaling


def test_transcode_hdr_tonemaps_only_when_allowed() -> None:
    args = ffargs.base_hls("in.mkv", "/out", copy=False, video_codec="hevc", hdr=True)
    assert "tonemap=tonemap=hable" in _after(args, "-vf")
    no_zscale = ffargs.base_hls(
        "in.mkv", "/out", copy=False, video_codec="hevc", hdr=True, tonemap=False
    )
    assert "-vf" not in no_zscale
    assert "-vf" not in ffargs.base_hls("in.mkv", "/o", copy=True, video_codec="hevc", hdr=True)


@pytest.mark.parametrize(
    ("codec", "pix_fmt", "depth", "ok"),
    [
        ("h264", "yuv420p", 8, True),
        ("h264", "yuvj420p", 8, True),
        ("h264", "yuv420p10le", 10, False),  # Hi10P: no browser decodes it
        ("h264", "yuv422p", 8, False),
        ("hevc", "yuv420p10le", 10, True),
        ("hevc", "yuv422p10le", 10, False),
        ("hevc", "yuv420p12le", 12, False),
        ("av1", "yuv420p10le", 10, True),
        ("vp9", "yuv420p", 8, False),
        ("mpeg4", "yuv420p", 8, False),
    ],
)
def test_which_videos_a_browser_plays_as_they_are(
    codec: str, pix_fmt: str, depth: int, ok: bool
) -> None:
    assert ffargs.copy_ok(codec, pix_fmt, depth) is ok


def test_progress_is_read_from_out_time() -> None:
    assert ffargs.progress_seconds("out_time_us=12500000") == 12.5
    assert ffargs.progress_seconds("out_time_us=N/A") is None
    assert ffargs.progress_seconds("out_time_us=-7") == 0.0
    assert ffargs.progress_seconds("out_time_ms=12500000") is None
    assert ffargs.progress_seconds("progress=end") is None
    args = ffargs.with_progress(["ffmpeg", "-i", "x", "out"])
    assert args == ["ffmpeg", "-progress", "pipe:1", "-nostats", "-i", "x", "out"]


def test_burn_ass_uses_subtitles_filter_with_zero_based_index() -> None:
    graph = ffargs.burn_filter(
        sub_number=2, image=False, filter_src="/tmp/x/source.mkv", hdr=False, tonemap=True
    )
    assert graph == (
        "[0:V:0]subtitles=filename=/tmp/x/source.mkv:si=1,setpts=PTS-STARTPTS,format=yuv420p[v]"
    )


def test_burn_image_is_scaled_to_the_video_then_overlaid() -> None:
    graph = ffargs.burn_filter(sub_number=1, image=True, filter_src="", hdr=False, tonemap=True)
    assert graph.startswith("[0:V:0]null[base];[0:s:0][base]scale2ref=w=rw:h=rh[sub][ref];")
    assert "[ref][sub]overlay=eof_action=pass" in graph and graph.endswith("[v]")
    hdr = ffargs.burn_filter(sub_number=3, image=True, filter_src="", hdr=True, tonemap=True)
    assert hdr.startswith("[0:V:0]zscale") and "[0:s:2][base]scale2ref" in hdr


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


def test_a_video_alone_gets_its_init_segment_name_written_out() -> None:
    # ffmpeg fills in %v of the init name only for two renditions or more: a burned-in
    # version (video alone) got a playlist naming "%v_init.mp4", which no browser fetches.
    burn = ffargs.burn_hls("in.mkv", "/o", sub_number=1, image=True, filter_src="")
    assert _after(burn, "-hls_fmp4_init_filename") == "video_init.mp4"
    silent = ffargs.base_hls("in.mkv", "/o", copy=True, video_codec="h264")
    assert _after(silent, "-hls_fmp4_init_filename") == "video_init.mp4"
    loud = ffargs.base_hls("in.mkv", "/o", copy=True, video_codec="h264", audio=["eng"])
    assert _after(loud, "-hls_fmp4_init_filename") == "%v_init.mp4"
