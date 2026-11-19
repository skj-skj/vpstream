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
IMPLICIT = {"list"}  # the action a command takes when it is given none
SPLIT = "a code span goes on to the next line: "
_COMMAND = re.compile(r"(?<![\w/.\[-])vpstream ([a-z][a-z-]*)")
_OPTION = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")
_PLACEHOLDER = re.compile(r"[A-Z][A-Z0-9_.]*|<[^>]*>")

pytestmark = pytest.mark.skipif(not README.is_file(), reason="no README.md (not a checkout)")


def code_snippets(text: str) -> list[list[str]]:
    """The code the reader sees, line by line: each line of a code block, or the inline code
    spans of a line of text (a table row's spans stay together, so an option in one cell
    belongs to the command in another)."""
    lines: list[list[str]] = []
    in_block = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_block = not in_block
        elif in_block:
            lines.append([re.sub(r"(^|\s)#.*", "", line)])  # without its comment
        elif line.count("`") % 2:
            # A span over two lines is read as none, so its command would go unchecked.
            lines.append([f"{SPLIT}{line}"])
        elif spans := re.findall(r"`([^`]+)`", line.replace(r"\|", "|")):
            lines.append(spans)
    return lines


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


def valued(parser: argparse.ArgumentParser) -> set[str]:
    """The options that take a value, such as --sub in `--sub eng`."""
    return {o for a in parser._actions if a.nargs != 0 for o in a.option_strings}


class _Readme:
    """What a README shows, and what it gets wrong, against one parser."""

    def __init__(self, parser: argparse.ArgumentParser) -> None:
        self.top = parser
        self.commands = subcommands(parser)
        self.every_option = options(parser).union(*(options(p) for p in self.commands.values()))
        self.found: list[str] = []
        self.shown: set[tuple[str, str]] = set()  # (command, option or choice)

    def options_of(self, snippet: str, name: str | None, text: str) -> None:
        """Options in `text`, which belongs to command `name` (None: to whichever has it)."""
        for option in _OPTION.findall(text):
            if name is None:
                if option not in self.every_option:
                    self.found.append(f"{snippet!r}: no command has {option}")
                owners = [n for n, p in self.commands.items() if option in options(p)]
                self.shown.update((n, option) for n in owners)
            elif option in options(self.commands[name]) | options(self.top):
                self.shown.add((name, option))
            else:
                self.found.append(f"{snippet!r}: `vpstream {name}` has no {option}")

    def words_of(self, snippet: str, name: str, text: str) -> None:
        """The literal words after `vpstream NAME`, against its positional choices."""
        sub = self.commands[name]
        allowed, takes_value, after_option = choices(sub), valued(sub), False
        for word in (w for w in re.split(r"[\s|\[\]]+", text) if w):
            if word.startswith("-"):
                after_option = word in takes_value
                continue
            if allowed and not after_option and re.fullmatch(r"[a-z][a-z-]*", word):
                if word in allowed:
                    self.shown.add((name, word))
                else:
                    self.found.append(f"{snippet!r}: `vpstream {name}` takes no {word!r}")
            after_option = False

    def line(self, snippets: list[str]) -> None:
        current: str | None = None  # the last command named on this line
        for snippet in snippets:
            if snippet.startswith(SPLIT):
                self.found.append(snippet)
                continue
            head, *pairs = _COMMAND.split(snippet)
            if current is not None:
                self.options_of(snippet, current, head)
            elif head.lstrip().startswith("--"):
                self.options_of(snippet, None, head)
            # else: another program's command, such as journalctl's --no-pager
            for name, rest in zip(pairs[0::2], pairs[1::2], strict=True):
                if name not in self.commands:
                    self.found.append(f"{snippet!r}: there is no command {name!r}")
                    current = None
                    continue
                current = name
                self.shown.add((name, ""))
                self.options_of(snippet, name, rest)
                self.words_of(snippet, name, rest)

    def missing(self) -> list[str]:
        gaps: list[str] = []
        for name, sub in sorted(self.commands.items()):
            if name in UNDOCUMENTED:
                continue
            if (name, "") not in self.shown:
                gaps.append(f"the command `vpstream {name}` is not shown")
            for word in sorted(options(sub) | choices(sub) - IMPLICIT):
                if (name, word) not in self.shown:
                    gaps.append(f"`vpstream {name} {word}` is not shown")
        return gaps


def problems(text: str, parser: argparse.ArgumentParser) -> list[str]:
    """What the README text gets wrong about `parser`, or leaves out."""
    readme = _Readme(parser)
    for snippets in code_snippets(text):
        readme.line(snippets)
    return readme.found + readme.missing()


def every_command() -> argparse.ArgumentParser:
    """The command line, with the commands only Windows has added: the README documents
    both."""
    from vpstream.platform.windows import commands as windows

    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    if "windows-install" not in sub.choices:  # on Windows they are there already
        windows.add_parsers(sub)
    return parser


def test_the_readme_matches_the_command_line() -> None:
    assert problems(README.read_text(encoding="utf-8"), every_command()) == []


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
# a comment line names vpstream group and --nothing
sudo journalctl -u vpstream --no-pager
```
"""


def test_a_readme_that_matches_has_no_problems() -> None:
    assert problems(GOOD, _parser()) == []


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (("--ttl MINUTES", "--tll MINUTES"), "`vpstream start` has no --tll"),
        (("start --file F", "start --file F --version --ttl"), None),
        (("journalctl -u vpstream --no-pager", "vpstream doctor --no-pager"), "no command"),
        (("vpstream start --file F", "vpstream stat --file F"), "there is no command 'stat'"),
        (("devices [enable", "devices [enabel"), "takes no 'enabel'"),
        (("`--ttl MINUTES`", "`--ttl\nMINUTES`"), "a code span goes on to the next line"),
        (("\\| lock]", "\\| lcok]"), "takes no 'lcok'"),  # every alternative, not the first
        (("DEVICE \\| lock]", "DEVICE]"), "`vpstream devices lock` is not shown"),
        (("| who gets in |", "| who gets in, `--ttl` |"), "`vpstream devices` has no --ttl"),
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
