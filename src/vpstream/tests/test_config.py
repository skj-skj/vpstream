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
