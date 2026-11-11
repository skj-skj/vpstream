from __future__ import annotations

import json
import re
import shutil
import subprocess
from importlib import resources
from typing import Any

import pytest

ASSETS = resources.files("vpstream.assets")


def test_bundled_hlsjs_matches_its_version_file() -> None:
    version = ASSETS.joinpath("hls.js.version").read_text().strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), version
    # hls.min.js carries its own version as a string literal; a partial update would differ.
    assert f'"{version}"'.encode() in ASSETS.joinpath("hls.min.js").read_bytes()


def test_hlsjs_license_is_bundled() -> None:
    assert "Apache License" in ASSETS.joinpath("hls.js.LICENSE").read_text()


VTT_CHECK = """
global.window = {};
require(process.argv[1]);
const V = window.VpVtt;
const cues = V.parse(process.argv[2]);
console.log(JSON.stringify({cues, at: process.argv.slice(3).map(t => V.at(cues, +t))}));
"""


def vtt(text: str, *times: float) -> dict[str, Any]:
    """What the player's WebVTT reader makes of `text`, and shows at these times."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = resources.files("vpstream.assets").joinpath("vtt.js")
    res = subprocess.run(
        [node, "-e", VTT_CHECK, str(script), text, *map(str, times)],
        capture_output=True, text=True, timeout=30, check=True,
    )  # fmt: skip
    data: dict[str, Any] = json.loads(res.stdout)
    return data


def test_the_player_reads_webvtt_as_ffmpeg_and_mkvextract_write_it() -> None:
    text = (
        "﻿WEBVTT\r\n\r\n1\r\n00:00:01.000 --> 00:00:02.500 line:85% align:center\r\n"
        "<i>Hello</i> &amp; <c.yellow>bye</c>\r\n\r\n"
        "02:00.000 --> 02:01.000\r\nshort times\r\n\r\n"
        "00:00:02.000 --> 00:00:04.000\r\ntwo\r\nlines\r\n"
    )
    out = vtt(text, 1.5, 2.2, 3.0, 4.0, 120.5)
    assert [c["text"] for c in out["cues"]] == ["Hello & bye", "two\nlines", "short times"]
    assert out["at"] == ["Hello & bye", "Hello & bye\ntwo\nlines", "two\nlines", "", "short times"]


def test_cues_that_cannot_be_read_are_left_out() -> None:
    text = (
        "WEBVTT\n\nNOTE a note --> not a cue\n\n00:00:05.000 --> 00:00:01.000\nbackwards\n\n"
        "xx --> 00:00:01.000\nbad\n\n00:00:01,5 --> 00:00:02\nno ms on the end\n\n"
        "00:00:03.000 --> 00:00:04.000\nok\n"
    )
    assert [c["text"] for c in vtt(text)["cues"]] == ["ok"]
