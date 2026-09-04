"""--long-poll-wait: was parsed into Config and then silently never read by
the --once path, which called run_once with a hardcoded wait=0 regardless
of what the flag said. Fixed by threading config.long_poll_wait into that
call. These tests exercise the real collector.cli.main() end to end (argument
parsing through to the wait value a fetch actually uses), with the network
client replaced by a fake so no real HTTP happens.
"""

from helpers import FakeClient

import collector.cli as cli_module
from collector.cli import main


def _empty_page(room):
    return {"room": room, "count": 0, "first_seq": None, "last_seq": 0, "generation": 0, "messages": []}


def _room_page_waits(client):
    return {call[1]: call[3] for call in client.calls if call[0] == "room_page"}


def test_long_poll_wait_flag_reaches_the_once_path(tmp_path, monkeypatch):
    client = FakeClient(
        rooms_overview={"rooms": [], "total": 0},
        room_pages={"events": _empty_page("events"), "lobby": _empty_page("lobby")},
    )
    monkeypatch.setattr(cli_module, "TechnocoreClient", lambda *a, **k: client)

    main(["--data-dir", str(tmp_path), "--once", "--long-poll-wait", "7"])

    waits = _room_page_waits(client)
    assert waits == {"events": 7, "lobby": 7}


def test_long_poll_wait_default_is_zero_unchanged(tmp_path, monkeypatch):
    # Confirms the fix does not change today's observed default: a plain
    # --once, with the flag absent, still fetches with wait=0.
    client = FakeClient(
        rooms_overview={"rooms": [], "total": 0},
        room_pages={"events": _empty_page("events"), "lobby": _empty_page("lobby")},
    )
    monkeypatch.setattr(cli_module, "TechnocoreClient", lambda *a, **k: client)

    main(["--data-dir", str(tmp_path), "--once"])

    waits = _room_page_waits(client)
    assert waits == {"events": 0, "lobby": 0}
