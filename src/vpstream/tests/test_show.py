from __future__ import annotations

from vpstream import show


def test_status_says_where_the_dashboard_is_and_how_it_stands() -> None:

    public = {
        "mode": "public",
        "url": "https://203.0.113.5:1338/",
        "port_state": "open",
        "certificate": "AB:CD",
        "password": True,
    }
    line = show.dashboard_line(public)
    assert line.startswith("https://203.0.113.5:1338/ (public, port open)")
    assert "certificate authority SHA-256 AB:CD" in line
    down = {
        **public,
        "url": None,
        "problem": "Caddy failed",
        "password": False,
        "certificate": None,
    }
    line = show.dashboard_line(down)
    assert "Caddy failed" in line and "NO PASSWORD" in line and "SHA-256" not in line
    tailnet = {"mode": "tailnet", "url": "https://host.ts.net:1338/"}
    assert show.dashboard_line(tailnet) == "https://host.ts.net:1338/ (Tailscale only)"


def test_status_tells_https_and_its_certificate() -> None:
    import time

    assert show._https_line({"enabled": False}) is None
    assert "no certificate yet" in str(show._https_line({"enabled": True, "certificate": None}))
    soon = {"host": "203.0.113.5", "expires": time.time() + 2 * 3600 + 30}
    line = str(show._https_line({"enabled": True, "certificate": soon}))
    assert "certificate for 203.0.113.5 until" in line and "2h" in line and "left" in line
    old = {"host": "203.0.113.5", "expires": 1.0}
    assert "expired" in str(show._https_line({"enabled": True, "certificate": old}))
    info = {"version": "1", "https": {"enabled": True, "certificate": None}, "dashboard": {}}
    assert "HTTPS:        on; no certificate yet" in show.format_status(info, "1")


def test_a_user_agent_cannot_write_control_characters_to_the_terminal() -> None:
    device = {
        "id": "192.0.2.1",
        "ip": "192.0.2.1",
        "kind": "player",
        "allowed": None,
        "user_agent": "VLC\u009b2J\x1b[31m/3.0",
        "requests": 1,
        "blocked": 0,
        "bytes": 0,
        "last_seen": 1.0,
        "passes": True,
    }
    out = show.format_devices({"devices": [device]}, 2.0)
    assert "\u009b" not in out and "\x1b" not in out
    assert "VLC?2J?[31m/3.0" in out


def test_status_notes_a_version_mismatch() -> None:
    info = {"version": "0.3.0", "uptime_seconds": 3725, "pid": 7, "stream_port": 1337}
    info["dashboard"] = {
        "address": None,
        "url": None,
        "problem": "the dashboard is not available: x",
    }
    text = show.format_status(info, "0.4.0")
    assert "up 1h 02m (pid 7)" in text
    assert "Dashboard:    the dashboard is not available: x" in text
    assert "sudo systemctl restart vpstream" in text
    assert "restart" not in show.format_status(info, "0.3.0")


def test_format_devices_and_states() -> None:
    stream = {
        "devices": [
            {
                "ip": "192.0.2.1",
                "allowed": None,
                "passes": True,
                "wl": False,
                "requests": 3,
                "blocked": 0,
                "bytes": 1_500_000,
                "last_seen": 90.0,
                "user_agent": "VLC/3.0.20",
            },
            {
                "ip": "2001:db8::1",
                "allowed": None,
                "passes": False,
                "wl": True,
                "requests": 2,
                "blocked": 2,
                "bytes": 0,
                "last_seen": 0,
                "user_agent": "",
                "code": "0420",
            },
            {"ip": "192.0.2.3", "allowed": True, "passes": True, "wl": True},
            {"ip": "192.0.2.4", "allowed": False, "passes": False, "wl": False},
            {
                "id": "b-0a1b2c",
                "kind": "browser",
                "ip": "192.0.2.5",
                "name": "Living-room TV",
                "allowed": True,
                "passes": True,
                "user_agent": "JioSphere",
            },
        ]
    }
    lines = [" ".join(line.split()) for line in show.format_devices(stream, 100.0).splitlines()]
    assert lines[0] == "DEVICE ADDRESS STATE CODE REQUESTS BLOCKED SENT LAST SEEN PLAYER"
    assert lines[1] == "192.0.2.1 192.0.2.1 allowed - 3 0 1.5 MB 10s ago VLC/3.0.20"
    assert lines[2] == "2001:db8::1 2001:db8::1 waiting WL 0420 2 2 0 B never -"
    assert "enabled WL" in lines[3] and "disabled" in lines[4]
    # A browser is listed by its id, and by the name the owner gave it.
    assert lines[5].split()[:3] == ["b-0a1b2c", "192.0.2.5", "enabled"]
    assert lines[5].endswith("Living-room TV")
    assert "No device" in show.format_devices({"devices": []}, 1.0)


def test_format_stream_of_one_file() -> None:
    text = show.format_stream(
        {
            "alias": "aaa",
            "mode": "direct",
            "policy": "locked",
            "seconds_left": None,
            "source": "/srv/film.mkv",
            "kind": "file",
            "url": "http://192.0.2.9:1337/aaa",
            "videos": [{"number": 1, "name": "film.mkv", "url": "http://192.0.2.9:1337/aaa/1"}],
        }
    )
    assert text.splitlines() == [
        "Stream:       /aaa (direct, policy locked, no time limit)",
        "Source:       /srv/film.mkv",
        "Address:      http://192.0.2.9:1337/aaa",
    ]


