"""Uptime tracking: session start/stop records, the periodic liveness
heartbeat, and the pure downtime-interval helper that reads them back.
Additive only -- none of this feeds into CoverageTracker's ratio yet.
"""

from datetime import datetime, timedelta

from helpers import FakeClient

from collector.config import Config
from collector.core import Collector
from collector.coverage import UptimeTracker, compute_downtime_intervals
from collector.storage import load_json, read_jsonl


def _empty_page(room):
    return {"room": room, "count": 0, "first_seq": None, "last_seq": 0, "generation": 0, "messages": []}


class FakeClock:
    """Same pattern as test_cadence.py's: sleeping is what advances the
    monotonic clock, so a multi-tick run costs no real wall time.
    """

    def __init__(self, start=0.0):
        self.now = start

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeNow:
    """A deterministic wall-clock source: each call advances by a fixed
    step and returns an ISO8601 string, so timestamp ordering in tests is
    exact and reproducible rather than dependent on real elapsed time.
    """

    def __init__(self, start="2026-01-01T00:00:00+00:00", step_seconds=1.0):
        self._t = datetime.fromisoformat(start)
        self._step = timedelta(seconds=step_seconds)

    def __call__(self):
        self._t += self._step
        return self._t.isoformat()


def _make_collector(tmp_path, message_interval, snapshot_interval):
    client = FakeClient(
        rooms_overview={"rooms": [], "total": 0},
        room_pages={"events": _empty_page("events"), "lobby": _empty_page("lobby")},
    )
    config = Config(
        data_dir=str(tmp_path),
        rooms=["lobby"],
        message_interval=message_interval,
        snapshot_interval=snapshot_interval,
    )
    return Collector(client, config)


# --- UptimeTracker: the plumbing -----------------------------------------


def test_record_start_writes_a_start_event_with_pid_and_ts(tmp_path):
    tracker = UptimeTracker(str(tmp_path))
    run_id = tracker.record_start("2026-01-01T00:00:00+00:00", pid=12345)

    assert run_id == "12345-2026-01-01T00:00:00+00:00"
    sessions = read_jsonl(tracker.sessions_path)
    assert sessions == [{"event": "start", "ts": "2026-01-01T00:00:00+00:00", "run_id": run_id}]


def test_record_stop_reuses_the_run_id_from_record_start(tmp_path):
    tracker = UptimeTracker(str(tmp_path))
    run_id = tracker.record_start("2026-01-01T00:00:00+00:00", pid=999)
    tracker.record_stop("2026-01-01T00:05:00+00:00")

    sessions = read_jsonl(tracker.sessions_path)
    assert sessions == [
        {"event": "start", "ts": "2026-01-01T00:00:00+00:00", "run_id": run_id},
        {"event": "stop", "ts": "2026-01-01T00:05:00+00:00", "run_id": run_id},
    ]


def test_heartbeat_is_atomic_and_overwrites_in_place(tmp_path):
    tracker = UptimeTracker(str(tmp_path))
    tracker.heartbeat("2026-01-01T00:00:00+00:00")
    assert load_json(tracker.uptime_state_path) == {"last_alive": "2026-01-01T00:00:00+00:00"}

    tracker.heartbeat("2026-01-01T00:05:00+00:00")
    assert load_json(tracker.uptime_state_path) == {"last_alive": "2026-01-01T00:05:00+00:00"}
    assert tracker.last_alive() == "2026-01-01T00:05:00+00:00"


# --- compute_downtime_intervals: the pure helper --------------------------


def test_no_intervals_with_fewer_than_two_sessions():
    assert compute_downtime_intervals([]) == []
    assert compute_downtime_intervals([{"event": "start", "ts": "2026-01-01T00:00:00+00:00"}]) == []


def test_clean_stop_then_start_gives_an_exact_interval():
    sessions = [
        {"event": "start", "ts": "2026-01-01T00:00:00+00:00"},
        {"event": "stop", "ts": "2026-01-01T00:10:00+00:00"},
        {"event": "start", "ts": "2026-01-01T00:12:30+00:00"},
    ]
    intervals = compute_downtime_intervals(sessions)
    assert intervals == [
        {
            "gap_start": "2026-01-01T00:10:00+00:00",
            "gap_end": "2026-01-01T00:12:30+00:00",
            "seconds": 150.0,
        }
    ]


def test_multiple_clean_sessions_give_one_interval_each():
    sessions = [
        {"event": "start", "ts": "2026-01-01T00:00:00+00:00"},
        {"event": "stop", "ts": "2026-01-01T00:10:00+00:00"},
        {"event": "start", "ts": "2026-01-01T00:11:00+00:00"},
        {"event": "stop", "ts": "2026-01-01T00:20:00+00:00"},
        {"event": "start", "ts": "2026-01-01T00:25:00+00:00"},
    ]
    intervals = compute_downtime_intervals(sessions)
    assert len(intervals) == 2
    assert intervals[0]["seconds"] == 60.0  # 00:10:00 -> 00:11:00
    assert intervals[1]["seconds"] == 300.0  # 00:20:00 -> 00:25:00


