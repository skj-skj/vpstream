"""Tests that run the real ffmpeg on tiny generated files. Run with --integration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vpstream import bench
from vpstream.caps import clips
from vpstream.ffargs import VIDEO, base_args
from vpstream.probe import SubtitleTrack, probe
from vpstream.tests.media import ffmpeg
from vpstream.tools import SystemRunner

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("need_ffmpeg")]

DATA = Path(__file__).parent / "data"
# The bundled fixture's three PGS cues (0-2.5, 3-5.5 and 6-8.5 s) are moved this far into
# the sample, so a seek to a cue is past ffargs.SUBTITLE_PREROLL_SECONDS.
CUES_AT = 30


def test_probe_real_file(sample: Path) -> None:
    info = probe(SystemRunner(), sample)
    assert info.video[0].codec == "h264"
    assert [a.language for a in info.audio] == ["eng", "jpn"]
    assert [s.treatment for s in info.subtitles] == ["soft", "burned in"]
    assert info.duration == pytest.approx(12, abs=0.5)


def test_bench_real_file(sample: Path, tmp_path: Path) -> None:
    report = bench.benchmark(
        SystemRunner(), sample, sample=6, presets=("ultrafast",), tmp_dir=str(tmp_path)
    )
    failed = [(s.name, s.error) for s in report.steps if not s.ok]
    assert not failed
    assert [s.name for s in report.steps] == ["copy", "transcode ultrafast", "burn-in ultrafast"]
    assert all(s.output_bytes > 0 and s.speed > 0 for s in report.steps)
    json.dumps(report.to_json())


def test_caps_clips(tmp_path: Path) -> None:
    manifest = clips.generate(SystemRunner(), tmp_path / "c")
    items = manifest["clips"]
    assert isinstance(items, list)
    h264 = next(c for c in items if c["id"] == "h264-8")
    assert h264["mp4"] and h264["hls"], h264
    assert (tmp_path / "c" / "h264-8" / "index.m3u8").is_file()
    master = (tmp_path / "c" / "multi-audio" / "master.m3u8").read_text()
    assert master.count("TYPE=AUDIO") == 2, master
    assert (tmp_path / "c" / "subs.vtt").read_text().startswith("WEBVTT")
    # Reused, not regenerated, on the next call.
    assert clips.generate(SystemRunner(), tmp_path / "c") == manifest


def _first_frame_peak(hls_dir: Path) -> int:
    """Brightest luma value in the first frame of an HLS output (black video: 16)."""
    key = "lavfi.signalstats.YMAX"
    res = SystemRunner().run(
        [
            *base_args("info"),
            "-i",
            str(hls_dir / f"{VIDEO}.m3u8"),
            "-frames:v",
            "1",
            "-vf",
            f"signalstats,metadata=print:key={key}",
            "-f",
            "null",
            "-",
        ],
        timeout=60,
    )
    assert res.returncode == 0, res.stderr
    peaks = [int(line.rsplit("=", 1)[1]) for line in res.stderr.splitlines() if key in line]
    assert peaks, res.stderr
    return peaks[0]


@pytest.fixture(scope="module", params=["pgs", "dvdsub", "dvbsub"])
def image_sample(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """40 s of black video with three image cues: 30-32.5, 33-35.5 and 36-38.5 s."""
    d = tmp_path_factory.mktemp(request.param)
    sup = cues = DATA / "three-cues.sup"
    if request.param != "pgs":
        # -fix_sub_duration turns the PGS clear events into cue end times.
        cues = d / "cues.mks"
        ffmpeg("-fix_sub_duration", "-i", sup, "-c:s", request.param, "-f", "matroska", cues)
    out = d / "sample.mkv"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=black:size=640x360:rate=25:duration=40",
        "-itsoffset",
        str(CUES_AT),
        "-i",
        cues,
        "-map",
        "0:v",
        "-map",
        "1:s",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-c:s",
        "copy",
        out,
    )
    return out


@pytest.mark.parametrize(
    ("start", "visible"),
    [
        (10.0, False),  # before any cue; the subtitle input starts at the file start
        (31.0, True),  # inside cue 1, the first event the subtitle input reads
        (34.0, True),  # inside cue 2, which started before the seek point
        (35.7, False),  # between cues: nothing may be left on screen
        (37.0, True),  # inside cue 3, which has no event after it
    ],
)
def test_image_subtitle_burn_after_a_seek(
    image_sample: Path, tmp_path: Path, start: float, visible: bool
) -> None:
    from vpstream.ffargs import Window, burn_hls

    args = burn_hls(
        str(image_sample),
        tmp_path,
        sub_number=1,
        image=True,
        filter_src="",
        window=Window(start, 0.2),
        loglevel="error",
    )
    res = SystemRunner().run(args, timeout=120)
    assert res.returncode == 0, res.stderr
    assert (_first_frame_peak(tmp_path) > 128) is visible


@pytest.fixture(scope="module")
def mkvmerge_webvtt(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Video, then a Spanish and a French WebVTT track, which mkvmerge stores as
    S_TEXT/WEBVTT, a codec ID ffmpeg cannot read."""
    runner = SystemRunner()
    if not runner.which("mkvmerge"):
        pytest.skip("mkvmerge (mkvtoolnix) is not installed")
    d = tmp_path_factory.mktemp("vtt")
    spa, fre = d / "spa.vtt", d / "fre.vtt"
    spa.write_text("WEBVTT\n\n00:00:00.500 --> 00:00:02.000 line:85% align:center\nHola\n")
    fre.write_text("WEBVTT\n\n00:00:00.500 --> 00:00:02.000\nBonjour\n")
    video = d / "v.mkv"
    ffmpeg("-f", "lavfi", "-i", "color=black:size=320x180:duration=3", "-c:v", "libx264", video)
    out = d / "vtt.mkv"
    args = ["mkvmerge", "-q", "-o", out, video, "--language", "0:spa", spa]
    res = runner.run([*map(str, args), "--language", "0:fre", str(fre)])
    assert res.returncode == 0, res.stdout + res.stderr
    return out


