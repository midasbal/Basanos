"""Collection logic: whole-commons snapshots and per-room/event-log followers.

Everything here is read-only and append-only. No message is ever mutated or
deleted once written; cursors only ever move forward, and only ever past a
page that was actually stored.
"""

import os
import time
from datetime import datetime, timezone

from .coverage import CoverageTracker, gap_dropped_count
from .http_client import TransientFetchError
from .storage import append_jsonl, load_json, save_json_atomic

# The service-wide discovery log is followed with a cursor exactly like any
# room (see collector.http_client.TechnocoreClient.get_room_page), so it
# gets a room-shaped directory under data/rooms/events/ too.
EVENTS_ROOM = "events"

# Confirmed live against /r/lobby (2026-09-02): since=<seq>&limit=200&
# format=json returns at most 200 messages, oldest-first, contiguous from
# first_seq == since+1 when the ring hasn't dropped anything underneath us.
# A page shorter than the limit means we've caught up.
DEFAULT_PAGE_LIMIT = 200

# Safety valve on the drain loop: caps how many pages one fetch_and_store()
# call will fetch for a single room, so a pathological backlog (or a bug)
# can't turn one pass into an unbounded, rate-limit-busting loop. At 200
# msgs/page this is up to 5000 messages per room per pass -- generous for
# any room this collector is likely to be pointed at, while still leaving
# read budget for the other rooms/snapshot/events in the same pass.
DEFAULT_MAX_PAGES_PER_DRAIN = 25


def utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


class RoomsSnapshotter:
    """Captures GET /rooms?format=json as a timestamped time series."""

    def __init__(self, client, data_dir, source):
        self.client = client
        self.path = os.path.join(data_dir, "rooms_snapshots.jsonl")
        self.failures_path = os.path.join(data_dir, "failures.jsonl")
        self.source = source

    def snapshot(self, now=None):
        captured_at = now or utcnow_iso()
        try:
            payload = self.client.get_rooms_overview()
        except TransientFetchError as exc:
            append_jsonl(
                self.failures_path,
                [
                    {
                        "target": "snapshot",
                        "room": None,
                        "error": str(exc),
                        "captured_at": captured_at,
                        "source": self.source,
                    }
                ],
            )
            return {"record": None, "failed": True, "error": str(exc)}

        rec = {
            "captured_at": captured_at,
            "source": self.source,
            "payload": payload,
        }
        append_jsonl(self.path, [rec])
        return {"record": rec, "failed": False, "error": None}


def detect_gap(since, first_seq, count):
    """True if the ring buffer dropped messages between our cursor and the
    first message the server actually returned.

    Per the endpoint contract: since=<seq> returns messages newer than
    <seq>; if the response's first_seq is greater than since+1, we missed a
    range. Only meaningful once we've actually established a cursor
    (since > 0) and the page has messages -- on the very first fetch
    (since == 0) a first_seq beyond 1 just means the room already had
    history before we started following it, not that we lost anything
    while watching.
    """
    if not count or since <= 0:
        return False
    if first_seq is None:
        return False
    return first_seq > since + 1


