"""The public dashboard's certificate authority, made by `init` for this host alone.

The owner installs this authority on their phone, and a phone trusts what an authority it
was given signs, for any site. Caddy's own authority (`tls internal`) carries no limit, and
its key lives on this host, which is on the internet. So vpstream makes its own, with name
constraints that permit only this host's addresses (and its name, when the configuration
gives one): whatever it signs for any other site is refused, even by someone who took the
key. Caddy then signs the dashboard's certificates with it (see `caddy.dashboard_config`).

The files are root's, next to the configuration: the certificate for anyone, the key for
root and the service's group (Caddy, run by the service, signs with it). Beside them is the
list of hosts the constraints permit, so `init` knows when the host has new addresses and
needs a new authority, and the daemon serves only addresses the phone will accept.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from collections.abc import Callable, Sequence
from pathlib import Path

from vpstream.config import Paths
from vpstream.files import read_json_object, remove_path, write_atomic
from vpstream.net import normal_ip
from vpstream.tools import Runner, tail

TOOL = "openssl"
NAME = "vpstream dashboard authority"
DAYS = 3650
# A permitted subtree of each kind is what limits that kind: without a name in the list, any
# site name would pass. Nothing can be in these two, so no other name or address does.
NO_NAME = "vpstream.invalid"  # .invalid is reserved and never resolves
NO_ADDRESS = "0.0.0.0/255.255.255.255"
_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
TIMEOUT = 60.0


class AuthorityError(RuntimeError):
    pass


def constraints(hosts: Sequence[str]) -> str:
    """The nameConstraints extension, as openssl's configuration writes it, that permits
    exactly these addresses and names."""
    permitted = []
    for host in hosts:
        normal = normal_ip(host)
        if normal is None:
            if not _NAME.fullmatch(host):
                raise AuthorityError(f"not a usable host name for the authority: {host!r}")
            permitted.append(f"DNS:{host}")
        elif isinstance(ipaddress.ip_address(normal), ipaddress.IPv4Address):
            permitted.append(f"IP:{normal}/255.255.255.255")
        else:
            permitted.append(f"IP:{normal}/{':'.join(['ffff'] * 8)}")
    if not any(p.startswith("DNS:") for p in permitted):
        permitted.append(f"DNS:{NO_NAME}")
    if not any(p.startswith("IP:") for p in permitted):
        permitted.append(f"IP:{NO_ADDRESS}")
    return "critical, " + ", ".join(f"permitted;{p}" for p in permitted)


def openssl_config(hosts: Sequence[str]) -> str:
    return f"""\
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no

[dn]
CN = {NAME}

[ext]
basicConstraints = critical, CA:TRUE, pathlen:1
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
nameConstraints = {constraints(hosts)}
"""


def covered(paths: Paths) -> list[str] | None:
    """The hosts the authority was made for, or None when there is no usable one."""
    if not (paths.dashboard_ca_cert.is_file() and paths.dashboard_ca_key.is_file()):
        return None
    hosts = (read_json_object(paths.dashboard_ca_hosts) or {}).get("hosts")
    if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
        return None
    return hosts


def make(
    runner: Runner,
    paths: Paths,
    hosts: Sequence[str],
    give_key: Callable[[Path], None] = lambda key: None,
) -> None:
    """Make a new authority for `hosts`, in place of any before it. `give_key` gets the
    finished key file, to hand it to the service's group, before it takes its place. A make
    that is cut off leaves no list of hosts, so the next `init` makes it again."""
    if not hosts:
        raise AuthorityError("no address to make the dashboard's authority for")
    work = paths.dashboard_ca_cert.with_name("dashboard-ca.new")
    remove_path(work)
    work.mkdir(mode=0o700)
    try:
        conf, key, cert = work / "openssl.cnf", work / "ca.key", work / "ca.crt"
        conf.write_text(openssl_config(hosts), encoding="ascii")
        res = runner.run(
            [
                TOOL, "req", "-x509", "-new", "-config", str(conf),
                "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                "-keyout", str(key), "-out", str(cert), "-days", str(DAYS),
            ],
            timeout=TIMEOUT,
        )  # fmt: skip
        if res.returncode != 0 or not (key.is_file() and cert.is_file()):
            raise AuthorityError(f"openssl could not make the authority: {tail(res)}")
        paths.dashboard_ca_hosts.unlink(missing_ok=True)
        key.chmod(0o600)
        give_key(key)
        cert.chmod(0o644)
        os.replace(key, paths.dashboard_ca_key)
        os.replace(cert, paths.dashboard_ca_cert)
        write_atomic(paths.dashboard_ca_hosts, json.dumps({"hosts": list(hosts)}) + "\n")
        paths.dashboard_ca_hosts.chmod(0o644)
    finally:
        remove_path(work)


def remove(paths: Paths) -> list[Path]:
    """Remove the authority's files; returns those that were there."""
    gone = []
    for path in (paths.dashboard_ca_hosts, paths.dashboard_ca_key, paths.dashboard_ca_cert):
        if path.exists() or path.is_symlink():
            path.unlink()
            gone.append(path)
    return gone
