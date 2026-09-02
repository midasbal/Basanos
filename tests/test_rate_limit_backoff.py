"""429 backoff reads the wait seconds from the response (confirmed field),
and transient failures (timeouts/connection errors/5xx) retry with
exponential backoff before giving up.
"""

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
    def __init__(self, status_code, text_body="", headers=None, json_body=None):
        self.status_code = status_code
        self.text = text_body
        self.headers = headers or {}
        self._json_body = json_body

    def json(self):
        if self._json_body is None:
            raise ValueError("not json")
        return self._json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")


class _FakeSession:
    """Serves a canned sequence of responses/exceptions in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.headers = {}
        self.requests = []

    def get(self, url, params=None, timeout=None):
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

    try:
        client.get_rooms_overview()
        assert False, "expected TransientFetchError"
    except TransientFetchError as exc:
        assert "503" in str(exc)

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
