import json
import os

from make_fixtures import FIXTURE_DID_1, FIXTURE_DID_2  # noqa: F401 -- re-exported

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES_DIR, name), encoding="utf-8") as f:
        return json.load(f)


class FakeClient:
    """Stand-in for TechnocoreClient that serves canned pages instead of
    making real HTTP calls, so collector logic can be tested against fixed
    fixture data.

    `room_pages[room]` is either a single page dict (returned every call)
    or a list of page dicts (returned in order, one per call, last one
    repeats once exhausted).
    """

    def __init__(self, rooms_overview=None, room_pages=None):
        self.rooms_overview = rooms_overview
        self.room_pages = room_pages or {}
        self._call_index = {}
        self.calls = []

    def get_rooms_overview(self, timeout=None, max_attempts=None, backoff_cap=None):
        self.calls.append(("rooms_overview", None))
        return self.rooms_overview

    def get_room_page(
        self, room, since=0, wait=0, limit=None, timeout=None, max_attempts=None, backoff_cap=None
    ):
        self.calls.append(("room_page", room, since, wait, limit))
        pages = self.room_pages[room]
        if isinstance(pages, list):
            idx = self._call_index.get(room, 0)
            page = pages[min(idx, len(pages) - 1)]
            self._call_index[room] = idx + 1
            return page
        return pages


class AlwaysFailingClient:
    """Stand-in client whose get_room_page/get_rooms_overview always raise
    the given exception (instance or zero-arg factory) -- for testing that
    a follower/snapshotter records a failure and does not advance its
    cursor, without hitting the network.
    """

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def _raise(self):
        self.calls += 1
        raise self.exc() if callable(self.exc) else self.exc

    def get_room_page(
        self, room, since=0, wait=0, limit=None, timeout=None, max_attempts=None, backoff_cap=None
    ):
        self._raise()

    def get_rooms_overview(self, timeout=None, max_attempts=None, backoff_cap=None):
        self._raise()
