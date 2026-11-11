"""The seek index of a Matroska (MKV, WebM) file, read without reading the whole file.

A player seeks with the index (Cues): it looks a time up and asks for the byte where it is.
VLC for Android resumes a film before it turns the video on, so it looks the time up among
the audio tracks. Most files index only the video, so it finds nothing, starts at the
beginning and reads up to the saved time: minutes, and that much data, on a phone. An index
is proper here when every video and audio track is in it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

EBML = 0x1A45DFA3
SEGMENT = 0x18538067
SEEK_HEAD, SEEK, SEEK_ID, SEEK_POSITION = 0x114D9B74, 0x4DBB, 0x53AB, 0x53AC
TRACKS, TRACK_ENTRY, TRACK_NUMBER, TRACK_TYPE = 0x1654AE6B, 0xAE, 0xD7, 0x83
CUES, CUE_POINT, CUE_TRACK_POSITIONS, CUE_TRACK = 0x1C53BB6B, 0xBB, 0xB7, 0xF7
CLUSTER = 0x1F43B675
KINDS = {1: "video", 2: "audio"}  # the track types that must be in the index
MAX_ELEMENT = 64 * 1024 * 1024  # Tracks and Cues are read whole; real ones are a few MB
MAX_TOP_LEVEL = 256


class MkvError(ValueError):
    """The file is Matroska but cannot be read as such."""


def _vint(data: bytes, pos: int, keep_marker: bool) -> tuple[int, int, bool]:
    """A variable-length number at `pos`: its value, its length, and whether it is the
    "unknown size" value."""
    if pos >= len(data):
        raise MkvError("the file ends inside an element")
    first = data[pos]
    length = 1
    while length <= 8 and not first & (0x80 >> (length - 1)):
        length += 1
    if length > 8 or pos + length > len(data):
        raise MkvError("not a valid element header")
    value = first if keep_marker else first & (0xFF >> length)
    for byte in data[pos + 1 : pos + length]:
        value = (value << 8) | byte
    unknown = not keep_marker and value == (1 << (7 * length)) - 1
    return value, length, unknown


def _header(data: bytes, pos: int) -> tuple[int, int, int, bool]:
    """An element's id, the start of its body, its size, and whether the size is unknown."""
    eid, id_len, _ = _vint(data, pos, True)
    size, size_len, unknown = _vint(data, pos + id_len, False)
    return eid, pos + id_len + size_len, size, unknown


def _children(data: bytes) -> Iterator[tuple[int, bytes]]:
    pos = 0
    while pos < len(data):
        eid, body, size, unknown = _header(data, pos)
        if unknown or body + size > len(data):
            raise MkvError("an element runs past its parent")
        yield eid, data[body : body + size]
        pos = body + size


def _uint(data: bytes) -> int:
    return int.from_bytes(data, "big")


def _read_at(fh: BinaryIO, pos: int, count: int) -> bytes:
    fh.seek(pos)
    return fh.read(count)


def _element(fh: BinaryIO, pos: int, want: int) -> bytes:
    """The body of the element at `pos`, which must be a `want`."""
    eid, body, size, unknown = _header(_read_at(fh, pos, 12), 0)
    if eid != want or unknown or size > MAX_ELEMENT:
        raise MkvError(f"no usable element {want:#x} at byte {pos}")
    data = _read_at(fh, pos + body, size)
    if len(data) != size:
        raise MkvError("the file ends inside an element")
    return data


def _top_level(fh: BinaryIO) -> tuple[dict[int, int], int]:
    """Where the Tracks and the Cues are (absolute byte positions), from the elements before
    the first cluster and from the SeekHead."""
    head = _read_at(fh, 0, 64)
    eid, body, size, _ = _header(head, 0)
    if eid != EBML:
        raise MkvError("not a Matroska file")
    pos = body + size
    eid, body, _, _ = _header(_read_at(fh, pos, 12), 0)
    if eid != SEGMENT:
        raise MkvError("no Segment after the header")
    segment = pos + body  # positions in the SeekHead count from here
    found: dict[int, int] = {}
    pos = segment
    for _ in range(MAX_TOP_LEVEL):
        raw = _read_at(fh, pos, 12)
        if len(raw) < 2:
            break
        eid, body, size, unknown = _header(raw, 0)
        if eid == CLUSTER:
            break
        found.setdefault(eid, pos)
        if eid == SEEK_HEAD and not unknown:
            for child, value in _children(_element(fh, pos, SEEK_HEAD)):
                if child != SEEK:
                    continue
                fields = dict(_children(value))
                if SEEK_ID in fields and SEEK_POSITION in fields:
                    target = _uint(fields[SEEK_ID])
                    found.setdefault(target, segment + _uint(fields[SEEK_POSITION]))
        if unknown:
            break
        pos += body + size
    return found, segment


def track_kinds(data: bytes) -> dict[int, str]:
    """Track number -> "video" or "audio", from the body of Tracks. Other tracks are left
    out: they need no index."""
    kinds: dict[int, str] = {}
    for eid, entry in _children(data):
        if eid != TRACK_ENTRY:
            continue
        fields = dict(_children(entry))
        number, kind = fields.get(TRACK_NUMBER), fields.get(TRACK_TYPE)
        if number is not None and kind is not None and _uint(kind) in KINDS:
            kinds[_uint(number)] = KINDS[_uint(kind)]
    return kinds


def indexed_tracks(data: bytes) -> set[int]:
    """Every track number that has a seek point, from the body of Cues."""
    tracks: set[int] = set()
    for eid, point in _children(data):
        if eid != CUE_POINT:
            continue
        for child, positions in _children(point):
            if child == CUE_TRACK_POSITIONS:
                tracks.update(_uint(v) for e, v in _children(positions) if e == CUE_TRACK)
    return tracks


def is_matroska(path: str | Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return _read_at(fh, 0, 4) == EBML.to_bytes(4, "big")
    except OSError:
        return False


def index_problem(path: str | Path) -> str | None:
    """Why players cannot seek in this Matroska file straight away, or None when they can
    (or it is not Matroska, which has an index of its own kind)."""
    if not is_matroska(path):
        return None
    try:
        with open(path, "rb") as fh:
            where, _ = _top_level(fh)
            if TRACKS not in where:
                return "it lists no tracks"
            kinds = track_kinds(_element(fh, where[TRACKS], TRACKS))
            if CUES not in where:
                return "it has no seek index"
            cued = indexed_tracks(_element(fh, where[CUES], CUES))
    except (OSError, MkvError) as exc:
        return f"its seek index cannot be read: {exc}"
    missing = sorted({kind for number, kind in kinds.items() if number not in cued})
    if missing:
        return f"its seek index has no {' or '.join(missing)} entries"
    return None