def _extract(src: Path, track: SubtitleTrack, out: Path) -> str:
    from vpstream.subtitles import extract_webvtt

    extract_webvtt(SystemRunner(), str(src), track, out)
    return out.read_text(encoding="utf-8-sig")


def test_webvtt_from_mkvmerge_is_soft_and_extracted(mkvmerge_webvtt: Path, tmp_path: Path) -> None:
    from vpstream.probe import MKVEXTRACT

    spa, fre = probe(SystemRunner(), mkvmerge_webvtt).subtitles
    for track in (spa, fre):
        assert (track.codec, track.reader, track.treatment) == ("webvtt", MKVEXTRACT, "soft")
    text = _extract(mkvmerge_webvtt, spa, tmp_path / "spa.vtt")
    assert text.startswith("WEBVTT")
    assert "00:00:00.500 --> 00:00:02.000 line:85% align:center\nHola" in text
    assert "Bonjour" in _extract(mkvmerge_webvtt, fre, tmp_path / "fre.vtt")


def test_webvtt_track_found_when_ffmpeg_skips_a_track(
    mkvmerge_webvtt: Path, tmp_path: Path
) -> None:
    # Turn the Spanish track into a "buttons" track (TrackType 0x12), which ffmpeg skips and
    # mkvextract still counts: the French track is then stream 1 but mkvextract track 2.
    data = bytearray(mkvmerge_webvtt.read_bytes())
    at = data.find(b"\x83\x81\x11")  # TrackType, 1 byte: subtitle
    assert at > 0
    data[at + 2] = 0x12
    src = tmp_path / "skipped.mkv"
    src.write_bytes(data)
    (fre,) = probe(SystemRunner(), src).subtitles
    assert (fre.index, fre.language) == (1, "fre")
    text = _extract(src, fre, tmp_path / "fre.vtt")
    assert "Bonjour" in text and "Hola" not in text
