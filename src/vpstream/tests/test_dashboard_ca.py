from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from vpstream import config, dashboard_ca
from vpstream.tools import SystemRunner


def test_the_constraints_permit_exactly_the_hosts() -> None:
    assert dashboard_ca.constraints(["203.0.113.5", "2001:db8::5"]) == (
        "critical, permitted;IP:203.0.113.5/255.255.255.255, "
        "permitted;IP:2001:db8::5/ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff, "
        "permitted;DNS:vpstream.invalid"  # without it, any site name would pass
    )
    assert dashboard_ca.constraints(["vps.example.org"]) == (
        "critical, permitted;DNS:vps.example.org, permitted;IP:0.0.0.0/255.255.255.255"
    )


@pytest.mark.parametrize("host", ["a,b", "x y", "-x", "a\nb", "é.example"])
def test_a_name_that_would_change_the_constraints_is_refused(host: str) -> None:
    with pytest.raises(dashboard_ca.AuthorityError):
        dashboard_ca.constraints([host])


def _leaf(tmp: Path, paths: config.Paths, san: str) -> Path:
    """A certificate for `san`, signed with the authority's key: what someone who took the
    key could make."""
    key, csr, ext, out = (tmp / f"leaf.{x}" for x in ("key", "csr", "cnf", "crt"))
    ext.write_text(f"subjectAltName={san}\nextendedKeyUsage=serverAuth\n")
    for args in (
        ["req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-nodes", "-keyout", str(key), "-subj", "/CN=leaf", "-out", str(csr)],
        ["x509", "-req", "-in", str(csr), "-CA", str(paths.dashboard_ca_cert),
         "-CAkey", str(paths.dashboard_ca_key), "-CAcreateserial", "-days", "1",
         "-extfile", str(ext), "-out", str(out)],
    ):  # fmt: skip
        subprocess.run(["openssl", *args], check=True, capture_output=True, cwd=tmp)
    return out


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl is not installed")
def test_the_authority_cannot_vouch_for_another_site(tmp_path: Path) -> None:
    paths = config.Paths.under(tmp_path)
    paths.config_file.parent.mkdir(parents=True)
    dashboard_ca.make(SystemRunner(), paths, ["203.0.113.5"])
    assert dashboard_ca.covered(paths) == ["203.0.113.5"]
    assert paths.dashboard_ca_key.stat().st_mode & 0o077 == 0  # no group given in a test

    def verifies(san: str) -> bool:
        leaf = _leaf(tmp_path, paths, san)
        res = subprocess.run(
            ["openssl", "verify", "-CAfile", str(paths.dashboard_ca_cert), str(leaf)],
            capture_output=True, text=True,
        )  # fmt: skip
        return res.returncode == 0

    assert verifies("IP:203.0.113.5")  # the dashboard's own address
    assert not verifies("DNS:bank.example")
    assert not verifies("IP:198.51.100.7")
    assert not verifies("IP:2001:db8::1")
