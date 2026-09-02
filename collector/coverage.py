"""Coverage accounting: how much of each room's message stream we actually
captured vs. how much the ring evicted before we ever read it.

Running per-room counters live in one small, atomically-written JSON file
(data/coverage_state.json). They're updated only after the page they
describe has already been durably committed -- its messages appended and
its cursor advanced on disk -- so a page is never counted twice: once a
room's cursor has moved past a page, that exact page is never re-fetched
with the same `since` again, and counting happens strictly after that
cursor move. The one edge a crash can cost is an under-count (the counter
update for the very last in-flight page never lands), never a double-count
-- which is the safe direction to be wrong in for a coverage number.
"""

import os
from datetime import datetime

from .storage import append_jsonl, load_json, save_json_atomic


def gap_dropped_count(expected_since, first_seq):
    """Exact number of evicted seqs for a gap.

    seq is contiguous per the protocol (the ring never skips a number, it
    only evicts a contiguous prefix), so the count of what's missing
    between our cursor and what the server actually handed back is exact
    arithmetic, not an estimate: the seqs strictly between expected_since
    and first_seq. Zero when there's no gap (first_seq <= expected_since+1)
    or when first_seq is unknown (an empty page).
    """
    if first_seq is None or first_seq <= expected_since + 1:
        return 0
    return first_seq - expected_since - 1


class CoverageTracker:
    """Owns data/coverage_state.json (running counters) and
    data/coverage.jsonl (the periodic report)."""

    def __init__(self, data_dir):
        self.state_path = os.path.join(data_dir, "coverage_state.json")
        self.report_path = os.path.join(data_dir, "coverage.jsonl")

    def _load(self):
        return load_json(self.state_path, default={})

    def record(self, room, captured_delta=0, dropped_delta=0):
        """Add to `room`'s running totals. A no-op call (both deltas zero)
        skips the read-modify-write entirely.
        """
        if not captured_delta and not dropped_delta:
            return
        state = self._load()
        entry = state.get(room, {"captured_total": 0, "dropped_total": 0})
        entry = {
            "captured_total": entry.get("captured_total", 0) + captured_delta,
            "dropped_total": entry.get("dropped_total", 0) + dropped_delta,
        }
        state[room] = entry
        save_json_atomic(self.state_path, state)

    def counters(self, room):
        entry = self._load().get(room)
        return dict(entry) if entry else {"captured_total": 0, "dropped_total": 0}

    @staticmethod
    def coverage_ratio(captured_total, dropped_total):
        denom = captured_total + dropped_total
        if denom == 0:
            return None
        return captured_total / denom

    def report(self, rooms, cursors, source, captured_at):
        """Append one coverage.jsonl record per room, plus a service-wide
        rollup (room="__all__") summed across them. Returns the records.
        """
        state = self._load()
        total_captured = 0
        total_dropped = 0
        records = []
        for room in rooms:
            entry = state.get(room, {"captured_total": 0, "dropped_total": 0})
            captured = entry.get("captured_total", 0)
            dropped = entry.get("dropped_total", 0)
            total_captured += captured
            total_dropped += dropped
            records.append(
                {
                    "room": room,
                    "captured_total": captured,
                    "dropped_total": dropped,
                    "coverage": self.coverage_ratio(captured, dropped),
                    "cursor": cursors.get(room),
                    "captured_at": captured_at,
                    "source": source,
                }
            )
        records.append(
            {
                "room": "__all__",
                "captured_total": total_captured,
                "dropped_total": total_dropped,
                "coverage": self.coverage_ratio(total_captured, total_dropped),
                "cursor": None,
                "captured_at": captured_at,
                "source": source,
            }
        )
        append_jsonl(self.report_path, records)
        return records


