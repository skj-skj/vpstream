"""The README's commands against the real command line, both ways: every command and option it
shows exists, and every command and option the CLI has is shown. The README is not in the
wheel, so these run in a checkout only (skipped by `vpstream selftest`)."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from vpstream import cli

README = Path(__file__).resolve().parents[3] / "README.md"
UNDOCUMENTED = {"daemon"}  # what the service runs, not a person
_COMMAND = re.compile(r"(?<![\w/.\[-])vpstream ([a-z][a-z-]*)")
_OPTION = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")
_PLACEHOLDER = re.compile(r"[A-Z][A-Z0-9_.]*|<[^>]*>")

pytestmark = pytest.mark.skipif(not README.is_file(), reason="no README.md (not a checkout)")


def code_snippets(text: str) -> list[str]:
    """Each line of a code block, and each inline code span, as the reader sees it."""
    snippets: list[str] = []
    in_block = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_block = not in_block
        elif in_block:
            snippets.append(line.split(" #", 1)[0])
        else:
            snippets += re.findall(r"`([^`]+)`", line.replace(r"\|", "|"))
    return snippets


def subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    found: dict[str, argparse.ArgumentParser] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            found.update(action.choices)
    return found


def options(parser: argparse.ArgumentParser) -> set[str]:
    return {o for a in parser._actions for o in a.option_strings if o.startswith("--")} - {"--help"}


def choices(parser: argparse.ArgumentParser) -> set[str]:
    """The words the first positional argument takes, when it has a fixed list."""
    for action in parser._actions:
        if (
            not action.option_strings
            and action.choices
            and not isinstance(action, argparse._SubParsersAction)
        ):
            return {str(c) for c in action.choices}
    return set()


def problems(text: str, parser: argparse.ArgumentParser) -> list[str]:
    """What the README text gets wrong about `parser`, or leaves out."""
    commands = subcommands(parser)
    every_option = options(parser).union(*(options(p) for p in commands.values()))
    found: list[str] = []
    shown_commands: set[str] = set()
    shown_options: set[str] = set()
    for snippet in code_snippets(text):
        if not (_COMMAND.search(snippet) or snippet.lstrip().startswith("--")):
            continue  # another program's command, such as journalctl's --no-pager
        shown_options.update(_OPTION.findall(snippet))
        for option in _OPTION.findall(snippet):
            if option not in every_option:
                found.append(f"{snippet!r}: no command has {option}")
        # Options after `vpstream NAME` up to the next `vpstream` belong to NAME.
        parts = _COMMAND.split(snippet)
        for name, rest in zip(parts[1::2], parts[2::2], strict=True):
            sub = commands.get(name)
            if sub is None:
                found.append(f"{snippet!r}: there is no command {name!r}")
                continue
            shown_commands.add(name)
            for option in _OPTION.findall(rest):
                if option not in options(sub) | options(parser):
                    found.append(f"{snippet!r}: `vpstream {name}` has no {option}")
            first = next((w for w in re.split(r"[\s|\[\]]+", rest) if w), "")
            allowed = choices(sub)
            literal = first and not (
                first.startswith(("-", "/", "<")) or _PLACEHOLDER.fullmatch(first)
            )
            if allowed and literal and first not in allowed:
                found.append(f"{snippet!r}: `vpstream {name}` takes no {first!r}")
    for name in sorted(set(commands) - shown_commands - UNDOCUMENTED):
        found.append(f"the command `vpstream {name}` is not shown")
    for name, sub in sorted(commands.items()):
        if name not in UNDOCUMENTED:
            for option in sorted(options(sub) - shown_options):
                found.append(f"`vpstream {name} {option}` is not shown")
    return found


def test_the_readme_matches_the_command_line() -> None:
    assert problems(README.read_text(encoding="utf-8"), cli.build_parser()) == []


# --- the check itself, on a small parser of its own


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vpstream")
    p.add_argument("--version", action="version", version="1")
    sub = p.add_subparsers()
    go = sub.add_parser("start")
    go.add_argument("--file")
    go.add_argument("--ttl")
    dv = sub.add_parser("devices")
    dv.add_argument("action", nargs="?", choices=["enable", "lock"])
    dv.add_argument("device", nargs="?")
    sub.add_parser("daemon")
    return p


GOOD = """\
| `vpstream start --file F` | starts, `--ttl MINUTES` |
| `vpstream devices [enable DEVICE \\| lock]` | who gets in |

```text
vpstream start --file /srv/film.mkv   # --nothing in a comment
vpstream --version
sudo journalctl -u vpstream --no-pager
```
"""


def test_a_readme_that_matches_has_no_problems() -> None:
    assert problems(GOOD, _parser()) == []


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (("--ttl MINUTES", "--tll MINUTES"), "no command has --tll"),
        (("start --file F", "start --file F --version --ttl"), None),
        (("journalctl -u vpstream --no-pager", "vpstream doctor --no-pager"), "no command"),
        (("vpstream start --file F", "vpstream stat --file F"), "there is no command 'stat'"),
        (("devices [enable", "devices [enabel"), "takes no 'enabel'"),
        (("`--ttl MINUTES`", "`x`"), "`vpstream start --ttl` is not shown"),
        (
            ("`vpstream start --file F`", "`vpstream devices --file F`"),
            "`vpstream devices` has no --file",
        ),
        (("| `vpstream devices [enable DEVICE \\| lock]` | who gets in |\n", ""), "devices"),
    ],
)
def test_each_kind_of_drift_is_found(change: tuple[str, str], problem: str | None) -> None:
    assert change[0] in GOOD
    found = problems(GOOD.replace(*change), _parser())
    if problem is None:
        assert found == []
    else:
        assert any(problem in line for line in found), found
