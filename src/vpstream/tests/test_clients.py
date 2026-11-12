from __future__ import annotations

from typing import Any

import pytest

from vpstream import clients
from vpstream.clients import Client, ClientError


@pytest.mark.parametrize(
    ("value", "number"), [("2", 2), (2, 2), ("off", 0), (" OFF ", 0), ("0", 0), (0, 0)]
)
def test_audio_choices(value: Any, number: int) -> None:
    assert clients.audio_value(value) == number


@pytest.mark.parametrize("value", ["eng", "-1", "1.5", "", None, True, 1.0, "99999", [1]])
def test_audio_choices_that_do_not_read(value: Any) -> None:
    with pytest.raises(ClientError, match="not an audio track"):
        clients.audio_value(value)


@pytest.mark.parametrize(("value", "spec"), [("3-burn", "3-burn"), (" 2-SOFT", "2-soft")])
def test_subtitle_choices(value: str, spec: str) -> None:
    assert clients.sub_value(value) == spec


@pytest.mark.parametrize("value", ["2", "0-soft", "2-hard", "2-soft-x", "eng", 2, None])
def test_subtitle_choices_that_do_not_read(value: Any) -> None:
    with pytest.raises(ClientError, match="not a subtitle choice"):
        clients.sub_value(value)


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("10:10", 610.0), ("1:02:03", 3723.0), ("0:05", 5.0), (90, 90.0), ("90.5", 90.5)],
)
def test_times(value: Any, seconds: float) -> None:
    assert clients.seconds_value(value) == seconds


@pytest.mark.parametrize("value", ["10:60", "-5", "nan", "inf", "1e9", "", "ten", True, None])
def test_times_that_are_not_in_a_video(value: Any) -> None:
    with pytest.raises(ClientError, match="not a time"):
        clients.seconds_value(value)


def test_a_name_is_one_short_line() -> None:
    assert clients.name_value("  Living-room\n\tTV \x1b[31m ") == "Living-room TV [31m"
    assert len(clients.name_value("x" * 100)) == clients.MAX_NAME
    with pytest.raises(ClientError):
        clients.name_value(5)


def report(client: Client, video: int = 1, now: float = 100.0, **values: str) -> list[Any]:
    return client.report(video, values, now)


def test_a_report_is_the_record_and_answers_with_what_waits() -> None:
    client = Client()
    assert report(client, t="600.5", d="5400", p="0", a="2", s="1-soft", ack="0") == []
    assert (client.video, client.position, client.duration, client.paused) == (
        1,
        600.5,
        5400,
        False,
    )
    assert (client.audio, client.sub, client.reported) == (2, "1-soft", 100.0)
    client.command(clients.AUDIO, 1)
    client.command(clients.SEEK, 610.0)
    client.command(clients.AUDIO, 0)  # a newer one of the same kind replaces it
    assert report(client) == [{"seq": 3, "audio": 0}, {"seq": 2, "seek": 610.0}]
    assert client.audio == 2, "the record is what the player says, not what it was asked"
    assert report(client, ack="2") == [{"seq": 3, "audio": 0}]
    assert report(client, ack="3", a="off") == [] and client.audio == 0


def test_a_report_with_values_that_do_not_read_keeps_what_was_there() -> None:
    client = Client()
    report(client, t="60", d="100", a="2", s="off")
    report(client, t="nan", d="-1", a="eng", s="<b>", ack="x", p="0")
    assert (client.position, client.duration, client.audio, client.sub) == (60, 100, 2, "off")
    report(client, t="1e300")
    assert client.position == 60
    client.command(clients.SUB, "off")
    report(client, ack="99999999999999999999")  # too long to be a command number
    assert client.commands


