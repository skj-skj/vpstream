from __future__ import annotations

import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

if sys.platform == "win32":
    # What only Linux and Termux run: the daemon and its socket and the commands that ask
    # it, systemd, sudo, ufw, the installers that use apt or pkg, Termux and its dashboard,
    # bench. Windows has its own in platform/windows.
    collect_ignore = [
        "test_access.py",
        "test_bench.py",
        "test_caddy_install.py",
        "test_cli_prepare.py",
        "test_daemon.py",
        "test_doctor.py",
        "test_ffmpeg_install.py",
        "test_firewall.py",
        "test_host_integration.py",
        "test_integration.py",
        "test_local_dashboard.py",
        "test_service.py",
        "test_service_local.py",
        "test_termux.py",
        "test_up_down.py",
    ]


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="run tests that need ffmpeg and other real tools on this host",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: needs real tools such as ffmpeg")


POSIX_PATHS = "it expects POSIX paths (/x), which Windows writes C:\\x or \\x"
NOT_UTF8 = "a file name that is not UTF-8: Windows names are always Unicode"
CHMOD = "chmod does not take access away on Windows"
MODES = "file modes: Windows keeps files private with access lists, not modes"
HTTPS = "HTTPS with Let's Encrypt is for the Linux service"
LINUX = "a Linux command"
# Tests of what Windows does otherwise, by file::name (every parameter) or file::name[id].
WINDOWS_SKIPS = {
    "test_caddy.py::test_every_request_goes_to_the_gate_with_the_real_address": POSIX_PATHS,
    "test_caddy.py::test_a_file_is_sent_only_when_the_gate_names_one": POSIX_PATHS,
    "test_caddy.py::test_the_access_log_goes_to_its_own_file": POSIX_PATHS,
    "test_caddy.py::test_videos_are_linked_by_number": NOT_UTF8,
    "test_caddy.py::test_start_writes_the_configuration_and_waits_until_caddy_listens": (
        "the fake Caddy is a launcher whose Python ends a moment after it is stopped"
    ),
    "test_caddy.py::test_the_requesting_caddy_is_stopped_however_it_ends": HTTPS,
    "test_caddy.py::test_a_requesting_caddy_that_cannot_start_says_so": HTTPS,
    "test_caddy.py::test_a_busy_check_port_is_seen_before_port_80_is_opened": HTTPS,
    "test_certificate.py::test_the_command_line_waits_longer_than_a_request_can_take": HTTPS,
    "test_cli.py::test_bench_missing_file": LINUX,
    "test_cli.py::test_bench_checks_paths_before_running": LINUX,
    "test_cli.py::test_init_takes_the_mode_and_the_password": LINUX,
    "test_cli.py::test_dash_pass_without_a_terminal_needs_a_file": LINUX,
    "test_config.py::test_load": POSIX_PATHS,
    "test_config.py::test_media_folder": POSIX_PATHS,
    "test_config.py::test_a_test_authority_can_be_set": POSIX_PATHS,
    "test_config.py::test_init_sets_https_and_leaves_the_dashboard_as_it_is": POSIX_PATHS,
    "test_control.py::test_a_bad_request_touches_nothing": POSIX_PATHS,
    "test_control_browser.py::test_prepare_refuses_what_it_cannot_read[options4-not a file]": (
        POSIX_PATHS
    ),
    "test_control_browser.py::test_a_video_that_is_not_prepared_must_be_readable_for_"
    "browser_mode": CHMOD,
    "test_control_browser.py::test_a_folder_needs_access_only_to_the_episodes_not_prepared": CHMOD,
    "test_control_browser.py::test_direct_mode_says_a_prepared_video_can_play_in_browser_mode": (
        CHMOD
    ),
    "test_control_browser.py::test_removing_the_stale_ones_keeps_what_cannot_be_looked_at": CHMOD,
    "test_control_https.py::test_the_record_keeps_https": POSIX_PATHS,
    "test_dashboard_ca.py::test_the_authority_cannot_vouch_for_another_site": MODES,
    "test_gate.py::test_a_name_that_is_not_utf8_can_be_shown": NOT_UTF8,
    "test_listing.py::test_a_file_ffprobe_cannot_read_and_a_folder_that_cannot_be_entered": CHMOD,
    "test_listing.py::test_ls_lists_the_configured_folder_by_default": POSIX_PATHS,
    "test_local.py::test_interface_addresses_of_this_host": (
        "Windows finds its addresses another way (platform.windows)"
    ),
    "test_local.py::test_linux_daemon_local_keeps_the_firewall_helper": LINUX,
    "test_login.py::test_the_verifier_file_is_never_readable_by_others": MODES,
    "test_login.py::test_the_device_secret_is_made_once_and_kept_private": MODES,
    "test_prepared.py::test_a_file_that_cannot_be_looked_at_is_kept_not_stale": CHMOD,
    "test_prepared.py::test_gone_and_changed_files_are_stale": (
        "it changes a file's time by 1 ns; NTFS keeps 100 ns"
    ),
    "test_public.py::test_refusals_are_json_with_the_reason[stream/start-body0-not a file]": (
        POSIX_PATHS
    ),
    "test_public.py::test_a_file_name_that_is_not_utf8_is_listed_and_served": NOT_UTF8,
    "test_reindex.py::test_mkvmerge_is_told_to_index_every_video_and_audio_track": POSIX_PATHS,
    "test_stream.py::test_bad_sources[options4-not a file]": POSIX_PATHS,
    "test_stream.py::test_bad_sources[options5-not a folder]": POSIX_PATHS,
    "test_stream.py::test_a_file_the_service_cannot_read_is_refused_with_the_reason": CHMOD,
    "test_stream.py::test_a_folder_that_cannot_be_entered_is_unreadable_not_missing": CHMOD,
    "test_stream.py::test_a_folder_that_cannot_be_listed_is_unreadable": CHMOD,
    "test_stream.py::test_what_is_served_survives_a_round_trip": POSIX_PATHS,
    "test_subtitles.py::test_ffmpeg_reads_ordinary_text_tracks": POSIX_PATHS,
    "test_subtitles.py::test_mkvextract_args": POSIX_PATHS,
    "test_tools.py::test_the_key_of_a_file_changes_with_its_content_or_time": NOT_UTF8,
}


