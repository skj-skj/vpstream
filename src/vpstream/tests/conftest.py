from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="run tests that need ffmpeg and other real tools on this host",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: needs real tools such as ffmpeg")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--integration"):
        return
    skip = pytest.mark.skip(reason="integration test: run with --integration")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def need_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            pytest.fail(f"{tool} is required for integration tests")


@pytest.fixture
def short_tmp() -> Iterator[Path]:
    """A temporary folder with a short path: a unix socket path may be at most 107 bytes."""
    path = Path(tempfile.mkdtemp(prefix="vp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="module")
def sample(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 12 s H.264 file with two audio tracks, an SRT and an ASS subtitle track."""
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            pytest.fail(f"{tool} is required for integration tests")
    from vpstream.tests.media import make_sample

    return make_sample(tmp_path_factory.mktemp("media"))
