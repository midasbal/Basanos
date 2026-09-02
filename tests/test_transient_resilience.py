"""Transient-failure resilience: a room/snapshot that keeps failing (503s,
timeouts, connection errors) must not advance its cursor, must record a
failure distinct from a gap record, and must never stop the rest of the
collection pass.
"""

from helpers import AlwaysFailingClient

from collector.core import Collector, RoomFollower, RoomsSnapshotter
from collector.config import Config
from collector.http_client import TransientFetchError
from collector.storage import load_json, read_jsonl, save_json_atomic


def _exc():
    return TransientFetchError("GET .../r/lobby failed after 5 attempts (HTTP 503)")


def test_follower_does_not_advance_cursor_on_persistent_failure(tmp_path):
    client = AlwaysFailingClient(_exc())
    follower = RoomFollower(client, str(tmp_path), "lobby", source="test")
    save_json_atomic(follower.state_path, {"since": 500})

    result = follower.fetch_and_store()

    assert result["failed"] is True
    assert "503" in result["error"]
    assert result["since_before"] == 500
    assert result["since_after"] == 500  # unchanged
    assert result["new_count"] == 0
    assert result["pages_fetched"] == 0

    # cursor on disk is untouched
    assert load_json(follower.state_path) == {"since": 500}

    # no messages, no gap -- a failure is not a gap
    assert read_jsonl(follower.messages_path) == []
    assert read_jsonl(follower.gaps_path) == []


def test_failure_record_is_distinct_from_a_gap_record(tmp_path):
    client = AlwaysFailingClient(_exc())
    follower = RoomFollower(client, str(tmp_path), "lobby", source="test")
    save_json_atomic(follower.state_path, {"since": 500})

    follower.fetch_and_store()

    failures = read_jsonl(follower.failures_path)
    assert len(failures) == 1
    assert failures[0]["target"] == "room:lobby"
    assert failures[0]["room"] == "lobby"
    assert failures[0]["since"] == 500
    assert "error" in failures[0]

    # A gap record means "the ring dropped data"; a failure means "we
    # couldn't read". Different files, never conflated.
    assert read_jsonl(follower.gaps_path) == []


def test_snapshotter_records_failure_without_raising(tmp_path):
    client = AlwaysFailingClient(_exc())
    snapshotter = RoomsSnapshotter(client, str(tmp_path), source="test")

    result = snapshotter.snapshot()

    assert result["failed"] is True
    assert result["record"] is None
    failures = read_jsonl(snapshotter.failures_path)
    assert len(failures) == 1
    assert failures[0]["target"] == "snapshot"
    assert failures[0]["room"] is None


def test_one_failing_room_does_not_block_the_rest_of_the_pass(tmp_path):
    """A room stuck on 503s must not crash run_once() or stop the snapshot,
    the events log, or any other configured room from being collected.
    """

    class MixedClient:
        """rooms overview + events succeed; the "lobby" room always fails."""

        def get_rooms_overview(self, timeout=None, max_attempts=None, backoff_cap=None):
            return {"rooms": [], "total": 0}

        def get_room_page(
            self, room, since=0, wait=0, limit=None, timeout=None, max_attempts=None, backoff_cap=None
        ):
            if room == "lobby":
                raise _exc()
            return {
                "room": room,
                "count": 1,
                "first_seq": since + 1,
                "last_seq": since + 1,
                "generation": 0,
                "messages": [
                    {
                        "seq": since + 1,
                        "ts": "2026-09-02T00:00:00.000000Z",
                        "from": "server" if room == "events" else "nick",
                        "text": "ok",
                    }
                ],
            }

    config = Config(data_dir=str(tmp_path), rooms=["lobby", "meta"])
    collector = Collector(MixedClient(), config)

    results = collector.run_once(wait=0)  # must not raise

    assert results["snapshot"]["failed"] is False
    assert results["events"]["failed"] is False
    lobby_result = next(r for r in results["rooms"] if r["room"] == "lobby")
    meta_result = next(r for r in results["rooms"] if r["room"] == "meta")
    assert lobby_result["failed"] is True
    assert lobby_result["new_count"] == 0
    assert meta_result["failed"] is False
    assert meta_result["new_count"] == 1
