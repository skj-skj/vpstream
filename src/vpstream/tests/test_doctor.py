from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from vpstream import cli, config, doctor, tailscale
from vpstream.tests.fakes import NAME, FakeRunner, node_status, serve_status

ALL_TOOLS = (*(t.name for t in doctor.TOOLS), "fc-list")
STATUS = node_status()
FAMILIES = "Noto Sans CJK JP\nLiberation Sans\nDejaVu Sans\n"


def _host(tmp_path: Path, available: tuple[str, ...] = ALL_TOOLS) -> tuple[FakeRunner, dict]:
    """A fake host where everything is fine."""
    runner = FakeRunner(available)
    runner.on("tailscale", "ip", "-4", stdout="100.64.0.7\n")
    runner.on("tailscale", "status", "--json", stdout=json.dumps(STATUS))
    runner.on("tailscale", "serve", "status", "--json", stdout="{}")
    runner.on("fc-list", stdout=FAMILIES)
    marker = tmp_path / "systemd"
    marker.mkdir()
    ufw_conf = tmp_path / "ufw.conf"
    ufw_conf.write_text("# comment\nENABLED=yes\nLOGLEVEL=low\n")
    package = tmp_path / "pkg"
    package.mkdir(mode=0o755)
    kwargs = {
        "python_version": (3, 12, 3),
        "package_dir": package,
        "paths": config.Paths.under(tmp_path),
        "systemd_marker": marker,
        "ufw_conf": ufw_conf,
        "unit_file": tmp_path / "vpstream.service",
    }
    return runner, kwargs


def _by_name(checks: list[doctor.Check]) -> dict[str, doctor.Check]:
    return {c.name: c for c in checks}


def test_every_missing_tool_is_reported(tmp_path: Path) -> None:
    runner, kwargs = _host(tmp_path, available=())
    checks = _by_name(doctor.run_checks(runner, **kwargs))
    for tool in doctor.TOOLS:
        assert checks[tool.name].status == ("problem" if tool.required else "warning")
        assert checks[tool.name].fix
    assert checks["ffprobe"].fix == "vpstream install ffmpeg"
    assert checks["mkvmerge"].fix == "sudo apt install mkvtoolnix"
    # Checks that need a missing tool are left out instead of failing a second time.
    assert not {"systemd", "ufw enabled", "tailscale up", "tailnet https", "dashboard"} & set(
        checks
    )
    assert doctor.exit_code(list(checks.values())) == 1


def test_a_missing_optional_tool_is_only_a_warning(tmp_path: Path) -> None:
    tools = tuple(t for t in ALL_TOOLS if not t.startswith("mkv"))
    runner, kwargs = _host(tmp_path, available=tools)
    checks = doctor.run_checks(runner, **kwargs)
    # The install check needs root-owned folders, which a test cannot make.
    checks = [c for c in checks if c.name != "install"]
    warned = {c.name for c in checks if c.status == "warning"}
    assert warned == {"mkvextract", "mkvmerge", "service"}
    assert doctor.exit_code(checks) == 0
    report = doctor.format_report(checks)
    assert "Everything vpstream needs is here. 3 warnings." in report
    assert "fix: sudo apt install mkvtoolnix" in report


def test_python_version() -> None:
    assert doctor.check_python((3, 12, 0)).status == "ok"
    assert doctor.check_python((3, 14, 8)).detail == "3.14.8"
    old = doctor.check_python((3, 11, 9))
    assert old.status == "problem" and "3.12 or newer" in old.detail


def test_systemd_must_be_running(tmp_path: Path) -> None:
    assert doctor.check_systemd(tmp_path).status == "ok"
    assert doctor.check_systemd(tmp_path / "nope").status == "problem"


@pytest.mark.parametrize(
    ("text", "enabled"),
    [
        ("ENABLED=yes\n", True),
        ('ENABLED="yes"\n', True),
        ("ENABLED=no\n", False),
        ("# ENABLED=yes\n", False),
        ("LOGLEVEL=low\n", False),
    ],
)
def test_ufw_enabled(text: str, enabled: bool) -> None:
    assert doctor.ufw_enabled(text) is enabled


def test_ufw_not_enabled_is_a_problem_vpstream_does_not_fix(tmp_path: Path) -> None:
    conf = tmp_path / "ufw.conf"
    conf.write_text("ENABLED=no\n")
    check = doctor.check_ufw_enabled(conf)
    assert check.status == "problem" and "never enables" in check.fix
    assert doctor.check_ufw_enabled(tmp_path / "nope").status == "warning"


def test_tailscale_address() -> None:
    runner = FakeRunner(("tailscale",))
    runner.on("tailscale", "ip", "-4", stdout="100.64.0.7\n")
    assert tailscale.ipv4(runner) == "100.64.0.7"
    assert doctor.check_tailscale(runner).detail == "100.64.0.7"


