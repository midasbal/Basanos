"""Untrusted-input handling: a page whose numeric envelope can't be
trusted, or a response body that exceeds the size cap, must degrade to a
recorded failure through RoomFollower, never an uncaught
KeyError/TypeError (page shape) or unbounded memory use (response size).
"""

import json

from helpers import FakeClient

from collector.core import RoomFollower
from collector.http_client import ResponseTooLarge, TechnocoreClient
from collector.storage import read_jsonl


# --- fix 3: malformed numeric envelope -----------------------------------


def test_page_missing_seq_on_a_message_is_a_recorded_failure_not_a_crash(tmp_path):
    page = {
        "room": "lobby",
        "count": 1,
        "first_seq": 101,
        "last_seq": 101,
        "generation": 0,
        "messages": [{"ts": "t", "from": "nick", "text": "no seq field here"}],
    }
    client = FakeClient(room_pages={"lobby": page})
    follower = RoomFollower(client, str(tmp_path), "lobby", source="test")

    result = follower.fetch_and_store()  # must not raise KeyError

    assert result["failed"] is True
    assert "seq" in result["error"]
    assert result["new_count"] == 0
    failures = read_jsonl(follower.failures_path)
    assert len(failures) == 1
    assert failures[0]["room"] == "lobby"


def test_page_with_non_numeric_count_is_a_recorded_failure_not_a_crash(tmp_path):
    page = {
        "room": "lobby",
        "count": "5",  # string, not int -- untrusted/malformed response
        "first_seq": 101,
        "last_seq": 105,
        "generation": 0,
        "messages": [],
    }
    client = FakeClient(room_pages={"lobby": page})
    follower = RoomFollower(client, str(tmp_path), "lobby", source="test")

    result = follower.fetch_and_store()  # must not raise TypeError

    assert result["failed"] is True
    assert "count" in result["error"]


def test_page_with_non_numeric_first_seq_is_a_recorded_failure_not_a_crash(tmp_path):
    page = {
        "room": "lobby",
        "count": 1,
        "first_seq": "abc",  # not an int -- would raise TypeError in detect_gap
        "last_seq": 101,
        "generation": 0,
        "messages": [{"seq": 101, "ts": "t", "from": "nick", "text": "x"}],
    }
    client = FakeClient(room_pages={"lobby": page})
    follower = RoomFollower(client, str(tmp_path), "lobby", source="test")

    result = follower.fetch_and_store()  # must not raise TypeError

    assert result["failed"] is True
    assert "first_seq" in result["error"]


def test_valid_page_with_null_first_seq_still_processes_normally(tmp_path):
    # Sanity check the validator isn't over-strict: first_seq/last_seq are
    # legitimately null on an empty (count=0) page -- existing, tolerated
    # shape, must keep working exactly as before.
    page = {
        "room": "lobby",
        "count": 0,
        "first_seq": None,
        "last_seq": 5,
        "generation": 0,
        "messages": [],
    }
    client = FakeClient(room_pages={"lobby": page})
    follower = RoomFollower(client, str(tmp_path), "lobby", source="test")

    result = follower.fetch_and_store()

    assert result["failed"] is False
    assert result["since_after"] == 5


def test_one_malformed_room_does_not_block_a_run_once_pass(tmp_path):
    from collector.config import Config
    from collector.core import Collector

    class MixedClient:
        def get_rooms_overview(self, timeout=None, max_attempts=None, backoff_cap=None):
            return {"rooms": [], "total": 0}

        def get_room_page(
            self, room, since=0, wait=0, limit=None, timeout=None, max_attempts=None, backoff_cap=None
        ):
            if room == "lobby":
                return {
                    "room": "lobby",
                    "count": 1,
                    "first_seq": 1,
                    "last_seq": 1,
                    "generation": 0,
                    "messages": [{"ts": "t", "from": "nick", "text": "missing seq"}],
                }
            return {
                "room": room,
                "count": 1,
                "first_seq": since + 1,
                "last_seq": since + 1,
                "generation": 0,
                "messages": [{"seq": since + 1, "ts": "t", "from": "nick", "text": "ok"}],
            }

    config = Config(data_dir=str(tmp_path), rooms=["lobby", "meta"])
    collector = Collector(MixedClient(), config)

    results = collector.run_once(wait=0)  # must not raise

    lobby_result = next(r for r in results["rooms"] if r["room"] == "lobby")
    meta_result = next(r for r in results["rooms"] if r["room"] == "meta")
    assert lobby_result["failed"] is True
    assert meta_result["failed"] is False
    assert meta_result["new_count"] == 1


# --- fix 4: bounded response size -----------------------------------------


class _HugeResponse:
    """iter_content() yields well past the cap -- the client must refuse
    before buffering it all, not read it to the end and check after.
    """

    def __init__(self, total_bytes, chunk_size=1024):
        self.status_code = 200
        self.headers = {}
        self._total = total_bytes
        self._chunk_size = chunk_size
        self.closed = False

    def iter_content(self, chunk_size=65536):
        chunk = b"x" * self._chunk_size
        sent = 0
        while sent < self._total:
            sent += len(chunk)
            yield chunk

    def close(self):
        self.closed = True


class _HugeResponseSession:
    def __init__(self, total_bytes):
        self._total_bytes = total_bytes
        self.headers = {}
        self.requests = []
        self.last_response = None

    def get(self, url, params=None, timeout=None, stream=False):
        self.requests.append((url, params))
        self.last_response = _HugeResponse(self._total_bytes)
        return self.last_response


def test_over_cap_response_is_refused_not_buffered():
    # A tiny cap (10 bytes) so the test doesn't need to actually move
    # megabytes of fake data around to prove the point.
    session = _HugeResponseSession(total_bytes=10_000_000)
    client = TechnocoreClient(
        "https://example.invalid",
        session=session,
        sleep_fn=lambda s: None,
        max_response_bytes=10,
    )

    try:
        client.get_rooms_overview()
        raise AssertionError("expected ResponseTooLarge")
    except ResponseTooLarge as exc:
        assert "10" in str(exc)

    # Refused early, not after reading the whole (fake) 10MB body: proves
    # the cap is enforced during the read, not by measuring afterward.
    assert session.last_response.closed is True


def test_under_cap_response_is_read_normally():
    ok_body = {"rooms": [], "total": 0}
    body_bytes = json.dumps(ok_body).encode("utf-8")

    class _SmallResponse:
        status_code = 200
        headers = {}

        def iter_content(self, chunk_size=65536):
            yield body_bytes

        def close(self):
            pass

    class _SmallSession:
        def get(self, url, params=None, timeout=None, stream=False):
            return _SmallResponse()

    client = TechnocoreClient(
        "https://example.invalid",
        session=_SmallSession(),
        sleep_fn=lambda s: None,
        max_response_bytes=len(body_bytes),  # exactly enough, not a real-world 20MB cap
    )

    assert client.get_rooms_overview() == ok_body


def test_follower_records_failure_on_over_cap_response_does_not_raise(tmp_path):
    session = _HugeResponseSession(total_bytes=10_000_000)
    client = TechnocoreClient(
        "https://example.invalid",
        session=session,
        sleep_fn=lambda s: None,
        max_response_bytes=10,
    )
    follower = RoomFollower(client, str(tmp_path), "lobby", source="test")

    result = follower.fetch_and_store()  # must not raise

    assert result["failed"] is True
    assert "10" in result["error"]
