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
