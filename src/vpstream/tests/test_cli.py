from __future__ import annotations

import pytest

from vpstream import __version__, cli, selftest


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


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


def test_tests_ship_inside_package() -> None:
    assert (selftest.TESTS_DIR / "conftest.py").is_file()
