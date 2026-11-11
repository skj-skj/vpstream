from __future__ import annotations

import pytest

from vpstream import text


@pytest.mark.parametrize(
    ("seconds", "shown"),
    [
        (5, "5s"),
        (59.9, "59s"),
        (95, "1m 35s"),
        (192, "3m 12s"),
        (7380, "2h 03m"),
        (7500, "2h 05m"),
        (273600, "3d 4h"),
    ],
)
def test_duration(seconds: float, shown: str) -> None:
    assert text.duration(seconds) == shown


@pytest.mark.parametrize(
    ("count", "shown"),
    [(0, "0 B"), (999, "999 B"), (1000, "1.0 kB"), (2.5e9, "2.5 GB"), (5e12, "5000.0 GB")],
)
def test_size(count: float, shown: str) -> None:
    assert text.size(count) == shown