class UptimeTracker:
    """Session start/stop records (data/sessions.jsonl) and a periodic
    liveness heartbeat (data/uptime_state.json), so coverage can eventually
    tell "the collector was off" apart from "it was running but the ring
    outran it" -- today's coverage ratio only ever sees the latter, since
    it's only updated while a page is actively being fetched.

    Additive only: nothing here feeds into CoverageTracker's ratio yet.
    That's a deliberate follow-up, not an oversight -- see
    compute_downtime_intervals's docstring for why doing it well needs
    more than what a single overwritten heartbeat file can tell you.
    """

    def __init__(self, data_dir):
        self.sessions_path = os.path.join(data_dir, "sessions.jsonl")
        self.uptime_state_path = os.path.join(data_dir, "uptime_state.json")
        self.run_id = None

    def record_start(self, ts, pid=None):
        """Append a {"event": "start", ...} record and remember the run id
        (pid + this start's ts) so record_stop() can tag itself with the
        same one.
        """
        pid = os.getpid() if pid is None else pid
        self.run_id = f"{pid}-{ts}"
        append_jsonl(self.sessions_path, [{"event": "start", "ts": ts, "run_id": self.run_id}])
        return self.run_id

    def record_stop(self, ts):
        """Append a {"event": "stop", ...} record for the session
        record_start() began. Safe to call even if record_start() was
        never called in this process (run_id stays None) -- a stop record
        with no matching start is still informative, not a crash.
        """
        append_jsonl(self.sessions_path, [{"event": "stop", "ts": ts, "run_id": self.run_id}])

    def heartbeat(self, ts):
        """Atomically record `ts` as the last time we know the collector
        was alive. Called on the snapshot cadence (not every message
        tick) -- cheaper, and still frequent enough relative to a crash
        to be a useful lower bound on "when did it go quiet".
        """
        save_json_atomic(self.uptime_state_path, {"last_alive": ts})

    def last_alive(self):
        state = load_json(self.uptime_state_path, default={})
        return state.get("last_alive")


def _seconds_between(ts1, ts2):
    return (datetime.fromisoformat(ts2) - datetime.fromisoformat(ts1)).total_seconds()


def compute_downtime_intervals(sessions, last_alive=None):
    """Given data/sessions.jsonl's records (a chronological list of
    {"event": "start"|"stop", "ts": <iso8601>, ...} dicts) and the current
    data/uptime_state.json value (`last_alive`, the single most recent
    heartbeat on record, or None), return the downtime intervals: gaps
    where the collector was not running, as a list of
    {"gap_start", "gap_end", "seconds"} dicts.

    For a session that shut down cleanly (a "stop" record appears before
    the next "start"), the gap to that next start runs from the stop's
    own ts -- exact. For a session with NO "stop" record before the next
    "start" (it crashed, or was killed harder than SIGTERM), there is no
    persisted record of exactly when it went quiet UNLESS this is the
    single most recent such gap -- the one immediately preceding the
    latest session -- in which case `last_alive` (read from
    uptime_state.json by the new session, before its own first heartbeat
    overwrites that file) is the only surviving signal of when the
    previous session was last seen, and is used as the gap's start.

    Older crashes further back in history get no such treatment: because
    uptime_state.json holds only ONE value, overwritten on every
    heartbeat, a heartbeat from three sessions ago is gone by the time a
    fourth session starts. Rather than fabricate a number for those, this
    function reports nothing for that pair -- silence, not a guess. This
    is exactly why it isn't wired into the coverage ratio yet: a metric
    that quietly under-reports old downtime without saying so would be
    worse than not having it.
    """
    start_indices = [i for i, r in enumerate(sessions) if r.get("event") == "start"]
    if len(start_indices) < 2:
        return []  # need at least two sessions to have a gap between them

    intervals = []
    last_pair = len(start_indices) - 2
    for k in range(len(start_indices) - 1):
        this_idx = start_indices[k]
        next_idx = start_indices[k + 1]
        next_start_ts = sessions[next_idx]["ts"]
        between = sessions[this_idx + 1 : next_idx]
        stop_rec = next((r for r in between if r.get("event") == "stop"), None)

        if stop_rec is not None:
            gap_start_ts = stop_rec["ts"]
        elif k == last_pair and last_alive is not None:
            gap_start_ts = last_alive
        else:
            continue  # no persisted signal for this older gap; say nothing rather than guess

        seconds = _seconds_between(gap_start_ts, next_start_ts)
        if seconds > 0:
            intervals.append(
                {"gap_start": gap_start_ts, "gap_end": next_start_ts, "seconds": seconds}
            )
    return intervals