class RoomFollower:
    """Follows one room (or the /r/events discovery log) with a persisted
    `since` cursor, appending every new message and deduping by (room, seq).

    A single fetch_and_store() pass drains the room: it keeps fetching
    limit-sized pages and advancing the cursor, one stored page at a time,
    until a page comes back shorter than the limit (caught up), the
    per-pass page cap is hit (recorded, not hidden), or a page fetch fails
    (recorded as a failure; the cursor stays at the last page that was
    actually stored).
    """

    def __init__(
        self,
        client,
        data_dir,
        room,
        source,
        page_limit=DEFAULT_PAGE_LIMIT,
        max_pages_per_drain=DEFAULT_MAX_PAGES_PER_DRAIN,
        coverage_tracker=None,
    ):
        self.client = client
        self.room = room
        self.source = source
        self.page_limit = page_limit
        self.max_pages_per_drain = max_pages_per_drain
        self.coverage_tracker = coverage_tracker or CoverageTracker(data_dir)
        room_dir = os.path.join(data_dir, "rooms", room)
        self.messages_path = os.path.join(room_dir, "messages.jsonl")
        self.state_path = os.path.join(room_dir, "state.json")
        self.gaps_path = os.path.join(data_dir, "gaps.jsonl")
        self.drain_caps_path = os.path.join(data_dir, "drain_caps.jsonl")
        self.failures_path = os.path.join(data_dir, "failures.jsonl")

    def _load_since(self):
        state = load_json(self.state_path, default={"since": 0})
        return state.get("since", 0)

    def _store_page(self, page, since, captured_at):
        """Apply one already-fetched page: gap check, append new messages
        (deduped, nonce stringified), persist the advanced cursor.

        Returns (new_record_count, gap_bool, new_since, page_count).
        """
        messages = page.get("messages", [])
        count = page.get("count", len(messages))
        first_seq = page.get("first_seq")
        last_seq = page.get("last_seq")

        gap = detect_gap(since, first_seq, count)
        # Same eligibility as detect_gap: a since==0 or empty page never
        # counts as dropped, for the identical reason (see detect_gap).
        dropped = gap_dropped_count(since, first_seq) if (since > 0 and count) else 0
        if gap:
            append_jsonl(
                self.gaps_path,
                [
                    {
                        "room": self.room,
                        "expected_since": since,
                        "first_seq": first_seq,
                        "last_seq": last_seq,
                        "dropped": dropped,
                        "generation": page.get("generation"),
                        "captured_at": captured_at,
                        "source": self.source,
                    }
                ],
            )

        new_records = []
        max_seq_seen = since
        for m in messages:
            seq = m["seq"]
            if seq <= since:
                continue  # already stored this seq in a prior pass/page
            nonce = m.get("nonce")
            new_records.append(
                {
                    "room": self.room,
                    "seq": seq,
                    "ts": m.get("ts"),
                    "from": m.get("from"),
                    "text": m.get("text"),
                    # Stored as a string, exact digits preserved. Nonces run
                    # up to 19 digits (past 2^53); a JS frontend's
                    # JSON.parse silently rounds an integer that large,
                    # which would quietly break re-verification for those
                    # records. f"{room}|{nonce}|{text}" is byte-identical
                    # whether `nonce` is the int or str(int) -- Python ints
                    # are arbitrary precision, so nothing is lost here
                    # either.
                    "nonce": str(nonce) if nonce is not None else None,
                    "sig": m.get("sig"),
                    "captured_at": captured_at,
                    "source": self.source,
                }
            )
            if seq > max_seq_seen:
                max_seq_seen = seq
        append_jsonl(self.messages_path, new_records)

        new_since = last_seq if last_seq is not None else max_seq_seen
        if new_since < since:
            new_since = since  # cursors only ever move forward
        save_json_atomic(self.state_path, {"since": new_since})

        # Coverage counters are updated strictly AFTER the cursor above is
        # durably on disk. That ordering is what makes a crash here cost at
        # worst an under-count of this one page (never a double-count on
        # restart): once the cursor has moved past a page, that exact page
        # is never fetched again, so this call never runs twice for it.
        self.coverage_tracker.record(
            self.room, captured_delta=len(new_records), dropped_delta=dropped
        )

        return len(new_records), gap, new_since, count

    def fetch_and_store(self, wait=0, now=None):
        since = self._load_since()
        since_before = since
        total_new = 0
        any_gap = False
        pages_fetched = 0
        capped = False
        failed = False
        error = None

        current_since = since
        while True:
            captured_at = now or utcnow_iso()
            page_wait = wait if pages_fetched == 0 else 0  # long-poll only the first page
            try:
                page = self.client.get_room_page(
                    self.room, since=current_since, wait=page_wait, limit=self.page_limit
                )
            except TransientFetchError as exc:
                failed = True
                error = str(exc)
                append_jsonl(
                    self.failures_path,
                    [
                        {
                            "target": f"room:{self.room}",
                            "room": self.room,
                            "since": current_since,
                            "error": error,
                            "captured_at": captured_at,
                            "source": self.source,
                        }
                    ],
                )
                break

            pages_fetched += 1
            new_count, gap, new_since, page_count = self._store_page(
                page, current_since, captured_at
            )
            total_new += new_count
            any_gap = any_gap or gap
            current_since = new_since

            if page_count < self.page_limit:
                break  # caught up

            if pages_fetched >= self.max_pages_per_drain:
                capped = True
                append_jsonl(
                    self.drain_caps_path,
                    [
                        {
                            "room": self.room,
                            "pages_fetched": pages_fetched,
                            "since_after": current_since,
                            "captured_at": captured_at,
                            "source": self.source,
                        }
                    ],
                )
                break

        return {
            "room": self.room,
            "new_count": total_new,
            "gap": any_gap,
            "since_before": since_before,
            "since_after": current_since,
            "pages_fetched": pages_fetched,
            "capped": capped,
            "failed": failed,
            "error": error,
        }


