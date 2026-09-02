"""Minimal read-only HTTP client for the Technocore commons API.

Only ever issues GET requests. Never writes, posts, signs, or touches any
identity/key material.

Two independent retry paths, kept separate on purpose (they have different
causes and different exit conditions):

- HTTP 429 (the read/write rate limit): back off exactly as long as the
  server says, then retry. Never gives up silently -- exhausting
  max_429_retries raises, since a caller that's still rate limited after
  that long is a configuration problem, not a blip.
- Connection errors, timeouts, and 5xx (transient infrastructure trouble):
  exponential backoff, capped attempts, and on exhaustion a distinct
  TransientFetchError the caller can catch to skip this pass without
  losing its cursor or crashing the collection loop.
"""

import re
import time

import requests

# Confirmed against the technocore-chat source (src/limit.py, limit.limited(),
# and src/app.py's text()/PlainTextResponse): every 429 -- for every route,
# regardless of ?format=json -- sets the `Retry-After` header to
# str(max(1, round(retry_after))), an integer number of seconds. The 429
# body itself is ALWAYS text/plain (limited() calls text(body, 429), and
# `text()` hardcodes media_type="text/plain"; ?format=json only changes the
# *200* response shape in respond(), never the 429 path), so there is no
# JSON field to read -- "wait" was a guess in increment 1 and never
# appears in practice. The plain-text body does say the same number in
# prose ("retry after: {wait}s"), which we parse as a second, independent
# confirmation in case a header ever gets stripped by something in between.
_RETRY_AFTER_BODY_RE = re.compile(r"retry after:\s*(\d+(?:\.\d+)?)s", re.IGNORECASE)

# Kept as a last-resort fallback only: in case a future version of the
# service ever does put a wait duration in a JSON error body, these are the
# key names we'd check. Never observed in the confirmed source.
_LEGACY_GUESS_BODY_KEYS = ("wait", "wait_seconds", "retry_after", "retry_after_seconds")

_RETRYABLE_STATUS = {500, 502, 503, 504}


class RateLimitExceeded(Exception):
    """Raised when the server keeps returning 429 past max_429_retries."""


class TransientFetchError(Exception):
    """A GET failed with a connection error, timeout, or 5xx and stayed
    failed through every retry.

    Callers (RoomFollower, RoomsSnapshotter) catch this to record a failure
    and move on without advancing a cursor or crashing the collection loop.
    """

    def __init__(self, message, cause=None):
        super().__init__(message)
        self.cause = cause


def parse_wait_seconds(headers=None, body_text=None, json_body=None, default=1.0):
    """Extract a 429 backoff duration (seconds), in confirmed-priority order.

    1. The `Retry-After` header -- confirmed authoritative (see module
       docstring): the server sets this on every 429, always.
    2. The "retry after: <N>s" line in the plain-text body -- the same
       number, in prose, as an independent cross-check.
    3. A handful of guessed JSON keys, only reachable if the body somehow
       parses as JSON -- dead code against the real service today, kept for
       forward compatibility.
    4. `default`.
    """
    if headers:
        retry_after = headers.get("Retry-After")
        if retry_after is not None:
            try:
                return float(retry_after)
            except (TypeError, ValueError):
                pass
    if body_text:
        m = _RETRY_AFTER_BODY_RE.search(body_text)
        if m:
            return float(m.group(1))
    if isinstance(json_body, dict):
        for key in _LEGACY_GUESS_BODY_KEYS:
            if key in json_body:
                try:
                    return float(json_body[key])
                except (TypeError, ValueError):
                    pass
    return default


class TechnocoreClient:
    """Thin, read-only wrapper around the Technocore public HTTP API."""

    def __init__(
        self,
        base_url,
        session=None,
        user_agent=None,
        timeout=30,
        max_429_retries=5,
        max_transient_retries=5,
        transient_backoff_base=1.0,
        transient_backoff_cap=30.0,
        sleep_fn=time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        if user_agent:
            self.session.headers["User-Agent"] = user_agent
        self.timeout = timeout
        self.max_429_retries = max_429_retries
        self.max_transient_retries = max_transient_retries
        self.transient_backoff_base = transient_backoff_base
        self.transient_backoff_cap = transient_backoff_cap
        self._sleep = sleep_fn

    def _get(self, path, params=None):
        url = f"{self.base_url}{path}"
        attempts_429 = 0
        attempts_transient = 0
        backoff = self.transient_backoff_base
        while True:
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.exceptions.RequestException as exc:
                attempts_transient += 1
                if attempts_transient > self.max_transient_retries:
                    raise TransientFetchError(
                        f"GET {url} failed after {attempts_transient} attempts "
                        f"(connection error/timeout): {exc}",
                        cause=exc,
                    ) from exc
                self._sleep(min(backoff, self.transient_backoff_cap))
                backoff *= 2
                continue

            if resp.status_code == 429:
                attempts_429 += 1
                if attempts_429 > self.max_429_retries:
                    resp.raise_for_status()
                try:
                    json_body = resp.json()
                except ValueError:
                    json_body = None
                wait_s = parse_wait_seconds(
                    headers=resp.headers, body_text=resp.text, json_body=json_body
                )
                self._sleep(wait_s)
                continue

            if resp.status_code in _RETRYABLE_STATUS:
                attempts_transient += 1
                if attempts_transient > self.max_transient_retries:
                    raise TransientFetchError(
                        f"GET {url} failed after {attempts_transient} attempts "
                        f"(HTTP {resp.status_code})",
                        cause=None,
                    )
                self._sleep(min(backoff, self.transient_backoff_cap))
                backoff *= 2
                continue

            resp.raise_for_status()
            return resp.json()

    def get_rooms_overview(self):
        """GET /rooms?format=json"""
        return self._get("/rooms", params={"format": "json"})

    def get_room_page(self, room, since=0, wait=0, limit=None):
        """GET /r/<room>?since=<seq>&format=json

        `room` may be the literal room name, or "events" for the
        service-wide room-discovery log at /r/events, which has the
        identical page shape and is followed with a cursor the same way.

        `limit` caps the page size (confirmed live: /r/lobby?since=...&
        limit=200&format=json returns at most 200 messages, oldest-first,
        starting at first_seq == since+1 when the ring hasn't dropped
        anything). Followers use it to drain a backlog page by page
        instead of losing everything past the first `limit` messages.
        """
        params = {"since": since, "format": "json"}
        if wait:
            params["wait"] = wait
        if limit:
            params["limit"] = limit
        return self._get(f"/r/{room}", params=params)
