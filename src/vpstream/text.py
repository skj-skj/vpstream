"""Short human forms of durations and sizes, for the command line."""

from __future__ import annotations


def duration(seconds: float) -> str:
    """45s, 3m 12s, 2h 05m, 3d 4h."""
    total = int(seconds)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def size(count: float) -> str:
    """0 B, 999 B, 1.0 kB ... in GB at most."""
    for unit in ("B", "kB", "MB", "GB"):
        if count < 1000 or unit == "GB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1000
    raise AssertionError("unreachable")


def table(rows: list[list[str]], indent: str = "") -> list[str]:
    """Rows as lines of columns lined up two spaces apart. The last column is not padded,
    so a long one (a path, a user agent) only makes its own line longer."""
    if not rows:
        return []
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    return [
        indent
        + "  ".join([*(c.ljust(w) for c, w in zip(row, widths, strict=False)), row[-1]]).rstrip()
        for row in rows
    ]