def _skip_on_windows(items: list[pytest.Item]) -> None:
    """What only POSIX does is skipped, with the reason (test_windows checks that every
    name here names a test)."""
    for item in items:
        name = getattr(item, "originalname", item.name)
        reason = WINDOWS_SKIPS.get(f"{item.path.name}::{item.name}") or WINDOWS_SKIPS.get(
            f"{item.path.name}::{name}"
        )
        if item.path.name == "test_public.py" and "show" in getattr(item, "fixturenames", ()):
            reason = "its file names have < and ?, which Windows does not allow"
        if reason:
            item.add_marker(pytest.mark.skip(reason=reason))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if sys.platform == "win32":
        _skip_on_windows(items)
    if config.getoption("--integration"):
        return
    skip = pytest.mark.skip(reason="integration test: run with --integration")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def on_linux() -> Iterator[None]:
    """Every test acts as on the Linux VPS, wherever it runs; a Termux test says so with
    the `on_termux` fixture."""
    from vpstream import platform

    platform.override(platform.LINUX)
    yield
    platform.override(None)


@pytest.fixture
def on_termux(on_linux: None) -> None:
    from vpstream import platform

    platform.override(platform.TERMUX)


@pytest.fixture
def on_windows(on_linux: None) -> None:
    """Act as on Windows: what the code decides there. What only a real Windows has
    (ctypes.windll, netsh) is faked in each test, or tested on Windows."""
    from vpstream import platform

    platform.override(platform.WINDOWS)


@pytest.fixture(autouse=True)
def quick_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """The daemon's servers in a test notice a shutdown within 0.05 s, where werkzeug looks
    every 0.5 s: a test daemon then stops in a moment, not in about a second."""
    from vpstream import daemon

    make = daemon._make_server

    def quick(*args: Any, **kwargs: Any) -> Any:
        server = make(*args, **kwargs)
        serve = server.serve_forever
        server.serve_forever = lambda poll_interval=0.05: serve(poll_interval)
        return server

    monkeypatch.setattr(daemon, "_make_server", quick)


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