@pytest.mark.parametrize(
    ("returncode", "stdout", "message"),
    [
        (1, "Tailscale is stopped.\n", "Tailscale is stopped"),
        (0, "", "printed no address"),
        (0, "fd7a:115c:a1e0::1\n", "printed no address"),
    ],
)
def test_tailscale_down(returncode: int, stdout: str, message: str) -> None:
    runner = FakeRunner(("tailscale",))
    runner.on("tailscale", "ip", "-4", returncode=returncode, stdout=stdout)
    with pytest.raises(tailscale.TailscaleDown, match=message):
        tailscale.ipv4(runner)
    check = doctor.check_tailscale(runner)
    assert check.status == "problem" and check.fix == "sudo tailscale up"


def test_tailscale_not_installed() -> None:
    with pytest.raises(tailscale.TailscaleDown, match="not found"):
        tailscale.ipv4(FakeRunner(()))


def test_missing_fonts_are_warnings() -> None:
    runner = FakeRunner(("fc-list",))
    runner.on("fc-list", stdout="DejaVu Sans\nLiberation Sans\n")
    checks = _by_name(doctor.check_fonts(runner))
    assert checks["font Liberation Sans"].status == "ok"
    assert checks["font Noto Sans CJK"].status == "warning"
    assert checks["font Noto Sans CJK"].fix == "sudo apt install fonts-noto-cjk"
    assert doctor.check_fonts(FakeRunner(()))[0].fix == "sudo apt install fontconfig"


def test_config_check(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    assert "defaults" in doctor.check_config(path).detail
    path.write_text("[stream]\nport = 2000\n")
    assert "stream port 2000" in doctor.check_config(path).detail
    path.write_text("[stream]\nport = 1\n")
    assert doctor.check_config(path).status == "problem"


def test_each_folder_must_be_safe_for_the_service(tmp_path: Path) -> None:
    me = os.getuid()
    folder = tmp_path / "home"
    folder.mkdir()

    def problem(mode: int, owner: int = me) -> str | None:
        folder.chmod(mode)
        return doctor.folder_problem(folder, folder.stat(), owner)

    assert problem(0o755) is None
    assert problem(0o711) is None
    assert problem(0o750) == f"other users cannot enter {folder}"
    assert problem(0o757) == f"{folder} is writable by every user"
    assert problem(0o1777) is None  # sticky, like /tmp
    assert problem(0o755, owner=me + 1) == f"{folder} is not owned by root"
    if folder.stat().st_gid != 0:
        assert problem(0o775) == f"{folder} is writable by a group other than root"


def test_install_is_checked_up_to_the_root(tmp_path: Path) -> None:
    package = tmp_path / "closed" / "vpstream"
    package.mkdir(parents=True)
    package.chmod(0o755)
    (tmp_path / "closed").chmod(0o700)
    problem = doctor.install_problem(package, owner_uid=os.getuid())
    assert problem == f"other users cannot enter {tmp_path / 'closed'}"


def test_install_must_be_owned_by_root(tmp_path: Path) -> None:
    package = tmp_path / "vpstream"
    package.mkdir(mode=0o755)
    if os.getuid() == 0:
        pytest.skip("root owns the test folder")
    check = doctor.check_install(package)
    assert check.status == "problem"
    assert "PIPX_HOME=/opt/pipx" in check.fix
    assert doctor.install_problem(tmp_path / "nope") == f"cannot check {tmp_path / 'nope'}: " + (
        "No such file or directory"
    )


def test_report_and_exit_code() -> None:
    checks = [
        doctor.Check("python", "ok", "3.12.3"),
        doctor.Check("caddy", "problem", "not found", "sudo apt install caddy"),
        doctor.Check("mkvmerge", "warning", "not found", "sudo apt install mkvtoolnix"),
    ]
    lines = doctor.format_report(checks).splitlines()
    assert lines[0] == "ok       python    3.12.3"
    assert lines[1] == "PROBLEM  caddy     not found"
    assert lines[2].strip() == "fix: sudo apt install caddy"
    assert lines[-1] == "1 problem. 1 warning."
    assert doctor.exit_code(checks) == 1
    assert doctor.exit_code(checks[:1]) == 0


@pytest.mark.parametrize(("status", "code"), [("ok", 0), ("warning", 0), ("problem", 1)])
def test_cli_doctor_prints_the_report_and_exits_by_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], status: str, code: int
) -> None:
    check = doctor.Check("caddy", status, "detail here", "the fix")  # type: ignore[arg-type]
    monkeypatch.setattr(doctor, "run_checks", lambda runner: [check])
    assert cli.main(["doctor"]) == code
    assert "caddy  detail here" in capsys.readouterr().out