def test_a_seek_is_for_the_video_it_was_asked_for_on() -> None:
    client = Client()
    report(client, video=2)
    client.command(clients.SEEK, 30.0)
    client.command(clients.AUDIO, 2)
    assert report(client, video=3) == [{"seq": 2, "audio": 2}]
    assert clients.SEEK not in client.commands
    fresh = Client()  # one that has not reported yet: the seek goes to whatever it plays
    fresh.command(clients.SEEK, 30.0)
    assert report(fresh, video=4) == [{"seq": 1, "seek": 30.0}]


def test_resume_where_it_left_off_but_not_near_either_end() -> None:
    client = Client()
    report(client, video=1, t="1200", d="5400")
    report(client, video=2, t="300", d="1500")
    assert (client.start_position(1), client.start_position(2)) == (1200, 300)
    report(client, video=1, t="5390")  # watched to the end
    report(client, video=2, t="4")  # started over
    assert (client.start_position(1), client.start_position(2)) == (0, 0)
    assert client.positions == {}
    assert client.start_position(3) == 0


def test_a_start_position_is_used_once_and_beats_the_rest() -> None:
    client = Client()
    report(client, video=1, t="1200", d="5400")
    client.command(clients.SEEK, 50.0)
    assert client.start_position(1, consume=False) == 50, "a seek that waits beats resuming"
    client.set_start(1, 610.0)
    client.set_start(2, 99.0)  # the newer one replaces it, for another video
    assert client.start_position(1) == 50
    assert client.start_position(2, consume=False) == 99 and client.start_at == (2, 99)
    assert client.start_position(2) == 99 and client.start_at is None
    assert client.start_position(2) == 0


def test_a_page_starts_with_what_waits_for_its_player() -> None:
    client = Client()
    report(client, a="1", s="off")
    assert (client.wanted(clients.AUDIO), client.wanted(clients.SUB)) == (1, "off")
    client.command(clients.SUB, "2-soft")
    assert client.wanted(clients.SUB) == "2-soft" and client.wanted(clients.AUDIO) == 1


def test_the_record_survives_a_restart() -> None:
    client = Client()
    report(client, video=2, t="700", d="1500", a="2", s="3-burn", p="0")
    client.command(clients.SEEK, 10.0)
    client.command(clients.SUB, "off")
    client.set_start(1, 610.0)
    again = Client.from_dict(client.to_dict(), videos=2)
    assert again == client
    # The seq carries on, so a player that saw commands before the restart sees new ones.
    assert again is not None
    again.command(clients.AUDIO, 1)
    assert again.commands[clients.AUDIO].seq == 3


def test_a_record_that_was_tampered_with_keeps_only_what_fits() -> None:
    data: dict[str, Any] = {
        "video": 9,  # the stream has 2 videos
        "position": "x",
        "duration": 1e300,
        "reported": -5,
        "paused": "no",
        "audio": "eng",
        "sub": "<script>",
        "positions": {"1": 60, "0": 5, "3": 9, "x": 1, "2": float("nan")},
        "start_at": [5, 10],
        "seq": 2,
        "commands": {
            "audio": {"seq": 1, "value": 2},
            "sub": {"seq": 7, "value": "off"},  # newer than the last command
            "seek": {"seq": 2, "value": "later"},
            "eval": {"seq": 1, "value": 1},
        },
    }
    client = Client.from_dict(data, videos=2)
    assert client is not None
    assert (client.video, client.position, client.duration, client.reported) == (0, 0, 0, 0)
    assert client.paused is True and client.audio is None and client.sub is None
    assert client.positions == {1: 60} and client.start_at is None
    assert list(client.commands) == ["audio"]
    assert Client.from_dict("not a record", 2) is None


def test_the_dashboard_sees_what_waits() -> None:
    client = Client()
    client.command(clients.AUDIO, 2)
    shown = client.describe(100.0)
    assert shown["pending"] == {"audio": 2} and shown["active"] is False
    assert "commands" not in shown and "seq" not in shown
    report(client, now=100.0)
    assert client.describe(100.0 + clients.ACTIVE_SECONDS)["active"] is True
    assert client.describe(100.1 + clients.ACTIVE_SECONDS)["active"] is False


