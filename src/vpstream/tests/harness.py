"""Running the node harnesses that check the page scripts (dashboard_test.js, player_test.js)."""

from __future__ import annotations

import shutil
import subprocess
from importlib import resources
from pathlib import Path

import pytest


def run(harness: str, *assets: str, at_least: int) -> list[str]:
    """Run tests/HARNESS on these files of vpstream.assets and return its lines, each a
    passed check. Skipped where node is not installed."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    files = resources.files("vpstream.assets")
    script = Path(__file__).with_name(harness)
    res = subprocess.run(
        [node, str(script), *(str(files.joinpath(name)) for name in assets)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    lines = res.stdout.splitlines()
    assert res.returncode == 0 and len(lines) >= at_least, res.stdout + res.stderr
    assert all(line.startswith("pass ") for line in lines), res.stdout
    return lines
