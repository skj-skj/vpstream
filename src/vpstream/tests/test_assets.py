from __future__ import annotations

import re
from importlib import resources

ASSETS = resources.files("vpstream.assets")


def test_bundled_hlsjs_matches_its_version_file() -> None:
    version = ASSETS.joinpath("hls.js.version").read_text().strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), version
    # hls.min.js carries its own version as a string literal; a partial update would differ.
    assert f'"{version}"'.encode() in ASSETS.joinpath("hls.min.js").read_bytes()


def test_hlsjs_license_is_bundled() -> None:
    assert "Apache License" in ASSETS.joinpath("hls.js.LICENSE").read_text()
