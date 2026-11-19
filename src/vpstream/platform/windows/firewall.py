"""Windows Firewall: one inbound rule, "vpstream stream", while a stream runs.

It lets in TCP on the stream port, to the Caddy that serves it only, from the local subnet
only, on private (home) networks only. It is added before Caddy starts listening, so
Windows never asks the person whether to allow Caddy (a "Cancel" there would add rules that
block it), and removed when the stream stops. netsh needs administrator rights, which the
window has.
"""

from __future__ import annotations

from vpstream.platform.common import FirewallError
from vpstream.platform.windows.shortcut import powershell, quote
from vpstream.tools import Runner, tail

RULE = "vpstream stream"


def add_args(port: int, program: str) -> list[str]:
    return [
        "netsh", "advfirewall", "firewall", "add", "rule",
        f"name={RULE}", "dir=in", "action=allow", "protocol=TCP", f"localport={port}",
        f"program={program}", "profile=private", "remoteip=localsubnet", "enable=yes",
    ]  # fmt: skip


DELETE = ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={RULE}"]
SHOW = ["netsh", "advfirewall", "firewall", "show", "rule", f"name={RULE}"]  # fails: none


class NetshFirewall:
    """The stream's rule, for `port` and the Caddy at `program`; both can change between
    streams (another port was free, another Caddy found)."""

    def __init__(self, runner: Runner, port: int, program: str) -> None:
        self.runner = runner
        self.port = port
        self.program = program
        self.opened: tuple[int, str] | None = None  # what the rule lets in now

    def open(self) -> None:
        """Caddy's start opens it before Caddy listens; the Control's after: once is enough."""
        if self.opened == (self.port, self.program):
            return
        self.close()  # one rule, for this port and program
        res = self.runner.run(add_args(self.port, self.program), timeout=30)
        if res.returncode == 0:
            self.opened = (self.port, self.program)
        else:
            raise FirewallError(
                f"Windows Firewall did not let the stream in: {tail(res, stdout=True)}"
            )

    def close(self) -> None:
        self.opened = None
        res = self.runner.run(DELETE, timeout=30)
        # netsh fails a delete of nothing too, in the language of Windows: is it there?
        if res.returncode != 0 and self.runner.run(SHOW, timeout=30).returncode == 0:
            raise FirewallError(
                f"Windows Firewall did not remove the stream's rule: {tail(res, stdout=True)}"
            )


def blocking_script(program: str) -> str:
    return (
        f"Get-NetFirewallApplicationFilter -Program {quote(program)} -ErrorAction "
        "SilentlyContinue | Get-NetFirewallRule | Where-Object { $_.Action -eq 'Block' -and "
        "$_.Enabled -eq 'True' -and $_.Direction -eq 'Inbound' } | ForEach-Object "
        "{ $_.DisplayName }"
    )


def blocking_rules(runner: Runner, program: str) -> list[str]:
    """Rules that block `program` (Windows adds them when its question is cancelled); they
    win over vpstream's. [] when there are none, or when it cannot be told."""
    res = runner.run(powershell(blocking_script(program)), timeout=60)
    if res.returncode != 0:
        return []
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


def remove_blocking(runner: Runner, program: str) -> None:
    """Remove the rules that block `program` (the person asks, with a button)."""
    script = blocking_script(program).replace(
        "ForEach-Object { $_.DisplayName }", "Remove-NetFirewallRule"
    )
    res = runner.run(powershell(script), timeout=60)
    if res.returncode != 0:
        raise FirewallError(f"Windows Firewall kept the rules that block Caddy: {tail(res)}")
