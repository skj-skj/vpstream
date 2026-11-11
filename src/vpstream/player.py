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

from vpstream import ffargs, prepared, tracks

MASTER = ffargs.MASTER
SOFT, BURN = "soft", "burn"  # the kinds of subtitles the player switches between
JS = "text/javascript; charset=utf-8"
# Name under /ALIAS/assets/ -> content type. Only these are served.
ASSETS = {"player.js": JS, "vtt.js": JS, "hls.min.js": JS}
_PLACEHOLDER = re.compile(r"\{\{(TITLE|ASSETS|SETTINGS)\}\}")


def _choice(spec: str | None) -> tracks.Choice | None:
    try:
        return tracks.parse_choice(spec) if spec else None
    except tracks.SelectionError:
        return None


def settings(
    entry: Path,
    *,
    alias: str,
    number: int,
    names: list[str],
    folder: bool,
    audio: str | None,
    sub: str | None,
) -> dict[str, Any]:
    """What the player page is told about video `number` (1-based) of a stream: its audio
    tracks, the subtitles that are prepared (each track soft and/or burned in, in track
    order) and which of them it starts with."""
    have = prepared.versions(entry)
    hls = f"/{alias}/{number}/hls/"
    audio_tracks = prepared.meta_tracks(entry, "audio")
    chosen_audio = tracks.pick(audio_tracks, _choice(audio))
    if chosen_audio is not None:
        start_audio = audio_tracks.index(chosen_audio)
    else:
        start_audio = tracks.default_audio(audio_tracks) if audio_tracks else -1
    options: list[tuple[prepared.Track, dict[str, Any]]] = []
    for track in prepared.meta_tracks(entry, "subtitles"):
        n = track.number
        for kind, made, url in (
            (SOFT, have.soft, f"{hls}subs/{n}.vtt"),
            (BURN, have.burn, f"{hls}burn/{n}/{MASTER}"),
        ):
            if n in made:
                option = {"label": track.label, "number": n, "kind": kind, "url": url}
                options.append((track, option))
    chosen_sub = tracks.pick([t for t, _ in options], _choice(sub))
    start_sub = next((i for i, (t, _) in enumerate(options) if t is chosen_sub), -1)
    return {
        "title": names[number - 1] if 0 < number <= len(names) else "",
        "number": number,
        "count": len(names),
        "folder": folder,
        "index": f"/{alias}",
        "next": f"/{alias}/{number + 1}" if folder and number < len(names) else None,
        "master": f"{hls}base/{MASTER}",
        "audio": [{"label": t.label, "language": t.language} for t in audio_tracks],
        "start_audio": start_audio,
        "subs": [option for _, option in options],
        "start_sub": start_sub,
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