def test_crash_before_the_latest_session_uses_last_alive():
    # No "stop" between the two starts (a crash) -- last_alive is the
    # only surviving signal of when that session was last seen.
    sessions = [
        {"event": "start", "ts": "2026-01-01T00:00:00+00:00"},
        {"event": "start", "ts": "2026-01-01T01:00:00+00:00"},
    ]
    intervals = compute_downtime_intervals(sessions, last_alive="2026-01-01T00:05:00+00:00")
    assert intervals == [
        {
            "gap_start": "2026-01-01T00:05:00+00:00",
            "gap_end": "2026-01-01T01:00:00+00:00",
            "seconds": 3300.0,  # 00:05:00 -> 01:00:00
        }
    ]


def test_crash_before_the_latest_session_with_no_last_alive_reports_nothing():
    sessions = [
        {"event": "start", "ts": "2026-01-01T00:00:00+00:00"},
        {"event": "start", "ts": "2026-01-01T01:00:00+00:00"},
    ]
    assert compute_downtime_intervals(sessions, last_alive=None) == []


def test_older_crash_further_back_reports_nothing_not_a_fabricated_number():
    # Three sessions, the FIRST->SECOND gap is a crash with no stop record
    # and it is NOT the most recent gap (a third session follows) -- no
    # persisted heartbeat survives for it, so it must be silently skipped,
    # never guessed at. The SECOND->THIRD gap is clean and still reported.
    sessions = [
        {"event": "start", "ts": "2026-01-01T00:00:00+00:00"},
        {"event": "start", "ts": "2026-01-01T01:00:00+00:00"},  # crash, no stop -- older gap
        {"event": "stop", "ts": "2026-01-01T02:00:00+00:00"},
        {"event": "start", "ts": "2026-01-01T02:05:00+00:00"},  # clean gap, most recent
    ]
    intervals = compute_downtime_intervals(sessions, last_alive="2026-01-01T00:30:00+00:00")
    assert len(intervals) == 1  # only the clean, most-recent-eligible gap
    assert intervals[0]["gap_start"] == "2026-01-01T02:00:00+00:00"
    assert intervals[0]["gap_end"] == "2026-01-01T02:05:00+00:00"


def test_non_positive_gap_is_not_reported():
    # A malformed/out-of-order pair (stop before start, or clocks that
    # didn't advance) must not produce a negative or zero "downtime".
    sessions = [
        {"event": "start", "ts": "2026-01-01T00:10:00+00:00"},
        {"event": "stop", "ts": "2026-01-01T00:05:00+00:00"},  # before its own start -- bogus
        {"event": "start", "ts": "2026-01-01T00:05:00+00:00"},
    ]
    assert compute_downtime_intervals(sessions) == []


# --- run_loop integration: start record + heartbeat advancing -----------


def test_run_loop_writes_a_session_start_record_on_start(tmp_path):
    collector = _make_collector(tmp_path, message_interval=1000, snapshot_interval=1000)
    clock = FakeClock()
    now = FakeNow()

    collector.run_loop(stop_after=1, sleep_fn=clock.sleep, monotonic_fn=clock.monotonic, now_fn=now)

    sessions = read_jsonl(collector.uptime_tracker.sessions_path)
    assert len(sessions) == 1
    assert sessions[0]["event"] == "start"
    assert "run_id" in sessions[0] and sessions[0]["run_id"]


def test_run_loop_does_not_write_a_session_record_for_run_once(tmp_path):
    client = FakeClient(
        rooms_overview={"rooms": [], "total": 0},
        room_pages={"events": _empty_page("events"), "lobby": _empty_page("lobby")},
    )
    config = Config(data_dir=str(tmp_path), rooms=["lobby"])
    collector = Collector(client, config)

    collector.run_once(wait=0)

    import os

    assert not os.path.exists(collector.uptime_tracker.sessions_path)


def test_last_alive_advances_across_snapshot_cadence_ticks(tmp_path):
    collector = _make_collector(tmp_path, message_interval=1000, snapshot_interval=5)
    clock = FakeClock()
    now = FakeNow(step_seconds=1.0)

    # Both cadences due at t=0 (first snapshot + heartbeat); the loop's
    # sleep is capped at 1s, so ~5 more ticks reach the next snapshot due
    # at t=5. A little headroom for the exact tick boundary.
    collector.run_loop(stop_after=8, sleep_fn=clock.sleep, monotonic_fn=clock.monotonic, now_fn=now)

    uptime_state = load_json(collector.uptime_tracker.uptime_state_path)
    assert uptime_state is not None
    first_last_alive = uptime_state["last_alive"]

    # Run further ticks to cover a second snapshot cycle and confirm the
    # heartbeat moved forward, not just fired once.
    collector.run_loop(stop_after=8, sleep_fn=clock.sleep, monotonic_fn=clock.monotonic, now_fn=now)
    second_last_alive = load_json(collector.uptime_tracker.uptime_state_path)["last_alive"]

    assert second_last_alive > first_last_alive
