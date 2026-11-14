from __future__ import annotations

from pathlib import Path

import pytest

from vpstream import config


def test_defaults_and_default_text_agree() -> None:
    assert config.parse("") == config.Config()
    assert config.parse(config.DEFAULT_TEXT) == config.Config(stream_port=1337, dashboard_port=1338)


def test_values_are_read() -> None:
    cfg = config.parse("[stream]\nport = 2000\n[dashboard]\nport = 2001\n")
    assert (cfg.stream_port, cfg.dashboard_port) == (2000, 2001)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[stream]\nport = 80\n", "from 1024 to 65535"),
        ("[stream]\nport = 70000\n", "from 1024 to 65535"),
        ('[stream]\nport = "1337"\n', "whole number"),
        ("[stream]\nport = true\n", "whole number"),
        ("[stream]\nprot = 1337\n", "unknown setting 'stream.prot'"),
        ("port = 1337\n", "unknown setting 'port'"),
        ("[stream]\nport = 1338\n", "both 1338"),
        ("[stream\n", "not valid TOML"),
    ],
)
def test_bad_values_are_refused(text: str, message: str) -> None:
    with pytest.raises(config.ConfigError, match=message):
        config.parse(text)


def test_every_problem_is_listed() -> None:
    with pytest.raises(config.ConfigError) as exc:
        config.parse("[stream]\nport = 1\n[dashboard]\nport = 2\nx = 1\n")
    assert str(exc.value).count(";") == 2


def test_load(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    assert config.load(path) == config.Config()
    path.write_text("[dashboard]\nport = 4000\n")
    assert config.load(path).dashboard_port == 4000
    path.write_text("[dashboard]\nport = 1\n")
    with pytest.raises(config.ConfigError, match=str(path)):
        config.load(path)


def test_paths_under_a_root(tmp_path: Path) -> None:
    paths = config.Paths.under(tmp_path)
    assert paths.config_file == tmp_path / "etc/vpstream/config.toml"
    assert paths.socket == tmp_path / "run/vpstream/daemon.sock"
    assert config.Paths().socket == Path("/run/vpstream/daemon.sock")


def test_stream_host() -> None:
    assert config.parse('[stream]\nhost = "203.0.113.5"').stream_host == "203.0.113.5"
    assert config.parse('[stream]\nhost = "tv.example.org"\nport = 2000').stream_port == 2000
    assert config.parse("").stream_host == ""
    for bad in ('"a b"', '"http://x/"', "5", '"x/y"'):
        with pytest.raises(config.ConfigError, match=r"stream\.host must be"):
            config.parse(f"[stream]\nhost = {bad}")
    with pytest.raises(config.ConfigError, match=r"stream\.port must be a whole number"):
        config.parse('[stream]\nport = "1337"')


def test_the_default_text_still_parses_to_the_defaults() -> None:
    assert config.parse(config.DEFAULT_TEXT) == config.Config()
    uncommented = config.DEFAULT_TEXT.replace('# host = "', 'host = "')
    assert config.parse(uncommented).stream_host == "203.0.113.5"


def test_paths_for_caddy_live_under_the_root(tmp_path: Path) -> None:
    paths = config.Paths.under(tmp_path)
    assert paths.gate_socket == tmp_path / "run/vpstream/gate.sock"
    assert paths.access_log == tmp_path / "var/lib/vpstream/caddy/access.log"


def test_prepare_speeds_from_bench() -> None:
    cfg = config.parse("[prepare]\ncopy_speed = 14.5\nencode_speed = 2\n")
    assert (cfg.copy_speed, cfg.encode_speed) == (14.5, 2)
    assert (config.Config().copy_speed, config.Config().encode_speed) == (11.0, 1.6)
    for bad in ("0", "-1", "true", '"fast"', "5000"):
        with pytest.raises(config.ConfigError, match="times real time"):
            config.parse(f"[prepare]\nencode_speed = {bad}\n")
    assert config.parse(config.DEFAULT_TEXT) == config.Config()


def test_media_folder() -> None:
    assert config.Config().media_folder == "/srv/media"
    assert config.parse('[media]\nfolder = "/data/films"\n').media_folder == "/data/films"
    for bad in ('"films"', "3", '""'):
        with pytest.raises(config.ConfigError, match="full path"):
            config.parse(f"[media]\nfolder = {bad}\n")


# --- where the dashboard is


def test_the_dashboard_is_on_the_tailnet_unless_the_file_says_public() -> None:
    assert config.parse("").dashboard_public is False
    assert config.parse(config.DEFAULT_TEXT).dashboard_public is False
    assert config.parse("[dashboard]\npublic = true\n").dashboard_public is True


@pytest.mark.parametrize("value", ['"yes"', "1", "0", '"true"'])
def test_public_must_be_true_or_false(value: str) -> None:
    with pytest.raises(config.ConfigError, match="true or false"):
        config.parse(f"[dashboard]\npublic = {value}\n")


@pytest.mark.parametrize(
    ("text", "expect"),
    [
        (config.DEFAULT_TEXT, None),  # the line written by init: changed in place
        ("", "[dashboard]\npublic = true\n"),
        ("[stream]\nport = 2000", "[stream]\nport = 2000\n\n[dashboard]\npublic = true\n"),
        ("[dashboard]\nport = 1400\n", "[dashboard]\npublic = true\nport = 1400\n"),
        (
            "[ dashboard ]  # mine\nport = 1400\n",
            "[ dashboard ]  # mine\npublic = true\nport = 1400\n",
        ),
        (
            "[dashboard]\n# public = false\npublic=false # off\n[media]\n",
            "[dashboard]\n# public = false\npublic = true\n[media]\n",
        ),
        ("[media]\npublic_x = 1\n", None),  # an unknown key elsewhere stays an error
    ],
)
def test_init_sets_public_and_leaves_every_other_line(text: str, expect: str | None) -> None:
    if "public_x" in text:
        with pytest.raises(config.ConfigError):
            config.with_public_dashboard(text, True)
        return
    result = config.with_public_dashboard(text, True)
    assert config.parse(result).dashboard_public is True
    if expect is not None:
        assert result == expect
    else:
        changed = [
            (a, b) for a, b in zip(text.splitlines(), result.splitlines(), strict=True) if a != b
        ]
        assert changed == [("public = false", "public = true")]
    back = config.with_public_dashboard(result, False)
    assert config.parse(back).dashboard_public is False
    assert config.with_public_dashboard(back, False) == back


def test_a_public_setting_written_another_way_is_not_guessed_at() -> None:
    with pytest.raises(config.ConfigError, match="cannot set"):
        config.with_public_dashboard("dashboard.public = false\n", True)
    with pytest.raises(config.ConfigError, match="cannot set"):
        config.with_public_dashboard('[dashboard]\n"public" = false\n', True)
