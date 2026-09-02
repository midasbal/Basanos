"""Gap detection when first_seq > since+1 (synthetic fixture)."""

from helpers import FIXTURE_DID_1, FakeClient, load_fixture

from collector.core import RoomFollower
from collector.storage import read_jsonl, save_json_atomic


def test_gap_is_detected_and_recorded(tmp_path):
    gap_page = load_fixture("gap_page.fixture.json")
    client = FakeClient(room_pages={"lobby": gap_page})
    follower = RoomFollower(client, str(tmp_path), "lobby", source="test")

    # Pretend a previous pass had already advanced the cursor to 100.
    save_json_atomic(follower.state_path, {"since": 100})

    result = follower.fetch_and_store()

    assert result["gap"] is True
    assert result["since_before"] == 100
    # gap_page.fixture.json has first_seq=150 > 100+1
    assert gap_page["first_seq"] > 101

    gaps = read_jsonl(follower.gaps_path)
    assert len(gaps) == 1
    assert gaps[0]["room"] == "lobby"
    assert gaps[0]["expected_since"] == 100
    assert gaps[0]["first_seq"] == gap_page["first_seq"]
    assert gaps[0]["last_seq"] == gap_page["last_seq"]

    # messages themselves are still stored despite the gap
    stored = read_jsonl(follower.messages_path)
    assert [rec["seq"] for rec in stored] == [150, 151, 152]

    assert result["since_after"] == 152


def test_no_gap_when_contiguous(tmp_path):
    contiguous_page = {
        "room": "lobby",
        "count": 2,
        "first_seq": 101,
        "last_seq": 102,
        "generation": 0,
        "messages": [
            {
                "seq": 101,
                "ts": "2026-09-01T23:00:00.000000Z",
                "from": FIXTURE_DID_1,
                "text": "no gap here",
                "nonce": 1,
                "sig": "SYNTHETIC-NOT-REAL",
            },
            {
                "seq": 102,
                "ts": "2026-09-01T23:00:01.000000Z",
                "from": FIXTURE_DID_1,
                "text": "still contiguous",
                "nonce": 2,
                "sig": "SYNTHETIC-NOT-REAL",
            },
        ],
    }
    client = FakeClient(room_pages={"lobby": contiguous_page})
    follower = RoomFollower(client, str(tmp_path), "lobby", source="test")
    save_json_atomic(follower.state_path, {"since": 100})

    result = follower.fetch_and_store()

    assert result["gap"] is False
    assert read_jsonl(follower.gaps_path) == []