def test_format_stream_shows_what_is_being_prepared() -> None:
    url = "http://192.0.2.9:1337/aaa"
    stream = {
        "alias": "aaa",
        "mode": "direct",
        "policy": "open",
        "seconds_left": None,
        "source": "/srv/show",
        "kind": "dir",
        "url": url,
        "videos": [
            {"number": 1, "name": "e1.mkv", "url": f"{url}/1", "preparing": True, "note": None},
            {
                "number": 2,
                "name": "e2.mkv",
                "url": f"{url}/2",
                "preparing": False,
                "note": "its seek index has no audio entries, so VLC resumes it slowly",
            },
        ],
    }
    lines = show.format_stream(stream).splitlines()
    assert lines[3] == f"  1  {url}/1  e1.mkv  [preparing]"
    assert lines[4] == f"  2  {url}/2  e2.mkv"
    assert lines[5].startswith("Preparing:    1 MKV file gets a full seek index")
    assert (
        lines[6] == "note: video 2: its seek index has no audio entries, so VLC resumes it slowly"
    )


def test_format_clients() -> None:
    menu = {
        "audio": [{"value": "1", "label": "English"}, {"value": "2", "label": "Japanese"}],
        "subs": [{"value": "3-burn", "label": "Signs (burned in)"}],
    }
    client = {"video": 1, "position": 610, "duration": 5400, "paused": False, "audio": 2}
    stream = {
        "videos": [{"number": 1, "menu": menu}],
        "devices": [
            {"id": "192.0.2.2", "kind": "player", "passes": True},
            {
                "id": "b-000001",
                "kind": "browser",
                "passes": True,
                "name": "Living-room TV",
                "client": {
                    **client,
                    "sub": "3-burn",
                    "sub_size": "l",
                    "reported": 99,
                    "active": True,
                    "pending": {"seek": 3723, "sub_size": "xs"},
                    "start_at": [1, 65],
                },
            },
            {
                "id": "b-000002",
                "kind": "browser",
                "passes": True,
                "user_agent": "Chrome",
                "client": {**client, "reported": 10, "paused": True, "sub": "off"},
            },
            {"id": "b-000003", "kind": "browser", "passes": True, "client": None},
            {"id": "b-000004", "kind": "browser", "passes": False, "allowed": None, "wl": True},
        ],
    }
    lines = show.format_clients(stream, 100.0).splitlines()
    assert lines[0].split()[:3] == ["DEVICE", "STATE", "VIDEO"] and len(lines) == 5
    tv = lines[1]
    assert tv.startswith("b-000001") and "playing" in tv and "10:10 / 1:30:00" in tv
    assert "Japanese" in tv and "Signs (burned in)" in tv and "Living-room TV" in tv
    assert " large " in tv, "the subtitle size by its name"
    assert "seek 3723, subtitle size extra small, start 1 at 1:05" in tv
    assert "gone 1m 30s ago" in lines[2] and "off" in lines[2] and lines[2].endswith("Chrome")
    assert "not playing yet" in lines[3]
    assert "waiting WL" in lines[4]
    assert "No browser" in show.format_clients({"devices": stream["devices"][:1]}, 1.0)


def test_format_together() -> None:
    stream = {
        "videos": [{"name": "Film", "menu": {"audio": [{"value": "2", "label": "Japanese"}]}}],
        "devices": [{"id": "b-000001", "kind": "browser", "name": "TV"}],
        "together": {
            "video": 1,
            "position": 754.0,
            "duration": 5400.0,
            "paused": False,
            "audio": 2,
            "sub": None,
            "pending": ["seek"],
            "changed_by": "b-000001",
            "changed_ago": 3.0,
            "pages": [
                {"device": "b-000001", "drift": 0.34, "cooldown": 0},
                {"device": "b-000009", "drift": None, "cooldown": 6.2},
                {"device": "b-000001", "drift": -7.04, "cooldown": 0, "out_of_sync": True},
            ],
        },
    }
    lines = show.format_together(stream).splitlines()
    assert lines[:5] == [
        "Video:        1. Film",
        "At:           12:34 / 1:30:00, playing (changing: seek)",
        "Audio:        Japanese",
        "Subtitles:    as the video starts",
        "Changed:      by TV, 3s ago",
    ]
    assert lines[6].split() == ["DEVICE", "OFF", "BY", "COOLDOWN", "NAME"]
    assert lines[7].split() == ["b-000001", "+0.3", "s", "-", "TV"]
    assert lines[8].split() == ["b-000009", "-", "6", "s"]
    assert lines[9].split() == ["b-000001", "-7.0", "s,", "out", "of", "sync", "-", "TV"]
    stream["together"].update(changed_by="", pages=[])  # type: ignore[attr-defined]
    assert show.format_together(stream).splitlines()[-1] == "Changed:      at the end, 3s ago"
    assert "not watched together" in show.format_together({"together": None})
