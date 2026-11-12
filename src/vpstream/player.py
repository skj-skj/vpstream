"""The player page of browser mode: what it is told about a video, and its own files.

Everything the page needs is in one JSON object written into it, so a TV browser loads the
page, the scripts and then only the video.
"""

from __future__ import annotations

import json
import re
from html import escape
from importlib import resources
from pathlib import Path
from typing import Any

from vpstream import clients, ffargs, prepared, tracks

MASTER = ffargs.MASTER
SOFT, BURN = clients.SOFT, clients.BURN  # the kinds of subtitles the player switches between
REPORT = "client"  # /ALIAS/N/client?t=... is where the player reports and gets commands
# Sent with every report. A page on another site cannot add a header to a request without
# asking first (a preflight), which Caddy refuses: so a report is the player's, even in a
# browser that sends the cookie along with requests from other sites.
REPORT_HEADER = "X-Vpstream-Player"
JS = "text/javascript; charset=utf-8"
# Name under /ALIAS/assets/ -> content type. Only these are served.
ASSETS = {"player.js": JS, "vtt.js": JS, "hls.min.js": JS}
_PLACEHOLDER = re.compile(r"\{\{(TITLE|ASSETS|SETTINGS)\}\}")


def _choice(spec: str | None) -> tracks.Choice | None:
    try:
        return tracks.parse_choice(spec) if spec else None
    except tracks.SelectionError:
        return None


def choices(
    entry: Path, hls: str = ""
) -> tuple[list[prepared.Track], list[tuple[prepared.Track, dict[str, Any]]]]:
    """A video's audio tracks, and its subtitles as the player switches between them: each
    track that is prepared soft and/or burned in, in track order. `hls` is where the
    video's prepared files are, for the subtitles' addresses."""
    have = prepared.versions(entry)
    options: list[tuple[prepared.Track, dict[str, Any]]] = []
    for track in prepared.meta_tracks(entry, "subtitles"):
        n = track.number
        for kind, made, url in (
            (SOFT, have.soft, f"{hls}subs/{n}.vtt"),
            (BURN, have.burn, f"{hls}burn/{n}/{MASTER}"),
        ):
            if n in made:
                option = {"id": f"{n}-{kind}", "label": track.label, "number": n, "kind": kind}
                options.append((track, {**option, "url": url}))
    return prepared.meta_tracks(entry, "audio"), options


def menu(entry: Path) -> dict[str, list[dict[str, Any]]]:
    """What the dashboard offers for a video: its audio tracks and subtitle choices, as
    the commands of `clients` name them."""
    audio, subs = choices(entry)
    return {
        "audio": [{"value": str(t.number), "label": t.label} for t in audio],
        "subs": [
            {"value": o["id"], "label": o["label"] + (" (burned in)" if o["kind"] == BURN else "")}
            for _, o in subs
        ],
    }


def _start_tracks(
    audio_tracks: list[prepared.Track],
    options: list[tuple[prepared.Track, dict[str, Any]]],
    audio: str | None,
    sub: str | None,
    client: clients.Client | None,
) -> tuple[int, int]:
    """Which audio track (an index, len for Off) and subtitle option (an index, -1 for
    Off) the page starts with: what this client chose last, where this video has it, else
    the stream's choice, else the file's default audio and no subtitles."""
    numbers = [t.number for t in audio_tracks]
    ids = [o["id"] for _, o in options]
    chosen_audio = client.wanted(clients.AUDIO) if client else None
    chosen_sub = client.wanted(clients.SUB) if client else None
    if audio_tracks and chosen_audio in (0, *numbers):
        start_audio = numbers.index(chosen_audio) if chosen_audio else len(numbers)
    elif (chosen := tracks.pick(audio_tracks, _choice(audio))) is not None:
        start_audio = audio_tracks.index(chosen)
    else:
        start_audio = tracks.default_audio(audio_tracks) if audio_tracks else -1
    if chosen_sub in (clients.OFF, *ids):
        return start_audio, ids.index(chosen_sub) if chosen_sub != clients.OFF else -1
    picked = tracks.pick([t for t, _ in options], _choice(sub))
    return start_audio, next((i for i, (t, _) in enumerate(options) if t is picked), -1)


def settings(
    entry: Path,
    *,
    alias: str,
    number: int,
    names: list[str],
    folder: bool,
    audio: str | None,
    sub: str | None,
    client: clients.Client | None = None,
) -> dict[str, Any]:
    """What the player page is told about video `number` (1-based) of a stream: its audio
    tracks, the subtitles that are prepared, which of them it starts with, where it starts
    and where it reports (see `clients`)."""
    hls = f"/{alias}/{number}/hls/"
    audio_tracks, options = choices(entry, hls)
    start_audio, start_sub = _start_tracks(audio_tracks, options, audio, sub, client)
    return {
        "title": names[number - 1] if 0 < number <= len(names) else "",
        "number": number,
        "count": len(names),
        "folder": folder,
        "index": f"/{alias}",
        "next": f"/{alias}/{number + 1}" if folder and number < len(names) else None,
        "master": f"{hls}base/{MASTER}",
        "audio": [
            {"label": t.label, "language": t.language, "number": t.number} for t in audio_tracks
        ],
        "start_audio": start_audio,
        "subs": [option for _, option in options],
        "start_sub": start_sub,
        "start": client.start_position(number, consume=False) if client else 0.0,
        "ack": client.seq if client else 0,
        "report": f"/{alias}/{number}/{REPORT}",
        "assets": f"/{alias}/assets/",
    }


def page(data: dict[str, Any]) -> str:
    """The player page for these settings. The JSON is made safe to sit inside a script
    element: no "</script>" or "<!--" can come out of a title."""
    settings = json.dumps(data)
    for char in "<>&":
        settings = settings.replace(char, f"\\u{ord(char):04x}")
    values = {
        "TITLE": escape(str(data.get("title") or "vpstream")),
        "ASSETS": escape(str(data.get("assets") or "")),
        "SETTINGS": settings,
    }
    template = resources.files("vpstream.assets").joinpath("player.html").read_text("utf-8")
    # In one pass, so a title that holds "{{SETTINGS}}" stays text.
    return _PLACEHOLDER.sub(lambda m: values[m[1]], template)


def asset(name: str) -> bytes:
    return resources.files("vpstream.assets").joinpath(name).read_bytes()
