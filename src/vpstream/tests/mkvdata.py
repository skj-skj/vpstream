"""Small Matroska files made byte by byte, to test the seek index check without ffmpeg."""

from __future__ import annotations

from pathlib import Path

from vpstream import mkv

TYPES = {"video": 1, "audio": 2, "subtitles": 0x11}


def _id(eid: int) -> bytes:
    return eid.to_bytes((eid.bit_length() + 7) // 8, "big")


def element(eid: int, body: bytes) -> bytes:
    return _id(eid) + (0x01 << 56 | len(body)).to_bytes(8, "big") + body  # 8-byte size


def uint(eid: int, value: int) -> bytes:
    return element(eid, value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big"))


def tracks(kinds: list[str]) -> bytes:
    entries = b"".join(
        element(
            mkv.TRACK_ENTRY,
            uint(mkv.TRACK_NUMBER, n) + uint(mkv.TRACK_TYPE, TYPES[kind]),
        )
        for n, kind in enumerate(kinds, 1)
    )
    return element(mkv.TRACKS, entries)


def cues(numbers: list[int]) -> bytes:
    points = b"".join(
        element(
            mkv.CUE_POINT,
            uint(0xB3, t * 1000)
            + element(mkv.CUE_TRACK_POSITIONS, uint(mkv.CUE_TRACK, n) + uint(0xF1, 0)),
        )
        for t in range(3)
        for n in numbers
    )
    return element(mkv.CUES, points)


def write(
    path: Path,
    kinds: list[str] | tuple[str, ...] = ("video", "audio"),
    indexed: list[int] | None = None,
    where: str = "end",  # "end" (found through the SeekHead), "front", or "none"
    cluster: bytes = b"\x00" * 4096,
) -> Path:
    """A file with these tracks, numbered from 1, and seek points for `indexed` (default:
    all of them)."""
    kinds = list(kinds)
    index = cues(list(range(1, len(kinds) + 1)) if indexed is None else indexed)
    head = element(mkv.EBML, element(0x4282, b"matroska"))
    front = tracks(kinds) + (index if where == "front" else b"")
    clusters = element(mkv.CLUSTER, cluster)
    at_end = where == "end"

    def seekhead(cues_at: int) -> bytes:
        # Fixed 8-byte positions, so its size does not depend on them.
        wanted = [(mkv.TRACKS, tracks_at)] + ([(mkv.CUES, cues_at)] if at_end else [])
        return element(
            mkv.SEEK_HEAD,
            b"".join(
                element(
                    mkv.SEEK,
                    element(mkv.SEEK_ID, _id(eid))
                    + element(mkv.SEEK_POSITION, at.to_bytes(8, "big")),
                )
                for eid, at in wanted
            ),
        )

    tracks_at = 0
    tracks_at = len(seekhead(0))  # positions count from the start of the Segment's body
    cues_at = tracks_at + len(front) + len(clusters)
    segment = seekhead(cues_at) + front + clusters + (index if at_end else b"")
    path.write_bytes(head + element(mkv.SEGMENT, segment))
    return path
