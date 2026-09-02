"""Regression: a non-retryable status (404) or exhausted 429 must degrade
to a recorded failure through RoomFollower/RoomsSnapshotter/Collector,
never escape as a raw requests.exceptions.HTTPError.

These drive the REAL TechnocoreClient (a fake session/response standing in
only for the network), not an injected TransientFetchError, so they
exercise the exact code path the finding described: resp.raise_for_status()
at the two non-retried exit points in TechnocoreClient._get().

Before the fix: both of these raised requests.exceptions.HTTPError
uncaught out of RoomFollower.fetch_and_store()/Collector.run_once(),
which is exactly what killed the whole collector on one bad room.
"""

import json

from collector.config import Config
from collector.core import Collector, RoomFollower
from collector.http_client import NonRetryableFetchError, RateLimitExceeded, TechnocoreClient


class _FakeResponse:
    def __init__(self, status_code, body_obj=None, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body_bytes = json.dumps(body_obj if body_obj is not None else {}).encode("utf-8")

    def iter_content(self, chunk_size=65536):
        yield self._body_bytes

    def close(self):
        pass


class _FakeSession:
    """Repeats the last canned response/exception once its list is
    exhausted, since a Collector.run_once() pass issues several GETs
    (snapshot, events, each room) and every one of them should see the
    same failure mode in these tests.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.headers = {}
        self.requests = []

    def get(self, url, params=None, timeout=None, stream=False):
        self.requests.append((url, params))
        item = self._responses[0] if len(self._responses) == 1 else self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client_404():
    session = _FakeSession([_FakeResponse(404)])
    return TechnocoreClient("https://example.invalid", session=session, sleep_fn=lambda s: None)


def _client_429_exhausted():
    # Never below 429; max_429_retries=2 keeps the test fast.
    session = _FakeSession([_FakeResponse(429, headers={"Retry-After": "0"})] * 10)
    return TechnocoreClient(
        "https://example.invalid", session=session, sleep_fn=lambda s: None, max_429_retries=2
    )


# --- the client itself: confirms the specific exception types -----------


def test_client_raises_non_retryable_fetch_error_on_404_not_raw_httperror():
    client = _client_404()
    try:
        client.get_room_page("lobby", since=0)
        raise AssertionError("expected NonRetryableFetchError")
    except NonRetryableFetchError as exc:
        assert "404" in str(exc)


def test_client_raises_rate_limit_exceeded_on_429_exhaustion_not_raw_httperror():
    client = _client_429_exhausted()
    try:
        client.get_room_page("lobby", since=0)
        raise AssertionError("expected RateLimitExceeded")
    except RateLimitExceeded as exc:
        assert "429" in str(exc)


# --- through RoomFollower: must degrade to a recorded failure -----------


def test_follower_records_failure_on_real_404_does_not_raise(tmp_path):
    follower = RoomFollower(_client_404(), str(tmp_path), "lobby", source="test")

    result = follower.fetch_and_store()  # must not raise

    assert result["failed"] is True
    assert "404" in result["error"]
    assert result["new_count"] == 0


def test_follower_records_failure_on_real_429_exhaustion_does_not_raise(tmp_path):
    follower = RoomFollower(_client_429_exhausted(), str(tmp_path), "lobby", source="test")

    result = follower.fetch_and_store()  # must not raise

    assert result["failed"] is True
    assert "429" in result["error"]
    assert result["new_count"] == 0


# --- through Collector.run_once: one bad room must not take the rest down


def test_run_once_survives_a_real_404_on_one_room(tmp_path):
    config = Config(data_dir=str(tmp_path), rooms=["lobby"])
    collector = Collector(_client_404(), config)

    results = collector.run_once(wait=0)  # must not raise

    assert results["events"]["failed"] is True
    assert results["snapshot"]["failed"] is True
    assert results["rooms"][0]["failed"] is True


def test_run_once_survives_real_429_exhaustion_on_one_room(tmp_path):
    config = Config(data_dir=str(tmp_path), rooms=["lobby"])
    collector = Collector(_client_429_exhausted(), config)

    results = collector.run_once(wait=0)  # must not raise

    assert results["rooms"][0]["failed"] is True
