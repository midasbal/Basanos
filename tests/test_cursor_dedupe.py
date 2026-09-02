"""Cursor advancement and (room, seq) dedupe across two successive passes."""

from helpers import FIXTURE_DID_1, FakeClient

from collector.core import RoomFollower
from collector.storage import read_jsonl


def _page(first_seq, last_seq, seqs):
    return {
        "room": "lobby",
        "count": len(seqs),
        "first_seq": first_seq,
        "last_seq": last_seq,
        "generation": 0,
        "messages": [
            {
                "seq": s,
                "ts": f"2026-09-01T23:0{s}:00.000000Z",
                "from": FIXTURE_DID_1,
                "text": f"message {s}",
                "nonce": 1000 + s,
                "sig": "SYNTHETIC-NOT-REAL",
            }
            for s in seqs
        ],
    }


def test_two_passes_advance_cursor_and_dedupe(tmp_path):
    # Pass 1: seq 10,11,12 are new. Pass 2 simulates an overlapping refetch
    # (server hands back seq 12 again alongside genuinely new 13,14) --
    # the follower must not double-store seq 12.
    pages = [
        _page(first_seq=10, last_seq=12, seqs=[10, 11, 12]),
        _page(first_seq=12, last_seq=14, seqs=[12, 13, 14]),
    ]
    client = FakeClient(room_pages={"lobby": pages})
    follower = RoomFollower(client, str(tmp_path), "lobby", source="test")

    result1 = follower.fetch_and_store()
    assert result1["since_before"] == 0
    assert result1["since_after"] == 12
    assert result1["new_count"] == 3
    assert result1["gap"] is False

    result2 = follower.fetch_and_store()
    assert result2["since_before"] == 12
    assert result2["since_after"] == 14
    assert result2["new_count"] == 2  # only 13, 14 -- 12 deduped
    assert result2["gap"] is False

    # requested since= reflects the persisted cursor each time; both pages
    # are short of the 200 page_limit so each pass stops after one page
    assert client.calls == [
        ("room_page", "lobby", 0, 0, 200),
        ("room_page", "lobby", 12, 0, 200),
    ]

    stored = read_jsonl(follower.messages_path)
    seqs = [rec["seq"] for rec in stored]
    assert seqs == [10, 11, 12, 13, 14]
    assert len(seqs) == len(set(seqs))  # no duplicate (room, seq)
    assert all(rec["room"] == "lobby" for rec in stored)