class Collector:
    """Wires together the config, client, and followers for one collection
    pass (snapshot + events log + configured rooms).
    """

    def __init__(self, client, config):
        self.client = client
        self.config = config
        source = config.base_url
        self.coverage_tracker = CoverageTracker(config.data_dir)
        self.snapshotter = RoomsSnapshotter(client, config.data_dir, source)
        self.events_follower = RoomFollower(
            client, config.data_dir, EVENTS_ROOM, source, coverage_tracker=self.coverage_tracker
        )
        self.room_followers = [
            RoomFollower(
                client, config.data_dir, room, source, coverage_tracker=self.coverage_tracker
            )
            for room in config.rooms
        ]

    def _all_followers(self):
        return [self.events_follower] + self.room_followers

    def record_coverage(self, now=None):
        """Append one data/coverage.jsonl record per followed room (events
        + configured message rooms), plus a service-wide "__all__" rollup.
        """
        captured_at = now or utcnow_iso()
        followers = self._all_followers()
        rooms = [f.room for f in followers]
        cursors = {f.room: f._load_since() for f in followers}
        return self.coverage_tracker.report(
            rooms=rooms, cursors=cursors, source=self.snapshotter.source, captured_at=captured_at
        )

    def run_once(self, wait=0):
        # Every step below tolerates its own failure (snapshot and each
        # follower catch TransientFetchError internally and record it) so
        # one bad room or a snapshot hiccup never stops the rest of the
        # pass.
        results = {"snapshot": self.snapshotter.snapshot(), "events": None, "rooms": []}
        results["events"] = self.events_follower.fetch_and_store(wait=wait)
        for follower in self.room_followers:
            results["rooms"].append(follower.fetch_and_store(wait=wait))
        self.record_coverage()
        return results

    def run_loop(self, stop_after=None, sleep_fn=time.sleep, monotonic_fn=time.monotonic):
        """Continuous collection loop with two independent, fixed cadences
        off a monotonic clock -- single-threaded, no async, no busy-spin.

        MESSAGE cadence (`config.message_interval`, default 5s): drains the
        events log and every configured room (increment-2 drain: page
        forward at limit=200 until caught up, per-page cursor persist, gap
        + coverage accounting every page). Uses wait=0 -- the fixed short
        interval is the pacing, not server-side long-poll.

        SNAPSHOT cadence (`config.snapshot_interval`, default 300s):
        captures /rooms and appends a coverage.jsonl report.

        Each tick checks the message cadence BEFORE the snapshot cadence,
        so a due message-drain is delayed by at most one in-flight
        snapshot request -- never buried behind it indefinitely. When
        neither cadence is due, the loop sleeps only until whichever is
        due next, capped at ~1s, so it neither busy-spins nor sleeps
        through a cadence that's about to come due.

        This is meant to be started by the operator against the live
        service, not invoked by the collector's own tests/build step.
        `stop_after` (int) limits the loop to N ticks, for tests.
        """
        message_interval = self.config.message_interval
        snapshot_interval = self.config.snapshot_interval
        sleep_cap = 1.0

        start = monotonic_fn()
        next_message_due = start
        next_snapshot_due = start

        iterations = 0
        while stop_after is None or iterations < stop_after:
            now = monotonic_fn()
            if now >= next_message_due:
                self.events_follower.fetch_and_store(wait=0)
                for follower in self.room_followers:
                    follower.fetch_and_store(wait=0)
                next_message_due += message_interval
                if next_message_due <= now:  # we fell behind; don't burst-catch-up
                    next_message_due = now + message_interval

            now = monotonic_fn()
            if now >= next_snapshot_due:
                self.snapshotter.snapshot()
                self.record_coverage(now=None)
                next_snapshot_due += snapshot_interval
                if next_snapshot_due <= now:
                    next_snapshot_due = now + snapshot_interval

            iterations += 1
            if stop_after is not None and iterations >= stop_after:
                break

            now = monotonic_fn()
            next_due = min(next_message_due, next_snapshot_due)
            sleep_for = max(0.0, min(next_due - now, sleep_cap))
            sleep_fn(sleep_for)