def test_service_check(tmp_path: Path) -> None:
    unit = tmp_path / "vpstream.service"
    runner = FakeRunner(("systemctl",))
    missing = doctor.check_service(runner, unit)
    assert missing.status == "warning" and missing.fix == "sudo vpstream init"
    unit.write_text("[Unit]\n")
    runner.on("systemctl", "is-active", stdout="active\n")
    assert doctor.check_service(runner, unit) == doctor.Check("service", "ok", "active")
    runner.on("systemctl", "is-active", returncode=3, stdout="failed\n")
    failed = doctor.check_service(runner, unit)
    assert failed.status == "problem" and failed.detail == "failed" and "journalctl" in failed.fix


def test_tailnet_https_is_needed_for_the_dashboard() -> None:
    runner = FakeRunner(("tailscale",))
    runner.on("tailscale", "status", "--json", stdout=json.dumps(STATUS))
    assert doctor.check_tailnet_https(runner) == doctor.Check("tailnet https", "ok", NAME)
    runner.on("tailscale", "status", "--json", stdout=json.dumps({**STATUS, "CertDomains": None}))
    check = doctor.check_tailnet_https(runner)
    assert check.status == "problem" and "admin console" in check.fix
    runner.on("tailscale", "status", "--json", returncode=1, stderr="Tailscale is stopped.")
    assert doctor.check_tailnet_https(runner).fix == "sudo tailscale up"


def _serve(port: int, target: str) -> str:
    return json.dumps(serve_status({port: target}))


def test_dashboard_check() -> None:
    runner = FakeRunner(("tailscale",))
    runner.on("tailscale", "serve", "status", "--json", stdout="{}")
    missing = doctor.check_dashboard(runner, 1338)
    assert missing.status == "warning" and missing.fix == "sudo vpstream init"
    runner.on(
        "tailscale", "serve", "status", "--json", stdout=_serve(1338, "http://127.0.0.1:1338")
    )
    assert doctor.check_dashboard(runner, 1338).status == "ok"
    runner.on(
        "tailscale", "serve", "status", "--json", stdout=_serve(1338, "http://127.0.0.1:3000")
    )
    taken = doctor.check_dashboard(runner, 1338)
    assert taken.status == "problem" and "http://127.0.0.1:3000" in taken.detail


def test_dashboard_is_checked_only_once_the_service_is_set_up(tmp_path: Path) -> None:
    runner, kwargs = _host(tmp_path)
    assert "dashboard" not in _by_name(doctor.run_checks(runner, **kwargs))
    kwargs["unit_file"].write_text("[Unit]\n")
    runner.on("systemctl", "is-active", stdout="active\n")
    assert _by_name(doctor.run_checks(runner, **kwargs))["dashboard"].status == "warning"


# --- found in the review before milestone 2


@pytest.mark.parametrize(
    ("state", "message"),
    [("Stopped", "Tailscale is stopped"), ("NeedsLogin", "logged out"), ("NoState", "NoState")],
)
def test_tailscale_that_answers_but_is_not_up_counts_as_down(state: str, message: str) -> None:
    # `tailscale status --json` exits 0 in every state; only the answer says it is stopped.
    runner = FakeRunner(("tailscale",))
    runner.on("tailscale", "status", "--json", stdout=json.dumps(node_status(state=state)))
    with pytest.raises(tailscale.TailscaleDown, match=message):
        tailscale.node(runner)
    check = doctor.check_tailnet_https(runner)
    assert check.status == "problem" and "certificate" not in check.detail


def test_the_helper_and_its_folder_must_be_roots_alone(tmp_path: Path) -> None:
    me = os.getuid()
    helper = tmp_path / "bin" / "vpstream-fw"
    helper.parent.mkdir()
    helper.parent.chmod(0o755)
    helper.write_text("#!/bin/sh\n")
    helper.chmod(0o755)
    assert doctor.program_problem(helper, me) is None
    helper.chmod(0o757)
    assert doctor.program_problem(helper, me) == f"{helper} is writable by users other than root"
    helper.chmod(0o755)
    assert doctor.program_problem(helper, me + 1) == f"{helper.parent} is not owned by root"
    helper.parent.chmod(0o757)
    assert doctor.program_problem(helper, me) == f"{helper.parent} is writable by every user"
    helper.parent.chmod(0o755)
    assert "cannot check" in str(doctor.program_problem(tmp_path / "bin" / "nope", me))
    package = tmp_path / "pkg"
    package.mkdir(mode=0o755)
    if me != 0:  # the package folder itself is refused first: the test is not root
        assert doctor.check_install(package, helper=helper).status == "problem"
