"""The whole-commons /rooms snapshot has its own fail-fast HTTP budget,
separate from the message-room fetch defaults. A slow or timing-out
/rooms must degrade to a recorded failure in a small, bounded number of
attempts -- never the full message-room transient-retry budget (6
attempts x 30s timeout, confirmed live to freeze the single-threaded loop
for minutes) -- so the message cadence is never starved behind it.
"""

import time

from helpers import AlwaysFailingClient

from collector.config import DEFAULT_SNAPSHOT_BACKOFF_CAP, DEFAULT_SNAPSHOT_MAX_ATTEMPTS, DEFAULT_SNAPSHOT_TIMEOUT, Config
from collector.core import Collector, RoomsSnapshotter
from collector.http_client import ReadDeadlineExceeded, TechnocoreClient, TransientFetchError
from collector.storage import read_jsonl


# --- _read_bounded_body: the deadline mechanism itself -------------------


class _SlowTricklingResponse:
    """iter_content() yields real chunks with a real (tiny) delay between
    them -- simulates a response that keeps the connection alive without
    ever completing, the exact shape a per-socket-read timeout alone does
    NOT protect against (see ReadDeadlineExceeded's docstring).
    """

    def __init__(self, num_chunks, delay_per_chunk):
        self.status_code = 200
        self.headers = {}
        self._num_chunks = num_chunks
        self._delay = delay_per_chunk
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for _ in range(self._num_chunks):
            time.sleep(self._delay)
            yield b"x"

    def close(self):
        self.closed = True


def test_read_bounded_body_raises_on_a_slow_trickle_past_its_deadline():
    from collector.http_client import _read_bounded_body

    resp = _SlowTricklingResponse(num_chunks=50, delay_per_chunk=0.02)  # 1s total if unbounded
    deadline = time.monotonic() + 0.05  # far shorter than the trickle would take

    start = time.monotonic()
    try:
        _read_bounded_body(resp, max_bytes=10_000, deadline=deadline)
        raise AssertionError("expected ReadDeadlineExceeded")
    except ReadDeadlineExceeded:
        pass
    elapsed = time.monotonic() - start

    assert elapsed < 0.5  # nowhere near the ~1s the full trickle would take


def test_read_bounded_body_with_no_deadline_is_unaffected_by_a_slow_trickle():
    # The message-room path never passes a deadline -- confirms that
    # remains true: deadline=None tolerates slow chunks exactly as before
    # this parameter existed.
    from collector.http_client import _read_bounded_body

    resp = _SlowTricklingResponse(num_chunks=3, delay_per_chunk=0.01)
    body = _read_bounded_body(resp, max_bytes=10_000, deadline=None)
    assert body == b"xxx"


# --- TechnocoreClient.get_rooms_overview: the fail-fast budget in action -


class _AlwaysTimingOutSession:
    """Every GET raises a connection-level ReadTimeout -- the exact
    symptom described live: a slow /rooms under load.
    """

    def __init__(self):
        self.requests = []

    def get(self, url, params=None, timeout=None, stream=False):
        self.requests.append((url, params, timeout))
        import requests

        raise requests.exceptions.ReadTimeout(f"simulated slow response (timeout={timeout}s)")


def test_snapshot_budget_gives_up_in_defaults_worth_of_attempts_not_the_message_budget():
    session = _AlwaysTimingOutSession()
    sleeps = []
    client = TechnocoreClient(
        "https://example.invalid", session=session, sleep_fn=lambda s: sleeps.append(s)
    )

    try:
        client.get_rooms_overview(
            timeout=DEFAULT_SNAPSHOT_TIMEOUT,
            max_attempts=DEFAULT_SNAPSHOT_MAX_ATTEMPTS,
            backoff_cap=DEFAULT_SNAPSHOT_BACKOFF_CAP,
        )
        raise AssertionError("expected TransientFetchError")
    except TransientFetchError:
        pass

    # Exactly DEFAULT_SNAPSHOT_MAX_ATTEMPTS attempts -- NOT
    # max_transient_retries + 1 (6 by default), which is what the
    # message-room path would allow for the identical failure.
    assert len(session.requests) == DEFAULT_SNAPSHOT_MAX_ATTEMPTS == 2
    # Every attempt used the overridden short timeout, not the client's
    # own (30s) default.
    assert all(t == DEFAULT_SNAPSHOT_TIMEOUT for _, _, t in session.requests)
    # Only (attempts - 1) backoff sleeps, each capped at
    # DEFAULT_SNAPSHOT_BACKOFF_CAP, not the message path's 30s cap.
    assert sleeps == [DEFAULT_SNAPSHOT_BACKOFF_CAP]


