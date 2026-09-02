"""The message-room fetch path (get_room_page, via RoomFollower) has its
own fail-fast HTTP budget, the same mechanism the snapshot got, applied
one layer down. Confirmed live: a stalled fetch on /r/events blocked
inside a single response read for 30s+, ordinary service slowness under
load, not an adversarial case. With the client's original defaults
(timeout=30, 6 total attempts) that multiplies into the same multi-minute
freeze the snapshot fix was built to avoid -- except here the loss is not
recoverable: it's lobby's actual traffic, evicted from its ~20s ring
while the loop is frozen.
"""

import time

from helpers import AlwaysFailingClient, FakeClient

from collector.config import (
    DEFAULT_MESSAGE_BACKOFF_CAP,
    DEFAULT_MESSAGE_MAX_ATTEMPTS,
    DEFAULT_MESSAGE_TIMEOUT,
    Config,
)
from collector.core import Collector, RoomFollower
from collector.http_client import TechnocoreClient, TransientFetchError
from collector.storage import load_json, read_jsonl, save_json_atomic


# --- TechnocoreClient.get_room_page: the fail-fast budget in action -----


class _AlwaysTimingOutSession:
    """Every GET raises a connection-level ReadTimeout -- the exact live
    symptom: a stalled connect/response on an overloaded endpoint.
    """

    def __init__(self):
        self.requests = []

    def get(self, url, params=None, timeout=None, stream=False):
        self.requests.append((url, params, timeout))
        import requests

        raise requests.exceptions.ReadTimeout(f"simulated stalled response (timeout={timeout}s)")


def test_message_budget_gives_up_in_defaults_worth_of_attempts_not_the_old_six():
    session = _AlwaysTimingOutSession()
    sleeps = []
    client = TechnocoreClient(
        "https://example.invalid", session=session, sleep_fn=lambda s: sleeps.append(s)
    )

    try:
        client.get_room_page(
            "lobby",
            since=0,
            timeout=DEFAULT_MESSAGE_TIMEOUT,
            max_attempts=DEFAULT_MESSAGE_MAX_ATTEMPTS,
            backoff_cap=DEFAULT_MESSAGE_BACKOFF_CAP,
        )
        raise AssertionError("expected TransientFetchError")
    except TransientFetchError:
        pass

    # Exactly DEFAULT_MESSAGE_MAX_ATTEMPTS attempts -- not the client's
    # own max_transient_retries+1 (6), which is what get_room_page() used
    # before it accepted its own budget.
    assert len(session.requests) == DEFAULT_MESSAGE_MAX_ATTEMPTS == 4
    assert all(t == DEFAULT_MESSAGE_TIMEOUT for _, _, t in session.requests)
    # (attempts - 1) backoff sleeps, each capped at
    # DEFAULT_MESSAGE_BACKOFF_CAP, not the client's own 30s cap.
    assert sleeps == [DEFAULT_MESSAGE_BACKOFF_CAP] * (DEFAULT_MESSAGE_MAX_ATTEMPTS - 1)


def test_message_budget_allows_more_attempts_than_the_snapshot_does():
    # The explicit "more resilient than the snapshot" requirement: same
    # mechanism, more room to retry before giving up.
    from collector.config import DEFAULT_SNAPSHOT_MAX_ATTEMPTS

    assert DEFAULT_MESSAGE_MAX_ATTEMPTS > DEFAULT_SNAPSHOT_MAX_ATTEMPTS


def test_message_worst_case_stays_well_under_lobbys_ring_cycle():
    # The explicit sanity check the task asked for: worst case vs lobby's
    # observed ~20s ring cycle (see collector/config.py's derivation).
    worst_case = DEFAULT_MESSAGE_MAX_ATTEMPTS * DEFAULT_MESSAGE_TIMEOUT + (
        DEFAULT_MESSAGE_MAX_ATTEMPTS - 1
    ) * DEFAULT_MESSAGE_BACKOFF_CAP
    assert worst_case == 13.0
    lobby_ring_cycle_seconds = 20.0
    assert worst_case < lobby_ring_cycle_seconds * 0.75  # real margin, not a photo finish


# --- a genuinely stalled connect (never responds at all) ----------------


class _HangingConnectSession:
    """Simulates a connect/response that never comes back within the
    per-attempt timeout -- requests itself raises ConnectTimeout/
    ReadTimeout once the socket-level timeout elapses; this stands in for
    that without actually blocking real time in the test.
    """

    def __init__(self):
        self.requests = []

    def get(self, url, params=None, timeout=None, stream=False):
        self.requests.append((url, params, timeout))
        import requests

        raise requests.exceptions.ConnectTimeout(f"simulated hung connect (timeout={timeout}s)")


