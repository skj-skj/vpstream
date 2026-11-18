"""Which platform vpstream acts for."""

from __future__ import annotations

import sysconfig
from pathlib import Path

import pytest

from vpstream import bench, platform
from vpstream.platform.linux import ffmpeg_install


@pytest.mark.parametrize(
    ("system", "environ", "found"),
    [
        ("linux", {}, platform.LINUX),
        ("android", {}, platform.TERMUX),  # Termux's Python from 3.13
        ("linux", {"TERMUX_VERSION": "0.118.3"}, platform.TERMUX),  # an older one
        ("win32", {}, platform.WINDOWS),
    ],
)
def test_detect(system: str, environ: dict[str, str], found: str) -> None:
    assert platform.detect(system, environ) == found


@pytest.mark.parametrize("system", ["cygwin", "darwin"])
def test_detect_refuses_what_is_not_supported(system: str) -> None:
    with pytest.raises(platform.Unsupported, match="does not run on"):
        platform.detect(system, {})


def test_the_environment_chooses(monkeypatch: pytest.MonkeyPatch) -> None:
    platform.override(None)
    monkeypatch.setenv(platform.ENV, "termux")
    platform.override(None)  # forgets what was found
    assert platform.current() == platform.TERMUX
    monkeypatch.setenv(platform.ENV, "darwin")
    platform.override(None)
    with pytest.raises(platform.Unsupported, match="must be one of linux, termux, windows"):
        platform.current()
    monkeypatch.delenv(platform.ENV)
    platform.override(None)


def test_override_wins_and_is_checked() -> None:
    platform.override(platform.TERMUX)
    try:
        assert platform.is_termux()
        with pytest.raises(ValueError, match="not a platform"):
            platform.override("darwin")
    finally:
        platform.override(None)


def test_the_package_does_not_shadow_the_standard_platform_module() -> None:
    stdlib = Path(sysconfig.get_paths()["stdlib"]).resolve()
    for module in (bench, ffmpeg_install):
        assert Path(module.platform.__file__).resolve().parent == stdlib
        assert callable(module.platform.machine)
