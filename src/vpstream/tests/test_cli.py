from __future__ import annotations

from pathlib import Path

import pytest

from vpstream import cli, net, selftest


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 2
    assert "bench" in capsys.readouterr().out


def test_bench_missing_file(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["bench", "/no/such/file.mkv"]) == 2
    assert "not a file" in capsys.readouterr().err


def test_selftest_args() -> None:
    args = selftest.pytest_args(True, ["-k", "probe"])
    assert args[0] == str(selftest.TESTS_DIR)
    assert "--integration" in args
    assert args[-2:] == ["-k", "probe"]
    assert "--integration" not in selftest.pytest_args(False, [])


def test_selftest_without_pytest_names_the_system_wide_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "pytest", None)  # makes `import pytest` fail
    assert selftest.run() == 2
    err = capsys.readouterr().err
    # Not a plain `pipx install`: that makes the per-user install init refuses.
    assert 'sudo PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx install --force "vp' in err


@pytest.mark.parametrize("port", ["0", "70000", "x"])
def test_caps_port_is_checked(port: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["caps", "--port", port])
    assert exc.value.code == 2
    assert "port number" in capsys.readouterr().err


def test_bench_checks_paths_before_running(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    assert cli.main(["bench", str(f), "--tmp", str(tmp_path / "nope")]) == 2
    assert "--tmp" in capsys.readouterr().err
    assert cli.main(["bench", str(f), "--json", str(tmp_path / "nope" / "r.json")]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_caps_refuses_a_user_folder(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mine = tmp_path / "videos"
    mine.mkdir()
    (mine / "film.mkv").write_text("x")
    out = str(tmp_path / "r.json")
    assert cli.main(["caps", "--clips", str(mine), "--out", out]) == 1
    assert "not made by vpstream" in capsys.readouterr().err
    assert (mine / "film.mkv").is_file()


def test_caps_checks_the_results_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["caps", "--out", str(tmp_path / "nope" / "r.json")]) == 2
    assert "does not exist" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("host", "shown"),
    [("0.0.0.0", "<server-ip>"), ("::", "<server-ip>"), ("::1", "[::1]"), ("10.0.0.2", "10.0.0.2")],
)
def test_url_host(host: str, shown: str) -> None:
    assert net.url_host(host) == shown