def test_message_room_fetch_still_uses_the_full_instance_budget_unaffected():
    # Regression guard on the guard: confirms overriding get_rooms_overview
    # did not accidentally shrink get_room_page's own (unrelated) budget.
    session = _AlwaysTimingOutSession()
    sleeps = []
    client = TechnocoreClient(
        "https://example.invalid",
        session=session,
        sleep_fn=lambda s: sleeps.append(s),
        max_transient_retries=5,
        transient_backoff_base=0.001,  # keep the test itself fast; only the COUNT matters here
        transient_backoff_cap=0.001,
    )

    try:
        client.get_room_page("lobby", since=0)
        raise AssertionError("expected TransientFetchError")
    except TransientFetchError:
        pass

    assert len(session.requests) == 6  # max_transient_retries=5 -> 6 total attempts, unchanged
    assert all(t == 30 for _, _, t in session.requests)  # client's own default timeout, unchanged


# --- RoomsSnapshotter.snapshot(): fast, recorded failure, real time -----


def test_snapshotter_fails_fast_within_a_small_bounded_real_time(tmp_path):
    session = _AlwaysTimingOutSession()
    client = TechnocoreClient(
        "https://example.invalid",
        session=session,
        sleep_fn=time.sleep,  # real sleep, but tiny defaults keep this well under a second
    )
    snapshotter = RoomsSnapshotter(
        client,
        str(tmp_path),
        source="test",
        snapshot_timeout=0.05,
        snapshot_max_attempts=2,
        snapshot_backoff_cap=0.05,
    )

    start = time.monotonic()
    result = snapshotter.snapshot()
    elapsed = time.monotonic() - start

    assert result["failed"] is True
    assert elapsed < 2.0  # nowhere near the message budget's own worst case

    failures = read_jsonl(snapshotter.failures_path)
    assert len(failures) == 1
    assert failures[0]["target"] == "snapshot"


def test_snapshotter_records_failure_without_raising_stays_green(tmp_path):
    # The pre-existing test this fix must keep green, run again here
    # verbatim as a belt-and-braces check that RoomsSnapshotter's new
    # constructor defaults don't change AlwaysFailingClient's behavior
    # (it raises immediately, never touching TechnocoreClient at all).
    client = AlwaysFailingClient(TransientFetchError("GET .../rooms failed (HTTP 503)"))
    snapshotter = RoomsSnapshotter(client, str(tmp_path), source="test")

    result = snapshotter.snapshot()

    assert result["failed"] is True
    assert result["record"] is None


# --- Collector.run_once(): a stuck snapshot never starves the rest ------


class _FastRoomsOnlyClient:
    """Every room/events page succeeds immediately. get_rooms_overview()
    is never called on this one in the test below -- Collector.__init__
    wires a single shared client for everything, so the test swaps
    RoomsSnapshotter.client for a real TechnocoreClient (wrapping a
    timing-out session) right after construction, leaving this object
    serving only get_room_page().
    """

    def get_room_page(
        self, room, since=0, wait=0, limit=None, timeout=None, max_attempts=None, backoff_cap=None
    ):
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


def test_run_once_reaches_events_and_rooms_promptly_despite_a_stuck_snapshot(tmp_path):
    config = Config(
        data_dir=str(tmp_path),
        rooms=["lobby"],
        snapshot_timeout=0.02,
        snapshot_max_attempts=2,
        snapshot_backoff_cap=0.02,
    )
    collector = Collector(_FastRoomsOnlyClient(), config)

    # Swap in a real TechnocoreClient wrapping a timing-out session for
    # the snapshot only, so its HTTP-level retry loop actually runs,
    # while room/events pages keep going through the plain fake client
    # above (fast, always succeeds).
    session = _AlwaysTimingOutSession()
    collector.snapshotter.client = TechnocoreClient(
        "https://example.invalid", session=session, sleep_fn=lambda s: None
    )

    start = time.monotonic()
    results = collector.run_once(wait=0)  # must not raise, must not hang
    elapsed = time.monotonic() - start

    assert elapsed < 2.0
    assert results["snapshot"]["failed"] is True
    assert results["events"]["failed"] is False
    assert results["rooms"][0]["failed"] is False
    assert results["rooms"][0]["new_count"] == 1
    # The snapshot's own budget, not the 6-attempt message-room budget.
    assert len(session.requests) == config.snapshot_max_attempts == 2
