from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from vpstream import cli, client, listing
from vpstream.files import source_key
from vpstream.tests.fakes import FakeRunner
from vpstream.tests.test_probe import SAMPLE


@pytest.fixture
def media(tmp_path: Path) -> Path:
    root = tmp_path / "media"
    (root / "show").mkdir(parents=True)
    (root / "empty").mkdir()
    (root / ".hidden").mkdir()
    names = ["film 10.mkv", "film 2.mkv", "notes.txt", ".part.mkv"]
    for name in [*names, "show/e1.mkv", "show/e2.mkv", ".hidden/x.mkv"]:
        (root / name).write_bytes(name.encode())
    return root


def runner() -> FakeRunner:
    r = FakeRunner()
    r.on("ffprobe", stdout=json.dumps(SAMPLE))
    return r


def row(path: Path, **kw: Any) -> dict[str, Any]:
    return {"key": source_key(str(path)), "base": "copy", "soft": [1], "burn": [], **kw}


def test_videos_then_subfolders_with_what_is_prepared(media: Path) -> None:
    cache = {r["key"]: r for r in (row(media / "film 2.mkv", burn=[3]), row(media / "show/e2.mkv"))}
    rows = listing.listing(runner(), media, cache)
    assert [r.name for r in rows] == ["film 2.mkv", "film 10.mkv", "show/"]  # natural order
    film = rows[0]
    assert film.duration == 7380.5 and film.audio == ["eng", "jpn"]
    assert film.subs[0] == "1 eng soft" and film.subs[2] == "3 jpn burned in"
    assert film.prepared == "base (copy), soft 1, burned in 3"
    assert rows[1].prepared == "not prepared"
    assert (rows[2].videos, rows[2].ready) == (2, 1)
    text = listing.format_listing(rows, asked=True)
    assert "film 2.mkv  2h 03m\n  audio: eng, jpn\n" in text
    assert "  browser mode: base (copy), soft 1, burned in 3" in text
    assert "show/  2 videos, 1 prepared" in text and "empty" not in text and "hidden" not in text


def test_a_changed_file_is_not_prepared_any_more(media: Path) -> None:
    film = media / "film 2.mkv"
    cache = {r["key"]: r for r in [row(film)]}
    st = os.stat(film)
    os.utime(film, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    assert listing.listing(runner(), media, cache)[0].prepared == "not prepared"


def test_a_link_is_matched_by_the_file_it_names(media: Path, tmp_path: Path) -> None:
    other = tmp_path / "linked"
    other.mkdir()
    (other / "film.mkv").symlink_to(media / "film 2.mkv")
    cache = {r["key"]: r for r in [row(media / "film 2.mkv")]}
    assert listing.listing(runner(), other, cache)[0].prepared.startswith("base")


def test_without_the_daemon_the_tracks_are_still_listed(media: Path) -> None:
    rows = listing.listing(runner(), media, None)
    text = listing.format_listing(rows, asked=False)
    assert "browser mode" not in text and "show/  2 videos\n" in text
    assert text.endswith("(the daemon does not answer, so what is prepared is not shown)")


def test_a_file_ffprobe_cannot_read_and_a_folder_that_cannot_be_entered(media: Path) -> None:
    r = FakeRunner()
    r.on("ffprobe", returncode=1, stderr="Invalid data found")
    if os.geteuid() != 0:
        (media / "show").chmod(0)
    try:
        rows = listing.listing(r, media, {})
    finally:
        (media / "show").chmod(0o755)
    text = listing.format_listing(rows, asked=True)
    assert "film 2.mkv\n  cannot be read: ffprobe failed" in text
    if os.geteuid() != 0:
        assert "show/  cannot be read: Permission denied" in text


def test_an_empty_folder(tmp_path: Path) -> None:
    assert listing.format_listing(listing.listing(runner(), tmp_path, {}), asked=True) == (
        "No videos here."
    )


# --- the command


def daemon(rows: list[dict[str, Any]] | None) -> Any:
    def call(socket: Path, method: str, route: str, payload: Any = None) -> Any:
        if rows is None:
            raise client.DaemonUnavailable("no daemon")
        assert route == "prepared"
        return {"prepared": rows}

    return call


def test_ls_lists_the_configured_folder_by_default(
    media: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from vpstream import config, tools

    monkeypatch.setenv(config.ROOT_ENV, str(tmp_path / "root"))
    cfg = config.default_paths().config_file
    cfg.parent.mkdir(parents=True)
    cfg.write_text(f'[media]\nfolder = "{media}"\n')
    monkeypatch.setattr(tools, "SystemRunner", runner)
    monkeypatch.setattr(client, "call", daemon([row(media / "film 2.mkv")]))
    assert cli.main(["ls"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("film 2.mkv  2h 03m") and "browser mode: base (copy), soft 1" in out


def test_ls_of_what_is_not_a_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(client, "call", daemon(None))
    assert cli.main(["ls", str(tmp_path / "nothing")]) == 2
    assert "not a folder" in capsys.readouterr().err