def test_stalled_connect_fails_within_small_bounded_real_time_through_follower(tmp_path):
    session = _HangingConnectSession()
    client = TechnocoreClient(
        "https://example.invalid",
        session=session,
        sleep_fn=time.sleep,  # real sleep; tiny overrides below keep this well under a second
    )
    follower = RoomFollower(
        client,
        str(tmp_path),
        "lobby",
        source="test",
        message_timeout=0.02,
        message_max_attempts=3,
        message_backoff_cap=0.02,
    )
    save_json_atomic(follower.state_path, {"since": 500})

    start = time.monotonic()
    result = follower.fetch_and_store()  # must not raise, must not hang
    elapsed = time.monotonic() - start

    assert result["failed"] is True
    assert elapsed < 2.0  # nowhere near the old 6x30s worst case

    # cursor untouched -- a failed fetch never advances it
    assert result["since_after"] == 500
    assert load_json(follower.state_path) == {"since": 500}
    assert read_jsonl(follower.messages_path) == []

    failures = read_jsonl(follower.failures_path)
    assert len(failures) == 1
    assert failures[0]["room"] == "lobby"
    assert failures[0]["since"] == 500


# --- a slow-trickle body (the exact "stalled inside the response read"


class _SlowTricklingResponse:
    """iter_content() yields real chunks with a real (tiny) delay between
    them -- the exact shape of "blocked inside conn.getresponse() /
    reading the body", which a per-socket-read timeout alone does not
    bound (see ReadDeadlineExceeded's docstring in http_client.py).
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


class _SlowTrickleSession:
    def __init__(self, num_chunks, delay_per_chunk):
        self._num_chunks = num_chunks
        self._delay = delay_per_chunk
        self.requests = []

    def get(self, url, params=None, timeout=None, stream=False):
        self.requests.append((url, params, timeout))
        return _SlowTricklingResponse(self._num_chunks, self._delay)


def test_slow_trickle_body_fails_within_small_bounded_real_time_through_follower(tmp_path):
    # 100 chunks x 0.01s = 1s total if unbounded; each individual chunk
    # arrives fast, so a per-read timeout alone would never trip -- only
    # the total-read deadline (now applied to the message path) catches
    # this, exactly the live-confirmed failure mode.
    session = _SlowTrickleSession(num_chunks=100, delay_per_chunk=0.01)
    client = TechnocoreClient("https://example.invalid", session=session, sleep_fn=time.sleep)
    follower = RoomFollower(
        client,
        str(tmp_path),
        "events",
        source="test",
        message_timeout=0.05,
        message_max_attempts=2,
        message_backoff_cap=0.02,
    )
    save_json_atomic(follower.state_path, {"since": 100})

    start = time.monotonic()
    result = follower.fetch_and_store()
    elapsed = time.monotonic() - start

    assert result["failed"] is True
    assert elapsed < 1.0  # far under the ~1s the full trickle would take unbounded
    assert result["since_after"] == 100  # cursor untouched
    assert read_jsonl(follower.messages_path) == []


# --- Collector.run_once(): the pass proceeds despite a stuck room -------


def test_run_once_proceeds_promptly_despite_a_stalled_message_room(tmp_path):
    config = Config(
        data_dir=str(tmp_path),
        rooms=["lobby"],
        message_timeout=0.02,
        message_max_attempts=2,
        message_backoff_cap=0.02,
    )
    session = _AlwaysTimingOutSession()
    stalled_client = TechnocoreClient(
        "https://example.invalid", session=session, sleep_fn=lambda s: None
    )
    collector = Collector(stalled_client, config)

    # Snapshot succeeds fast via a plain fake client swapped in after
    # construction -- isolates the assertion to the message path, the
    # thing this fix is about.
    collector.snapshotter.client = FakeClient(rooms_overview={"rooms": [], "total": 0})

    start = time.monotonic()
    results = collector.run_once(wait=0)  # must not raise, must not hang
    elapsed = time.monotonic() - start

    assert elapsed < 2.0
    assert results["snapshot"]["failed"] is False
    assert results["events"]["failed"] is True
    assert results["rooms"][0]["failed"] is True
    # Each stalled follower used its own small budget, not the old 6.
    assert len(session.requests) == config.message_max_attempts * 2 == 4  # events + lobby


def test_snapshotter_records_failure_without_raising_still_stands(tmp_path):
    # Unrelated pre-existing behavior this fix must not disturb.
    from collector.core import RoomsSnapshotter

    client = AlwaysFailingClient(TransientFetchError("GET .../rooms failed (HTTP 503)"))
    snapshotter = RoomsSnapshotter(client, str(tmp_path), source="test")
    result = snapshotter.snapshot()
    assert result["failed"] is True
