"""Collector.record_coverage() emits one coverage.jsonl record per followed
room plus a service-wide "__all__" rollup, and run_once()/the snapshot
cadence in run_loop() both trigger it.
"""

from helpers import FIXTURE_DID_1, FakeClient

from collector.config import Config
from collector.core import Collector
from collector.storage import read_jsonl


def _page(room, seqs):
    return {
        "room": room,
        "count": len(seqs),
        "first_seq": seqs[0] if seqs else None,
        "last_seq": seqs[-1] if seqs else 0,
        "generation": 0,
        "messages": [
            {
                "seq": s,
                "ts": "t",
                "from": FIXTURE_DID_1,
                "text": f"m{s}",
                "nonce": s,
                "sig": "S",
            }
            for s in seqs
        ],
    }


def test_run_once_emits_a_coverage_record_per_room_plus_rollup(tmp_path):
    client = FakeClient(
        rooms_overview={"rooms": [], "total": 0},
        room_pages={"events": _page("events", [1, 2]), "lobby": _page("lobby", [1, 2, 3])},
    )
    config = Config(data_dir=str(tmp_path), rooms=["lobby"])
    collector = Collector(client, config)

    collector.run_once(wait=0)

    records = read_jsonl(collector.coverage_tracker.report_path)
    assert len(records) == 3  # events, lobby, __all__

    by_room = {r["room"]: r for r in records}
    assert by_room["events"]["captured_total"] == 2
    assert by_room["events"]["dropped_total"] == 0
    assert by_room["events"]["coverage"] == 1.0
    assert by_room["events"]["cursor"] == 2

    assert by_room["lobby"]["captured_total"] == 3
    assert by_room["lobby"]["coverage"] == 1.0
    assert by_room["lobby"]["cursor"] == 3

    rollup = by_room["__all__"]
    assert rollup["captured_total"] == 5
    assert rollup["dropped_total"] == 0
    assert rollup["coverage"] == 1.0
    assert rollup["cursor"] is None


def test_coverage_report_reflects_dropped_messages_in_rollup(tmp_path):
    gap_page = {
        "room": "lobby",
        "count": 1,
        "first_seq": 110,  # since=100 -> 9 dropped (101..109)
        "last_seq": 110,
        "generation": 0,
        "messages": [
            {"seq": 110, "ts": "t", "from": FIXTURE_DID_1, "text": "x", "nonce": 1, "sig": "S"},
        ],
    }
    client = FakeClient(
        rooms_overview={"rooms": [], "total": 0},
        room_pages={"events": _page("events", []), "lobby": gap_page},
    )
    config = Config(data_dir=str(tmp_path), rooms=["lobby"])
    collector = Collector(client, config)

    from collector.storage import save_json_atomic

    save_json_atomic(collector.room_followers[0].state_path, {"since": 100})

    collector.run_once(wait=0)

    records = read_jsonl(collector.coverage_tracker.report_path)
    by_room = {r["room"]: r for r in records}
    assert by_room["lobby"]["captured_total"] == 1
    assert by_room["lobby"]["dropped_total"] == 9
    assert by_room["lobby"]["coverage"] == 1 / 10

    assert by_room["__all__"]["dropped_total"] == 9


def test_snapshot_cadence_in_run_loop_also_emits_coverage(tmp_path):
    client = FakeClient(
        rooms_overview={"rooms": [], "total": 0},
        room_pages={"events": _page("events", []), "lobby": _page("lobby", [])},
    )
    config = Config(
        data_dir=str(tmp_path), rooms=["lobby"], message_interval=1000, snapshot_interval=1000
    )
    collector = Collector(client, config)

    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, s):
            self.now += s

    clock = Clock()
    # Both cadences due immediately at t=0 -> exactly one snapshot cycle.
    collector.run_loop(stop_after=1, sleep_fn=clock.sleep, monotonic_fn=clock.monotonic)

    records = read_jsonl(collector.coverage_tracker.report_path)
    assert len(records) == 3  # one report, 2 rooms + rollup
