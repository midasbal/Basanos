"""429 backoff reads the wait seconds from the response (confirmed field),
and transient failures (timeouts/connection errors/5xx) retry with
exponential backoff before giving up.
"""

import json

import pytest
import requests

from helpers import load_fixture

from collector.http_client import TechnocoreClient, TransientFetchError, parse_wait_seconds


# --- 429: confirmed field (see fixture's "_note" and http_client.py's module
# docstring for how this was confirmed against the public source) ---------


def test_parse_wait_seconds_from_confirmed_retry_after_header():
    fixture = load_fixture("rate_limited_429.synthetic.json")
    assert parse_wait_seconds(headers=fixture["headers"]) == 4.0


def test_parse_wait_seconds_from_body_prose_matches_header():
    fixture = load_fixture("rate_limited_429.synthetic.json")
    # Independent cross-check: the prose in the body states the same number
    # the Retry-After header carries.
    assert parse_wait_seconds(body_text=fixture["body"]) == 4.0
    assert parse_wait_seconds(body_text=fixture["body"]) == float(fixture["headers"]["Retry-After"])


def test_parse_wait_seconds_prefers_header_over_body():
    # If they ever disagreed, the header wins -- it's the confirmed
    # authoritative source, the body prose is only a cross-check.
    assert parse_wait_seconds(headers={"Retry-After": "9"}, body_text="retry after: 4s") == 9.0


def test_parse_wait_seconds_legacy_json_fallback_when_nothing_else_present():
    # Dead against the real service (429 is never JSON) but kept as a
    # forward-compat fallback.
    assert parse_wait_seconds(json_body={"wait": 2.5}) == 2.5


def test_parse_wait_seconds_default_when_nothing_present():
    assert parse_wait_seconds() == 1.0


class _FakeResponse:
    """Stands in for a requests.Response fetched with stream=True: the
    client reads its body via iter_content(), never .text/.json()/
    .raise_for_status() directly (see _read_bounded_body in http_client.py).
    """

    def __init__(
        self,
        status_code,
        text_body="",
        headers=None,
        json_body=None,
        body_bytes=None,
        iter_content_error=None,
    ):
        self.status_code = status_code
        self.headers = headers or {}
        if body_bytes is not None:
            self._body_bytes = body_bytes
        elif json_body is not None:
            self._body_bytes = json.dumps(json_body).encode("utf-8")
        else:
            self._body_bytes = text_body.encode("utf-8")
        # When set, iter_content() raises this instead of yielding a body --
        # simulates a connection dropping (or timing out) mid-body-read,
        # as opposed to session.get() itself failing before a response ever
        # comes back.
        self._iter_content_error = iter_content_error
        self.closed = False

    def iter_content(self, chunk_size=65536):
        if self._iter_content_error is not None:
            raise self._iter_content_error
        for i in range(0, len(self._body_bytes), chunk_size):
            yield self._body_bytes[i : i + chunk_size]

    def close(self):
        self.closed = True


