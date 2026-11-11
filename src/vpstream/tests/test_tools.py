from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from vpstream import files, text
from vpstream.tools import Cancelled, run_cancellable

PY = sys.executable


def test_a_program_is_run_and_its_lines_come_as_they_are_written() -> None:
    seen: list[str] = []
    code, output = run_cancellable(
        [PY, "-c", "import sys; print('one'); print('two', file=sys.stderr); sys.exit(3)"],
        threading.Event(),
        seen.append,
    )
    assert code == 3 and sorted(seen) == ["one", "two"] and "one" in output


def test_only_the_last_lines_are_kept() -> None:
    code, output = run_cancellable([PY, "-c", "for i in range(500): print(i)"], threading.Event())
    assert code == 0 and output.splitlines()[-1] == "499" and "\n100\n" not in output


def test_cancel_ends_the_program_at_once() -> None:
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(Cancelled):
        run_cancellable([PY, "-c", "import time; print('x', flush=True); time.sleep(60)"], cancel)
    assert time.monotonic() - started < 10


def test_a_program_that_ignores_terminate_is_killed(tmp_path: Path) -> None:
    script = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    cancel = threading.Event()
    cancel.set()
    started = time.monotonic()
    with pytest.raises(Cancelled):
        run_cancellable([PY, "-c", script], cancel, poll=0.05)
    assert time.monotonic() - started < 20


def test_a_line_callback_that_fails_does_not_hang_the_run() -> None:
    def boom(line: str) -> None:
        raise ValueError(line)

    # More than a pipe holds: a reader that stopped would leave the program stuck.
    many = "for i in range(200000): print('line', i)"
    code, output = run_cancellable([PY, "-c", many], threading.Event(), boom)
    assert code == 0 and output.endswith("line 199999")


def test_the_key_of_a_file_changes_with_its_content_or_time(tmp_path: Path) -> None:
    film = tmp_path / "f.mkv"
    film.write_bytes(b"one")
    first = files.source_key(film)
    assert files.source_key(str(film)) == first and len(first) == 32
    film.write_bytes(b"two")  # the same size: the change time tells it apart
    st = os.stat(film)
    os.utime(film, ns=(st.st_atime_ns, st.st_mtime_ns + 1))
    assert files.source_key(film) != first
    odd = tmp_path / os.fsdecode(b"\xff.mkv")
    odd.write_bytes(b"x")
    assert len(files.source_key(odd)) == 32  # a name that is not UTF-8
    with pytest.raises(OSError):
        files.source_key(tmp_path / "gone.mkv")


@pytest.mark.parametrize(
    ("path", "folder", "inside"),
    [
        ("/m/show/e1.mkv", "/m/show", True),
        ("/m/show", "/m/show", True),
        ("/m/show/e1.mkv", "/m/show/", True),
        ("/m/show2/e1.mkv", "/m/show", False),
        ("/m/showtime.mkv", "/m/show", False),
        ("/m/a.mkv", "/", True),
        ("/m", "/m/show", False),
    ],
)
def test_under(path: str, folder: str, inside: bool) -> None:
    assert files.under(path, folder) is inside


def test_dir_size_counts_links_as_themselves(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x").write_bytes(b"12345")
    big = tmp_path / "big"
    big.write_bytes(b"x" * 10000)
    (tmp_path / "a" / "link").symlink_to(big)
    assert files.dir_size(tmp_path / "a") < 1000
    assert files.dir_size(tmp_path / "missing") == 0


def test_table_lines_up_every_column_but_the_last() -> None:
    lines = text.table([["A", "BB", "last"], ["ccc", "d", "x"]], indent="  ")
    assert lines == ["  A    BB  last", "  ccc  d   x"]
    assert text.table([]) == []