# Superscript 2, Arabic-Indic 1, fullwidth 1: digits to str.isdigit, not numbers here.
@pytest.mark.parametrize("digits", [chr(0xB2), chr(0x661), chr(0xFF11)])
def test_only_ascii_digits_are_numbers(digits: str) -> None:
    with pytest.raises(ClientError):
        clients.audio_value(digits)
    client = Client()
    client.command(clients.AUDIO, 1)
    # A report with them is taken as far as it reads, and never raises.
    assert report(client, t="50", a=digits, ack=digits) == [{"seq": 1, "audio": 1}]
    assert client.position == 50 and client.audio is None
    with pytest.raises(ClientError):
        clients.seconds_value(chr(0x661) + ":00")


def test_a_press_after_a_command_was_made_wins_over_it() -> None:
    client = Client()
    client.command(clients.AUDIO, 3, now=100.0)
    client.command(clients.SEEK, 50.0, now=100.0)
    # The viewer pressed audio 0.5 s ago (at 101.5), after the owner asked (at 100).
    assert report(client, now=102.0, pa="0.5") == [{"seq": 2, "seek": 50.0}]
    assert clients.AUDIO not in client.commands
    # A command made after the press stands.
    client.command(clients.SUB, "off", now=103.0)
    assert report(client, now=104.0, ps="2") == [{"seq": 2, "seek": 50.0}, {"seq": 3, "sub": "off"}]
    assert report(client, now=104.5, pk="0.1", ack="2") == [{"seq": 3, "sub": "off"}]
    assert clients.SEEK not in client.commands


def test_every_open_page_of_a_browser_gets_a_command() -> None:
    client = Client()
    report(client, now=1.0, pg="tab1")
    report(client, now=1.0, pg="tab2")
    client.command(clients.SEEK, 10.0)
    assert report(client, now=2.0, pg="tab1") == [{"seq": 1, "seek": 10.0}]
    assert report(client, now=2.5, pg="tab1", ack="1") == []
    assert clients.SEEK in client.commands, "tab2 has not seen it"
    assert report(client, now=3.0, pg="tab2") == [{"seq": 1, "seek": 10.0}]
    report(client, now=3.5, pg="tab2", ack="1")
    assert client.commands == {}


def test_a_page_that_went_quiet_no_longer_holds_commands() -> None:
    client = Client()
    report(client, now=1.0, pg="closed")
    client.command(clients.AUDIO, 2)
    report(client, now=1.0 + clients.PAGE_SECONDS + 1, pg="open", ack="1")
    assert client.commands == {}
    assert list(client.pages) == ["open"]


def test_pages_are_few_and_their_ids_cannot_be_anything() -> None:
    client = Client()
    for n in range(10):
        report(client, now=1.0 + n, pg=f"p{n}")
    assert sorted(client.pages) == ["p6", "p7", "p8", "p9"]  # the ones that reported last
    report(client, now=20.0, pg="<script>" * 10)
    assert "" in client.pages and len(client.pages) <= clients.MAX_PAGES


def test_a_seek_waits_only_for_the_pages_on_its_video() -> None:
    client = Client()
    report(client, video=1, now=1.0, pg="a")
    report(client, video=2, now=1.0, pg="b")
    client.command(clients.SEEK, 30.0)  # asked while the record was on video 2
    assert report(client, video=1, now=2.0, pg="a") == []
    assert report(client, video=2, now=2.0, pg="b") == [{"seq": 1, "seek": 30.0}]


def test_when_a_command_was_made_survives_a_restart() -> None:
    client = Client()
    client.command(clients.AUDIO, 2, now=500.0)
    again = Client.from_dict(client.to_dict(), videos=1)
    assert again is not None and again.commands[clients.AUDIO].issued == 500.0
