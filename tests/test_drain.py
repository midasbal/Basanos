"""Draining a fast room: one fetch_and_store() pass must page through the
whole backlog (not just the first `limit` messages) until it catches up,
subject to the per-pass page cap.
"""

from helpers import FakeClient, load_fixture

from collector.core import RoomFollower
from collector.storage import load_json, read_jsonl, save_json_atomic


def test_drain_walks_every_page_until_short_page_signals_caught_up(tmp_path):
    fixture = load_fixture("drain_fast_room.fixture.json")
    client = FakeClient(room_pages={"lobby": fixture["pages"]})
    follower = RoomFollower(client, str(tmp_path), "lobby", source="test", page_limit=5)
    save_json_atomic(follower.state_path, {"since": 1000})

    result = follower.fetch_and_store()

    # All 3 pages (5 + 5 + 3 = 13 messages) were drained in one pass, not
    # just the first page's worth.
    assert result["pages_fetched"] == 3
    assert result["new_count"] == 13
    assert result["since_after"] == 1013
    assert result["capped"] is False
    assert result["failed"] is False

    stored = read_jsonl(follower.messages_path)
    seqs = [rec["seq"] for rec in stored]
    assert seqs == list(range(1001, 1014))
    assert len(seqs) == len(set(seqs))  # dedupe still holds across every page

    # Every page's fetch used the *advancing* cursor, not the pass's
    # starting cursor -- proof paging genuinely walked forward.
    requested_sinces = [call[2] for call in client.calls]
    assert requested_sinces == [1000, 1005, 1010]

    assert load_json(follower.state_path) == {"since": 1013}


def test_drain_respects_max_pages_per_drain_cap(tmp_path):
    fixture = load_fixture("drain_fast_room.fixture.json")
    # Only the first two (full) pages are reachable before the cap bites;
    # the room still has a backlog (page 2 was a full page) when we stop.
    client = FakeClient(room_pages={"lobby": fixture["pages"][:2]})
    follower = RoomFollower(
        client, str(tmp_path), "lobby", source="test", page_limit=5, max_pages_per_drain=2
    )
    save_json_atomic(follower.state_path, {"since": 1000})

    result = follower.fetch_and_store()

    assert result["pages_fetched"] == 2
    assert result["capped"] is True
    assert result["failed"] is False
    assert result["new_count"] == 10
    assert result["since_after"] == 1010  # cursor still advanced through what was fetched

    caps = read_jsonl(follower.drain_caps_path)
    assert len(caps) == 1
    assert caps[0]["room"] == "lobby"
    assert caps[0]["pages_fetched"] == 2
    assert caps[0]["since_after"] == 1010

    # Nothing was hidden: nothing raised, the backlog just resumes on the
    # next pass from where the cap left the cursor.
    assert read_jsonl(follower.gaps_path) == []


def test_drain_gap_detection_applies_to_every_page(tmp_path):
    # Page 1 is contiguous from the persisted cursor; page 2 (fetched using
    # page 1's advanced cursor) is deliberately made to skip ahead, as if
    # the ring dropped messages mid-drain.
    fixture = load_fixture("drain_fast_room.fixture.json")
    page1 = fixture["pages"][0]  # first_seq=1001, last_seq=1005, count=5 (full -> keep draining)
    page2_with_gap = dict(fixture["pages"][1])
    page2_with_gap["first_seq"] = 1008  # should have been 1006 -- 2 messages missing
    page2_with_gap["messages"] = [m for m in page2_with_gap["messages"] if m["seq"] >= 1008]
    page2_with_gap["count"] = len(page2_with_gap["messages"])  # 3, short -> caught up after this

    client = FakeClient(room_pages={"lobby": [page1, page2_with_gap]})
    follower = RoomFollower(client, str(tmp_path), "lobby", source="test", page_limit=5)
    save_json_atomic(follower.state_path, {"since": 1000})

    result = follower.fetch_and_store()

    assert result["pages_fetched"] == 2
    assert result["gap"] is True  # surfaced even though it happened on page 2, not page 1

    gaps = read_jsonl(follower.gaps_path)
    assert len(gaps) == 1
    assert gaps[0]["expected_since"] == 1005  # page 1's advanced cursor, not the pass's start
    assert gaps[0]["first_seq"] == 1008

    stored = read_jsonl(follower.messages_path)
    seqs = [rec["seq"] for rec in stored]
    assert seqs == [1001, 1002, 1003, 1004, 1005, 1008, 1009, 1010]
