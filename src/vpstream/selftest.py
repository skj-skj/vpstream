"""`vpstream selftest`: run the tests that ship inside the package."""

from __future__ import annotations

import sys
from pathlib import Path

from vpstream.doctor import SYSTEM_INSTALL

TESTS_DIR = Path(__file__).parent / "tests"


def pytest_args(integration: bool, extra: list[str]) -> list[str]:
    args = [str(TESTS_DIR), "--rootdir", str(TESTS_DIR), "-p", "no:cacheprovider", "-q"]
    if integration:
        args.append("--integration")
    return [*args, *extra]


def run(integration: bool = False, extra: list[str] | None = None) -> int:
    try:
        import pytest
    except ImportError:
        print(
            "pytest is not installed. Install vpstream again with the dev extra:\n"
            f"  {SYSTEM_INSTALL.replace('pipx install', 'pipx install --force')}",
            file=sys.stderr,
        )
        return 2
    return int(pytest.main(pytest_args(integration, extra or [])))
