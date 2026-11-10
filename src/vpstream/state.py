"""What the daemon remembers across restarts: whether a stream is running."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vpstream.files import write_atomic

FILE_NAME = "state.json"


@dataclass(frozen=True)
class State:
    stream: dict[str, Any] | None = None  # the running stream, or None


def load(path: Path) -> tuple[State, str | None]:
    """Read the state file. Returns the state and, when the file was unusable, why.

    An unusable file counts as "no stream running", which keeps the stream port closed.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return State(), None
    except (OSError, ValueError) as exc:
        return State(), f"cannot read {path}: {exc}"
    stream = data.get("stream") if isinstance(data, dict) else None
    if not isinstance(data, dict) or not (stream is None or isinstance(stream, dict)):
        return State(), f"{path} does not hold a vpstream state"
    return State(stream=stream), None


def save(path: Path, state: State) -> None:
    """Write the state file whole, so a crash never leaves half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, json.dumps({"stream": state.stream}) + "\n")
