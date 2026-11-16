"""Install or update Caddy from Caddy's own apt repository, as caddyserver.com/docs/install
says for Debian and Ubuntu.

Ubuntu 24.04 ships Caddy 2.6.2, which cannot ask for a certificate for an address: that needs
ACME profiles, from Caddy 2.10. HTTPS for streams needs this one; plain HTTP works with
either. The package replaces Ubuntu's in place (the same name, a newer version), and later
upgrades come with apt's, or with `vpstream update caddy`.

What is fetched is checked before root writes it: the key must be one OpenPGP public key,
and the list of sources may name nothing but Caddy's repository, signed by that key. The
package's own web server (`caddy.service`, which listens on port 80) is left as it was
before: a stopped one is stopped again, so it never holds the port Let's Encrypt checks.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from vpstream import caddy
from vpstream.system_install import APT_GET, Cancelled, Installer, SetupError
from vpstream.tools import tail

REPOSITORY = "https://dl.cloudsmith.io/public/caddy/stable"
KEY_URL = f"{REPOSITORY}/gpg.key"
SOURCES_URL = f"{REPOSITORY}/debian.deb.txt"
DOCS = "https://caddyserver.com/docs/install#debian-ubuntu-raspbian"
SERVICE = "caddy"
# A configuration file the user changed (/etc/caddy/Caddyfile) is kept, without a question.
KEEP_CONFIG = ("-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold")


DISTROS = ("debian", "ubuntu", "raspbian")  # what Caddy's commands are written for
PACKAGED = Path("/usr/bin/caddy")  # where the package puts the program


@dataclass(frozen=True)
class Layout:
    keyring: Path = Path("/usr/share/keyrings/caddy-stable-archive-keyring.gpg")
    sources: Path = Path("/etc/apt/sources.list.d/caddy-stable.list")
    os_release: Path = Path("/etc/os-release")


def distro_problem(os_release: Path) -> str | None:
    """Why Caddy's commands for Debian, Ubuntu and Raspbian are not for this system, or
    None when they are: `ID` or `ID_LIKE` in os-release names one of them."""
    try:
        text = os_release.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        text = ""
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip().strip("\"'")
    names = {fields.get("ID", "").lower(), *fields.get("ID_LIKE", "").lower().split()}
    if names & set(DISTROS):
        return None
    name = fields.get("PRETTY_NAME") or fields.get("ID") or "unknown"
    return (
        "this install works on Debian, Ubuntu and Raspbian, your OS seems different "
        f"({name}): please install Caddy 2.10 or newer for your OS (see "
        "https://caddyserver.com/docs/install)"
    )


_ARMOR = re.compile(
    r"-----BEGIN PGP PUBLIC KEY BLOCK-----\n.+?\n-----END PGP PUBLIC KEY BLOCK-----\n?", re.S
)


def check_key(text: str) -> None:
    """Refuse anything but one ASCII-armored OpenPGP public key."""
    if text.count("-----BEGIN") != 1 or not _ARMOR.fullmatch(text.strip() + "\n"):
        raise SetupError(f"{KEY_URL} is not one OpenPGP public key; see {DOCS}")


def check_sources(text: str, keyring: Path) -> str:
    """The list of sources as apt gets it, when it names only Caddy's repository, signed by
    `keyring`; raises SetupError otherwise."""
    line = re.compile(
        rf"(deb|deb-src) \[signed-by={re.escape(str(keyring))}\] "
        rf"{re.escape(REPOSITORY)}/deb/[a-z]+ [A-Za-z0-9.-]+( [a-z]+)+"
    )
    kept = []
    for raw in text.splitlines():
        entry = raw.strip()
        if entry and not entry.startswith("#") and not line.fullmatch(entry):
            raise SetupError(f"{SOURCES_URL} names a source other than Caddy's: {entry!r}")
        kept.append(entry)
    if not any(e.startswith("deb ") for e in kept):
        raise SetupError(f"{SOURCES_URL} names no package source; see {DOCS}")
    return "\n".join(kept).strip() + "\n"


def _shown(version: tuple[int, ...] | None) -> str:
    return "unknown version" if version is None else ".".join(map(str, version))


@dataclass
class Setup(Installer):
    layout: Layout = Layout()

    def run(self, *, update: bool) -> str:
        """Install (or update) Caddy: an older one, from Ubuntu's own packages for example, is
        replaced by the newest from Caddy's repository. Returns what was done; raises
        Cancelled or SetupError."""
        if problem := distro_problem(self.layout.os_release):
            raise SetupError(problem)
        if not self.runner.which("apt-get"):
            raise SetupError("vpstream installs Caddy with apt, which is not found here")
        before = caddy.caddy_version(self.runner)
        ours = self.layout.sources.is_file()
        if ours and not update and before is not None and not caddy.profiles_problem(self.runner):
            return (
                f"Caddy {_shown(before)} is installed from Caddy's repository. "
                "For a newer one: vpstream update caddy"
            )
        if not ours:
            instead = f", in place of Caddy {_shown(before)}" if before else ""
            question = (
                f"This adds Caddy's own package repository ({REPOSITORY}) to apt, as {DOCS} "
                f"says, and installs the newest Caddy from it{instead}. Do you want to proceed?"
            )
            if not self.confirm(question):
                raise Cancelled
            self._add_repository()
        state = self._service_state()
        self.progress("apt-get update")
        self._root(*APT_GET, "update", what="updating apt's package lists")
        self.progress("installing caddy")
        self._root(*APT_GET, *KEEP_CONFIG, "install", "-y", "caddy", what="installing caddy")
        self._leave_service(state)
        found = self.runner.which("caddy")
        if found and Path(found) != PACKAGED:
            # An older Caddy put in by hand comes first on PATH, and is what vpstream runs.
            raise SetupError(
                f"the new Caddy is {PACKAGED}, but {found} comes before it on PATH: remove "
                f"{found} (it is not from a package), then check with: caddy version"
            )
        after = caddy.caddy_version(self.runner)
        if problem := caddy.profiles_problem(self.runner):
            raise SetupError(f"apt installed it, but: {problem}")
        if after == before:
            return f"Caddy {_shown(after)} is already the newest from Caddy's repository"
        was = f" (was {_shown(before)})" if before else ""
        return f"Caddy {_shown(after)} is installed from Caddy's repository{was}"

    def _add_repository(self) -> None:
        """Fetch and check the key and the sources as this user; root only puts them in place."""
        if not self.runner.which("gpg"):
            raise SetupError("gpg is needed to read Caddy's key: sudo apt install gpg")
        self.progress(f"fetching Caddy's signing key and sources ({REPOSITORY})")
        key = self.http.get_text(KEY_URL)
        check_key(key)
        sources = check_sources(self.http.get_text(SOURCES_URL), self.layout.keyring)
        with tempfile.TemporaryDirectory(prefix="vpstream-caddy-", dir=self.tmp_dir) as tmp:
            armored, binary, listed = Path(tmp, "key.asc"), Path(tmp, "key.gpg"), Path(tmp, "list")
            armored.write_text(key, encoding="ascii")
            listed.write_text(sources, encoding="utf-8")
            res = self.runner.run(
                ["gpg", "--batch", "--yes", "--dearmor", "-o", str(binary), str(armored)],
                timeout=60,
            )
            if res.returncode != 0 or not binary.is_file():
                raise SetupError(f"gpg could not read Caddy's key: {tail(res)}")
            shown = self.runner.run(
                ["gpg", "--batch", "--show-keys", "--with-colons", str(armored)], timeout=60
            )
            fingerprints = [
                line.split(":")[9] for line in shown.stdout.splitlines() if line.startswith("fpr:")
            ]
            if fingerprints:
                self.progress(f"Caddy's signing key: {fingerprints[0]}")
            for src, dest in ((binary, self.layout.keyring), (listed, self.layout.sources)):
                self._root(
                    "install", "-D", "-m", "0644", "-o", "root", "-g", "root", src, dest,
                    what=f"writing {dest}",
                )  # fmt: skip
        self.progress(f"{self.layout.sources}: Caddy's repository added")

    def _service_state(self) -> tuple[bool, bool] | None:
        """Whether the package's own web server is enabled and running, before apt runs."""
        if not self.runner.which("systemctl"):
            return None
        enabled = self.runner.run(["systemctl", "is-enabled", SERVICE], timeout=30)
        active = self.runner.run(["systemctl", "is-active", "--quiet", SERVICE], timeout=30)
        return enabled.stdout.strip() == "enabled", active.returncode == 0

    def _leave_service(self, state: tuple[bool, bool] | None) -> None:
        """Put the package's web server back as it was: a new package may enable and start
        it, and it listens on port 80."""
        if state is None:
            return
        enabled, active = state
        if not enabled:
            self._root("systemctl", "disable", SERVICE, what="leaving caddy.service disabled")
        if not active:
            self._root("systemctl", "stop", SERVICE, what="leaving caddy.service stopped")
        if not (enabled or active):
            self.progress("caddy.service: left disabled and stopped (vpstream runs its own)")
