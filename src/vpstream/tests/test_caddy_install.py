"""`vpstream install caddy`: Caddy's own apt repository, checked before root writes it."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from vpstream.platform.linux import caddy_install as ci
from vpstream.platform.linux.system_install import Cancelled, SetupError
from vpstream.tests.fakes import FakeRunner
from vpstream.tools import Result

KEY = (
    "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nmQINBGSN\n=abcd\n-----END PGP PUBLIC KEY BLOCK-----\n"
)
# What Caddy's repository served on 2026-11-15.
SOURCES = """\
# Source: Caddy
# Site: https://github.com/caddyserver/caddy
# Repository: Caddy / stable
# Description: Fast, multi-platform web server with automatic HTTPS


deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main

deb-src [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main
"""  # noqa: E501
REAL_KEYRING = ci.Layout().keyring


class Host(FakeRunner):
    """apt, gpg, systemctl and caddy as far as the install uses them. apt-get install puts
    in the new Caddy."""

    def __init__(self, version: str = "2.6.2\n", enabled: bool = False, active: bool = False):
        super().__init__(("apt-get", "gpg", "systemctl", "sudo", "caddy"))
        self.version, self.new = version, "v2.11.9 h1:x=\n"
        self.enabled, self.active = enabled, active

    def run(self, args: Sequence[str], *, timeout: float | None = None) -> Result:
        cmd = list(args)
        self.calls.append(cmd)
        core = cmd[1:] if cmd[0] == "sudo" else cmd
        if core[:1] == ["/usr/bin/caddy"]:
            return Result(0, self.version, "")
        if core[:1] == ["gpg"] and "--dearmor" in core:
            Path(core[core.index("-o") + 1]).write_bytes(b"binary key")
            return Result(0, "", "")
        if core[:1] == ["gpg"]:
            return Result(0, "pub:-:4096:1:65760C51EDEA2017:\nfpr:::::::::155B6D79CA56EA34:\n", "")
        if core[:2] == ["systemctl", "is-enabled"]:
            return Result(0, "enabled\n" if self.enabled else "disabled\n", "")
        if core[:2] == ["systemctl", "is-active"]:
            return Result(0 if self.active else 3, "", "")
        if (
            "install" in core
            and "caddy" in core
            and core[:2] == ["env", "DEBIAN_FRONTEND=noninteractive"]
        ):
            self.version = self.new
            self.enabled = self.active = True  # what Caddy's package does
        if core[:2] == ["systemctl", "disable"]:
            self.enabled = False
        if core[:2] == ["systemctl", "stop"]:
            self.active = False
        return Result(0, "", "")


class Web:
    def __init__(self, key: str = KEY, sources: str = SOURCES) -> None:
        self.pages = {ci.KEY_URL: key, ci.SOURCES_URL: sources}

    def get_text(self, url: str) -> str:
        return self.pages[url]

    def get_json(self, url: str) -> Any:
        raise AssertionError("not used")

    def download(self, url: str, dest: Path, progress: Any) -> str:
        raise AssertionError("not used")


def _setup(tmp_path: Path, host: Host, web: Web | None = None, yes: bool = True) -> ci.Setup:
    asked: list[str] = []
    setup = ci.Setup(
        runner=host,
        http=web or Web(),
        confirm=lambda q: asked.append(q) or yes,
        progress=lambda m: None,
        # The real keyring's name, which the sources name; root's steps are only recorded.
        layout=ci.Layout(REAL_KEYRING, tmp_path / "caddy-stable.list", _os(tmp_path, "ubuntu")),
        is_root=False,
    )
    setup.asked = asked  # type: ignore[attr-defined]
    return setup


def _os(tmp_path: Path, ident: str, like: str = "", pretty: str = "") -> Path:
    path = tmp_path / f"os-release-{ident}"
    path.write_text(f'ID={ident}\nID_LIKE="{like}"\nPRETTY_NAME="{pretty or ident}"\n')
    return path


def _installs(host: Host) -> list[list[str]]:
    return [c for c in host.calls if c[:2] == ["sudo", "install"]]


def test_the_install_adds_the_repository_then_installs_and_leaves_the_service_off(
    tmp_path: Path,
) -> None:
    host = Host()
    setup = _setup(tmp_path, host)
    said = setup.run(update=False)
    assert said == "Caddy 2.11.9 is installed from Caddy's repository (was 2.6.2)"
    assert "dl.cloudsmith.io/public/caddy/stable" in setup.asked[0]  # type: ignore[attr-defined]
    assert "in place of Caddy 2.6.2" in setup.asked[0]  # type: ignore[attr-defined]
    keyring, sources = _installs(host)
    assert keyring[-1] == str(REAL_KEYRING) and "0644" in keyring
    assert sources[-1] == str(tmp_path / "caddy-stable.list")
    # gpg ran as this user; root only put the files in place.
    assert all(c[0] == "gpg" for c in host.calls if "gpg" in c[:2])
    apt = [c for c in host.calls if "apt-get" in c]
    assert [c[c.index("apt-get") + 1 :] for c in apt] == [
        ["update"],
        [*ci.KEEP_CONFIG, "install", "-y", "caddy"],
    ]
    assert apt[0][0] == "sudo"
    assert not host.enabled and not host.active, "the package's web server holds port 80"


def test_a_running_caddy_service_is_left_running(tmp_path: Path) -> None:
    host = Host(enabled=True, active=True)
    _setup(tmp_path, host).run(update=False)
    assert host.enabled and host.active
    assert not [
        c for c in host.calls if c[1:3] in (["systemctl", "disable"], ["systemctl", "stop"])
    ]


def test_nothing_changes_when_the_question_is_answered_no(tmp_path: Path) -> None:
    host = Host()
    with pytest.raises(Cancelled):
        _setup(tmp_path, host, yes=False).run(update=False)
    assert [c for c in host.calls if c[0] == "sudo"] == []


@pytest.mark.parametrize(
    "sources",
    [
        SOURCES
        + "deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] https://evil.example/deb/debian any main\n",  # noqa: E501
        SOURCES + "deb https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main\n",
        SOURCES.replace("caddy-stable-archive-keyring", "other-keyring"),
        SOURCES.replace("/public/caddy/stable/", "/public/caddy/testing/"),
        "# nothing\n",
        "<html>rate limited</html>",
    ],
)
def test_sources_naming_anything_but_caddy_s_repository_are_refused(
    tmp_path: Path, sources: str
) -> None:
    host = Host()
    setup = _setup(tmp_path, host, Web(sources=sources))
    with pytest.raises(SetupError, match="source"):
        setup.run(update=False)
    assert [c for c in host.calls if c[0] == "sudo"] == [], "refused before root wrote anything"


def test_the_real_sources_are_kept_as_served() -> None:
    kept = ci.check_sources(SOURCES, REAL_KEYRING)
    assert kept.count("deb ") == 1 and kept.count("deb-src ") == 1


@pytest.mark.parametrize(
    "key",
    [
        "",
        "<html>",
        KEY.replace("PUBLIC", "PRIVATE"),
        KEY + KEY,
        "junk\n" + KEY,
    ],
)
def test_anything_but_one_public_key_is_refused(tmp_path: Path, key: str) -> None:
    host = Host()
    with pytest.raises(SetupError, match="not one OpenPGP public key"):
        _setup(tmp_path, host, Web(key=key)).run(update=False)
    assert [c for c in host.calls if c[0] == "sudo"] == []


def test_once_installed_install_says_so_and_update_upgrades(tmp_path: Path) -> None:
    host = Host()
    setup = _setup(tmp_path, host)
    setup.layout.sources.write_text(SOURCES)  # what the first install left
    host.version = "v2.11.9 h1:x=\n"
    assert "For a newer one: vpstream update caddy" in setup.run(update=False)
    assert [c for c in host.calls if c[0] == "sudo"] == []
    host.new = "v2.12.0 h1:y=\n"
    assert setup.run(update=True) == (
        "Caddy 2.12.0 is installed from Caddy's repository (was 2.11.9)"
    )
    assert _installs(host) == [], "the repository is there: not added again"
    host.calls.clear()
    assert "already the newest" in setup.run(update=True)


def test_a_repository_whose_install_broke_is_installed_from_again(tmp_path: Path) -> None:
    host = Host()  # still 2.6.2: apt failed after the repository was added
    setup = _setup(tmp_path, host)
    setup.layout.sources.write_text(SOURCES)
    assert "2.11.9 is installed" in setup.run(update=False)


def test_update_replaces_an_older_caddy_too(tmp_path: Path) -> None:
    """Ubuntu's 2.6.2, installed before vpstream: update does what install does."""
    host = Host()
    setup = _setup(tmp_path, host)
    assert setup.run(update=True) == "Caddy 2.11.9 is installed from Caddy's repository (was 2.6.2)"
    assert len(_installs(host)) == 2, "the repository was added"


def test_an_older_caddy_first_on_path_is_named(tmp_path: Path) -> None:
    class Hidden(Host):
        def which(self, name: str) -> str | None:
            return "/usr/local/bin/caddy" if name == "caddy" else super().which(name)

    with pytest.raises(SetupError, match=r"/usr/local/bin/caddy comes before it on PATH"):
        _setup(tmp_path, Hidden()).run(update=False)


@pytest.mark.parametrize(
    ("ident", "like", "ok"),
    [
        ("ubuntu", "debian", True),
        ("debian", "", True),
        ("raspbian", "debian", True),
        ("linuxmint", "ubuntu debian", True),
        ("fedora", "", False),
        ("arch", "", False),
        ("alpine", "", False),
    ],
)
def test_only_debian_ubuntu_and_raspbian(tmp_path: Path, ident: str, like: str, ok: bool) -> None:
    host = Host()
    setup = _setup(tmp_path, host)
    setup.layout = ci.Layout(
        REAL_KEYRING, tmp_path / "caddy-stable.list", _os(tmp_path, ident, like, f"{ident} 1")
    )
    if ok:
        setup.run(update=False)
        return
    with pytest.raises(SetupError) as refused:
        setup.run(update=False)
    assert str(refused.value) == (
        "this install works on Debian, Ubuntu and Raspbian, your OS seems different "
        f"({ident} 1): please install Caddy 2.10 or newer for your OS "
        "(see https://caddyserver.com/docs/install)"
    )
    assert host.calls == [], "nothing is run on another system"


def test_without_os_release_nothing_is_done(tmp_path: Path) -> None:
    assert "(unknown)" in str(ci.distro_problem(tmp_path / "none"))


def test_a_caddy_still_too_old_afterwards_is_a_failure(tmp_path: Path) -> None:
    host = Host()
    host.new = "v2.9.1 h1:x=\n"
    with pytest.raises(SetupError, match=r"2\.10 or newer"):
        _setup(tmp_path, host).run(update=False)


def test_without_apt_or_gpg_nothing_is_done(tmp_path: Path) -> None:
    host = Host()
    host.available.discard("gpg")
    with pytest.raises(SetupError, match="sudo apt install gpg"):
        _setup(tmp_path, host).run(update=False)
    host.available.discard("apt-get")
    with pytest.raises(SetupError, match="apt, which is not found"):
        _setup(tmp_path, host).run(update=False)


def test_the_cli_takes_caddy(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    from vpstream import cli

    seen: list[tuple[type, bool]] = []

    def run(self: Any, *, update: bool) -> str:
        seen.append((type(self), update))
        return "done"

    monkeypatch.setattr(ci.Setup, "run", run)
    assert cli.main(["install", "caddy", "--yes"]) == 0
    assert cli.main(["update", "caddy"]) == 0
    assert seen == [(ci.Setup, False), (ci.Setup, True)]
    assert capsys.readouterr().out == "done\ndone\n"