class _FakeSession:
    """Serves a canned sequence of responses/exceptions in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.headers = {}
        self.requests = []

    def get(self, url, params=None, timeout=None, stream=False):
        self.requests.append((url, params))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client(session, **kwargs):
    sleeps = []
    client = TechnocoreClient(
        "https://example.invalid",
        session=session,
        sleep_fn=lambda s: sleeps.append(s),
        **kwargs,
    )
    return client, sleeps


def test_client_backs_off_by_confirmed_retry_after_then_retries():
    fixture = load_fixture("rate_limited_429.synthetic.json")
    ok_body = {"room": "lobby", "count": 0, "first_seq": None, "last_seq": 5, "generation": 0, "messages": []}

    session = _FakeSession(
        [
            _FakeResponse(429, text_body=fixture["body"], headers=fixture["headers"]),
            _FakeResponse(200, json_body=ok_body),
        ]
    )
    client, sleeps = _client(session)

    result = client.get_room_page("lobby", since=0)

    assert result == ok_body
    assert sleeps == [4.0]  # backed off exactly the confirmed Retry-After value
    assert len(session.requests) == 2


# --- untrusted Retry-After: clamped, never crashes, never stalls forever -


def test_negative_retry_after_is_clamped_to_zero_not_a_crash():
    # Regression: time.sleep(negative) raises ValueError uncaught. A
    # negative Retry-After (malformed/hostile) must not reach _sleep() as
    # a negative number.
    ok_body = {"rooms": [], "total": 0}
    session = _FakeSession(
        [
            _FakeResponse(429, headers={"Retry-After": "-100"}),
            _FakeResponse(200, json_body=ok_body),
        ]
    )
    client, sleeps = _client(session)

    result = client.get_rooms_overview()  # must not raise ValueError

    assert result == ok_body
    assert sleeps == [0.0]


def test_huge_retry_after_is_clamped_to_the_backoff_cap_not_an_unbounded_stall():
    # Regression: an unclamped wait_s honors whatever the server claims,
    # verbatim -- a hostile or misconfigured Retry-After of a huge number
    # would stall this single-threaded client (and therefore the whole
    # collection loop) for that entire duration.
    ok_body = {"rooms": [], "total": 0}
    session = _FakeSession(
        [
            _FakeResponse(429, headers={"Retry-After": "999999999"}),
            _FakeResponse(200, json_body=ok_body),
        ]
    )
    client, sleeps = _client(session, transient_backoff_cap=30.0)

    result = client.get_rooms_overview()  # must not stall on the full 999999999s

    assert result == ok_body
    assert sleeps == [30.0]  # clamped to transient_backoff_cap, not honored verbatim


# --- transient failures: timeouts / connection errors / 5xx --------------


def test_client_retries_5xx_with_exponential_backoff_then_succeeds():
    ok_body = {"rooms": [], "total": 0}
    session = _FakeSession(
        [
            _FakeResponse(503),
            _FakeResponse(502),
            _FakeResponse(200, json_body=ok_body),
        ]
    )
    client, sleeps = _client(session, transient_backoff_base=1.0, transient_backoff_cap=30.0)

    result = client.get_rooms_overview()

    assert result == ok_body
    assert sleeps == [1.0, 2.0]  # doubling: 1s after the 503, 2s after the 502
    assert len(session.requests) == 3


def test_client_retries_connection_errors_and_timeouts():
    ok_body = {"rooms": [], "total": 0}
    session = _FakeSession(
        [
            requests.exceptions.ConnectionError("connection refused"),
            requests.exceptions.ReadTimeout("read timed out"),
            _FakeResponse(200, json_body=ok_body),
        ]
    )
    client, sleeps = _client(session, transient_backoff_base=1.0, transient_backoff_cap=30.0)

    result = client.get_rooms_overview()

    assert result == ok_body
    assert sleeps == [1.0, 2.0]


def test_client_backoff_is_capped():
    session = _FakeSession(
        [_FakeResponse(503)] * 5 + [_FakeResponse(200, json_body={"ok": True})]
    )
    client, sleeps = _client(
        session, transient_backoff_base=10.0, transient_backoff_cap=15.0, max_transient_retries=5
    )

    client.get_rooms_overview()

    # 10, 20->capped 15, 40->capped 15, 80->capped 15, 160->capped 15
    assert sleeps == [10.0, 15.0, 15.0, 15.0, 15.0]


def test_client_gives_up_after_max_transient_retries_and_raises():
    session = _FakeSession([_FakeResponse(503)] * 10)  # never succeeds
    client, sleeps = _client(session, max_transient_retries=3, transient_backoff_base=0.01)

    with pytest.raises(TransientFetchError, match="503"):
        client.get_rooms_overview()

    assert len(session.requests) == 4  # 1 initial + 3 retries
    assert len(sleeps) == 3


def test_client_429_and_transient_retries_are_independent_counters():
    # A 429 followed by 5xx followed by success shouldn't confuse the two
    # attempt counters with each other.
    fixture = load_fixture("rate_limited_429.synthetic.json")
    ok_body = {"rooms": [], "total": 0}
    session = _FakeSession(
        [
            _FakeResponse(429, text_body=fixture["body"], headers=fixture["headers"]),
            _FakeResponse(503),
            _FakeResponse(200, json_body=ok_body),
        ]
    )
    client, sleeps = _client(session, transient_backoff_base=1.0)

    result = client.get_rooms_overview()

    assert result == ok_body
    assert sleeps == [4.0, 1.0]
    assert len(session.requests) == 3


# --- transient failures during the body read itself (not session.get) ----
#
# Regression: session.get() succeeding but the body read then raising
# requests.exceptions.ConnectionError (a connection dropping, or timing
# out, mid-stream) used to escape _get() as a raw, uncaught exception --
# the retry loop's body-read stage only caught ReadDeadlineExceeded, never
# a plain RequestException. That crashed the collector process in
# production (revived only by systemd's restart). These two tests inject
# the failure at iter_content() specifically, the path the earlier tests
# above never exercised.


def test_client_retries_a_connection_error_during_body_read_then_succeeds():
    ok_body = {"rooms": [], "total": 0}
    session = _FakeSession(
        [
            _FakeResponse(
                200,
                iter_content_error=requests.exceptions.ConnectionError(
                    "connection dropped mid-body"
                ),
            ),
            _FakeResponse(200, json_body=ok_body),
        ]
    )
    client, sleeps = _client(session, transient_backoff_base=1.0, transient_backoff_cap=30.0)

    result = client.get_rooms_overview()

    assert result == ok_body
    assert len(session.requests) == 2  # a fresh session.get() was reissued on retry
    assert sleeps == [1.0]  # one transient retry was actually consumed


def test_client_gives_up_after_persistent_connection_errors_during_body_read():
    session = _FakeSession(
        [
            _FakeResponse(
                200,
                iter_content_error=requests.exceptions.ConnectionError(
                    "connection dropped mid-body"
                ),
            )
        ]
        * 10  # never succeeds
    )
    client, sleeps = _client(session, max_transient_retries=3, transient_backoff_base=0.01)

    with pytest.raises(TransientFetchError) as exc_info:
        client.get_rooms_overview()

    # The exact regression: a raw requests exception must never escape --
    # only the wrapping TransientFetchError should.
    assert not isinstance(exc_info.value, requests.exceptions.RequestException)
    assert len(session.requests) == 4  # 1 initial + 3 retries, same budget as any other transient path
    assert len(sleeps) == 3
