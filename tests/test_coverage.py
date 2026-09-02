"""Coverage accounting: exact gap-size arithmetic, coverage ratio
(including the null-on-zero-denominator case), and crash-safe running
counters that don't double-count across a simulated restart.
"""

from helpers import FIXTURE_DID_1, FakeClient

from collector.core import RoomFollower
from collector.coverage import CoverageTracker, gap_dropped_count
from collector.storage import read_jsonl, save_json_atomic


# --- gap size arithmetic ----------------------------------------------


def test_gap_dropped_count_contiguous_range():
    # expected_since=100, first_seq=150 -> seqs 101..149 were evicted = 49
    assert gap_dropped_count(100, 150) == 49


def test_gap_dropped_count_zero_when_contiguous():
    assert gap_dropped_count(100, 101) == 0  # no gap at all


def test_gap_dropped_count_zero_when_first_seq_none():
    assert gap_dropped_count(100, None) == 0


def test_gap_dropped_count_off_by_one_boundary():
    # first_seq == since+1 is the contiguous boundary, not a gap
    assert gap_dropped_count(100, 101) == 0
    # first_seq == since+2 means exactly one seq (101) was dropped
    assert gap_dropped_count(100, 102) == 1


# --- coverage ratio ------------------------------------------------------


def test_coverage_ratio_normal_case():
    assert CoverageTracker.coverage_ratio(90, 10) == 0.9


def test_coverage_ratio_perfect_capture():
    assert CoverageTracker.coverage_ratio(50, 0) == 1.0


def test_coverage_ratio_null_when_denominator_zero():
    assert CoverageTracker.coverage_ratio(0, 0) is None


# --- running counters: persist, resume, no double-count -----------------


def test_counters_accumulate_from_captures_and_gaps(tmp_path):
    tracker = CoverageTracker(str(tmp_path))
    tracker.record("lobby", captured_delta=5, dropped_delta=0)
    tracker.record("lobby", captured_delta=3, dropped_delta=2)
    assert tracker.counters("lobby") == {"captured_total": 8, "dropped_total": 2}


def test_counters_are_per_room(tmp_path):
    tracker = CoverageTracker(str(tmp_path))
    tracker.record("lobby", captured_delta=5)
    tracker.record("meta", captured_delta=1, dropped_delta=1)
    assert tracker.counters("lobby") == {"captured_total": 5, "dropped_total": 0}
    assert tracker.counters("meta") == {"captured_total": 1, "dropped_total": 1}


def test_counters_survive_a_simulated_restart_without_double_counting(tmp_path):
    """Drain some pages, throw away the in-memory follower (simulating a
    process restart), build a brand new follower against the same
    data_dir, drain more pages -- the counters must reflect exactly the
    union of both, never re-counting the pre-restart pages.
    """
    page1 = {
        "room": "lobby",
        "count": 2,
        "first_seq": 1,
        "last_seq": 2,
        "generation": 0,
        "messages": [
            {"seq": 1, "ts": "t", "from": FIXTURE_DID_1, "text": "a", "nonce": 1, "sig": "S"},
            {"seq": 2, "ts": "t", "from": FIXTURE_DID_1, "text": "b", "nonce": 2, "sig": "S"},
        ],
    }
    client1 = FakeClient(room_pages={"lobby": page1})
    follower1 = RoomFollower(client1, str(tmp_path), "lobby", source="test")
    result1 = follower1.fetch_and_store()
    assert result1["new_count"] == 2

    tracker = CoverageTracker(str(tmp_path))
    assert tracker.counters("lobby") == {"captured_total": 2, "dropped_total": 0}

    # "Restart": a fresh RoomFollower and CoverageTracker over the same
    # data_dir, as if the process had been killed and relaunched. It must
    # resume from the persisted cursor (since=2), not reprocess page1.
    page2_with_gap = {
        "room": "lobby",
        "count": 1,
        "first_seq": 5,  # since+1 would be 3 -- seqs 3,4 were evicted (gap=2)
        "last_seq": 5,
        "generation": 0,
        "messages": [
            {"seq": 5, "ts": "t", "from": FIXTURE_DID_1, "text": "c", "nonce": 5, "sig": "S"},
        ],
    }
    client2 = FakeClient(room_pages={"lobby": page2_with_gap})
    follower2 = RoomFollower(client2, str(tmp_path), "lobby", source="test")  # fresh instance
    result2 = follower2.fetch_and_store()

    assert result2["since_before"] == 2  # resumed from disk, not from 0
    assert result2["new_count"] == 1
    assert result2["gap"] is True

    tracker2 = CoverageTracker(str(tmp_path))  # also fresh
    counters = tracker2.counters("lobby")
    # 2 captured before "restart" + 1 after = 3. 0 dropped before + 2
    # dropped (gap_dropped_count(2, 5) == 2) after = 2. Not doubled.
    assert counters == {"captured_total": 3, "dropped_total": 2}

    # Re-running fetch_and_store again with nothing new available must not
    # add anything further (the ordinary "caught up, no page fetched
    # duplicate" case, exercised here as an extra double-count guard).
    client3 = FakeClient(
        room_pages={
            "lobby": {"room": "lobby", "count": 0, "first_seq": None, "last_seq": 5, "generation": 0, "messages": []}
        }
    )
    follower3 = RoomFollower(client3, str(tmp_path), "lobby", source="test")
    follower3.fetch_and_store()
    assert CoverageTracker(str(tmp_path)).counters("lobby") == {"captured_total": 3, "dropped_total": 2}


def test_gap_record_carries_exact_dropped_count(tmp_path):
    page_with_gap = {
        "room": "lobby",
        "count": 1,
        "first_seq": 150,
        "last_seq": 150,
        "generation": 0,
        "messages": [
            {"seq": 150, "ts": "t", "from": FIXTURE_DID_1, "text": "x", "nonce": 1, "sig": "S"},
        ],
    }
    client = FakeClient(room_pages={"lobby": page_with_gap})
    follower = RoomFollower(client, str(tmp_path), "lobby", source="test")
    save_json_atomic(follower.state_path, {"since": 100})

    follower.fetch_and_store()

    gaps = read_jsonl(follower.gaps_path)
    assert len(gaps) == 1
    assert gaps[0]["dropped"] == 49  # first_seq(150) - expected_since(100) - 1
