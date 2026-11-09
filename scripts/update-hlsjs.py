#!/usr/bin/env python3
"""Update the bundled hls.js from the npm registry.

  python scripts/update-hlsjs.py              update to the latest release
  python scripts/update-hlsjs.py --version X  update to (or reinstall) version X
  python scripts/update-hlsjs.py --check      only report; exit 1 when an update exists

The tarball is checked against the sha512 integrity that npm publishes, and only three
named files are read from it: dist/hls.min.js, LICENSE and package.json. When run in GitHub
Actions, `version` and `changed` are written to $GITHUB_OUTPUT.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import urllib.request
from pathlib import Path
from typing import Any

REGISTRY = "https://registry.npmjs.org/hls.js"
ASSETS = Path(__file__).resolve().parent.parent / "src" / "vpstream" / "assets"
VERSION_FILE = ASSETS / "hls.js.version"
# Published file name -> file inside the npm package.
FILES = {"hls.min.js": "package/dist/hls.min.js", "hls.js.LICENSE": "package/LICENSE"}
USER_AGENT = "vpstream-update-hlsjs"
_VERSION = re.compile(r"\d+\.\d+\.\d+")


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data: bytes = resp.read()
    return data


def release(version: str) -> dict[str, Any]:
    """Registry metadata for one version, or for "latest"."""
    if version != "latest" and not _VERSION.fullmatch(version):
        raise SystemExit(f"not a release version: {version!r}")
    meta: dict[str, Any] = json.loads(fetch(f"{REGISTRY}/{version}"))
    if not _VERSION.fullmatch(str(meta.get("version", ""))):
        raise SystemExit(f"the registry returned no usable version for {version!r}")
    return meta


def verify(data: bytes, integrity: str) -> None:
    algo, _, expected = integrity.partition("-")
    if algo != "sha512":
        raise SystemExit(f"unexpected integrity type: {algo!r}")
    actual = base64.b64encode(hashlib.sha512(data).digest()).decode()
    if actual != expected:
        raise SystemExit("the tarball does not match the integrity published by npm")


def read_member(tar: tarfile.TarFile, name: str) -> bytes:
    member = tar.getmember(name)
    if not member.isfile():
        raise SystemExit(f"{name} in the tarball is not a regular file")
    f = tar.extractfile(member)
    if f is None:
        raise SystemExit(f"cannot read {name} from the tarball")
    return f.read()


def package_files(meta: dict[str, Any]) -> dict[str, bytes]:
    """Download and check the release, returning the files to bundle by published name."""
    data = fetch(meta["dist"]["tarball"])
    verify(data, meta["dist"]["integrity"])
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        pkg = json.loads(read_member(tar, "package/package.json"))
        if pkg.get("version") != meta["version"]:
            raise SystemExit("package.json in the tarball names a different version")
        files = {name: read_member(tar, inner) for name, inner in FILES.items()}
    if f'"{meta["version"]}"'.encode() not in files["hls.min.js"]:
        raise SystemExit("hls.min.js does not contain its own version string")
    return files


def bundled_version() -> str:
    try:
        return VERSION_FILE.read_text().strip()
    except OSError:
        return ""


def github_output(**values: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.writelines(f"{k}={v}\n" for k, v in values.items())


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--version", default="latest", help="hls.js version (default: latest)")
    p.add_argument("--check", action="store_true", help="only report whether an update exists")
    args = p.parse_args()

    meta = release(args.version)
    new, old = meta["version"], bundled_version()
    changed = new != old
    github_output(version=new, changed=str(changed).lower())
    if args.check:
        print(f"bundled {old or 'unknown'}, {args.version} is {new}")
        return 1 if changed else 0
    if not changed and args.version == "latest":
        print(f"hls.js {new} is already bundled")
        return 0
    for name, body in package_files(meta).items():
        (ASSETS / name).write_bytes(body)
    VERSION_FILE.write_text(new + "\n")
    print(f"hls.js {old or 'unknown'} -> {new}: wrote " + ", ".join([*FILES, VERSION_FILE.name]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
