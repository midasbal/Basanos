"""Minimal read-only HTTP client for the Technocore commons API.

Only ever issues GET requests. Never writes, posts, signs, or touches any
identity/key material.

Two independent retry paths, kept separate on purpose (they have different
causes and different exit conditions):

- HTTP 429 (the read/write rate limit): back off exactly as long as the
  server says (clamped, see _clamp_wait below), then retry. Never gives up
  silently -- exhausting max_429_retries raises RateLimitExceeded, since a
  caller that's still rate limited after that long is a configuration
  problem, not a blip.
- Connection errors, timeouts, and 5xx (transient infrastructure trouble):
  exponential backoff, capped attempts, and on exhaustion a
  TransientFetchError the caller can catch to skip this pass without
  losing its cursor or crashing the collection loop.

Every non-2xx outcome this client cannot or will not retry -- 429
exhaustion, a non-retryable status (404/400/403/...), or a response body
that exceeds the size cap -- surfaces as a TransientFetchError subclass,
never a raw requests.exceptions.HTTPError. RoomFollower and
RoomsSnapshotter (collector/core.py) catch the TransientFetchError base,
so any of these degrade to a recorded failure and the loop moves on,
rather than crashing the process on the first 404 or oversized response.

_get()'s `timeout`/`max_attempts`/`backoff_cap` are per-call overrides of
this client's instance defaults. Both get_rooms_overview() (the
snapshot's fail-fast budget, collector/config.py's DEFAULT_SNAPSHOT_*)
and get_room_page() (the message-room path's own budget,
DEFAULT_MESSAGE_*) use them -- RoomFollower and RoomsSnapshotter each
pass their own values. A bare TechnocoreClient call that omits them
(None, the default for both methods) falls back to this client's plain
instance-level constructor defaults, unaffected.
"""

import json
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

# A generous ceiling for these text/JSON endpoints -- real captured pages
# top out in the tens of KB even at the largest configured page limit --
# and a hard bound against ever buffering an unbounded amount of untrusted
# response data into memory for a long-running process.
DEFAULT_MAX_RESPONSE_BYTES = 20 * 1024 * 1024


class TransientFetchError(Exception):
    """A GET could not be completed and the caller should record a
    failure and move on, never crash the loop or block other rooms or the
    snapshot.

    This is the base class every "this fetch attempt failed" outcome
    raises, distinguished by subclass: connection errors, timeouts, or a
    retried-and-still-failing 5xx (this class directly), sustained 429
    rate limiting past max_429_retries (RateLimitExceeded), a non-2xx
    status this client never retries at all, e.g. 404/400/403
    (NonRetryableFetchError), or a response body that exceeded the size
    cap (ResponseTooLarge). RoomFollower and RoomsSnapshotter catch this
    base class, so any subclass is handled identically without them
    needing to know which one occurred.
    """

    def __init__(self, message, cause=None):
        super().__init__(message)
        self.cause = cause


class RateLimitExceeded(TransientFetchError):
    """The server kept returning 429 past max_429_retries."""


class NonRetryableFetchError(TransientFetchError):
    """A GET returned a non-2xx status this client does not retry at all
    (not 429, not one of the retryable 5xx statuses) -- e.g. 404, 400, 403.
    """


class ResponseTooLarge(TransientFetchError):
    """A response body exceeded the configured size cap before finishing.

    Refused outright rather than buffered: this collector reads from a
    public, untrusted network endpoint, and nothing about a 200 response
    guarantees its body is small just because the real service's pages
    normally are.
    """


class ReadDeadlineExceeded(TransientFetchError):
    """A response body was still being read past an explicit wall-clock
    deadline for that attempt.

    Only ever raised when a caller opts into a deadline (see
    _read_bounded_body's `deadline` param and _get()'s `max_attempts`
    override) -- both get_rooms_overview() (the snapshot) and
    get_room_page() (message rooms) opt in with their own budgets, via
    RoomsSnapshotter/RoomFollower; a bare TechnocoreClient call that
    doesn't pass max_attempts stays unbounded, as before this existed.
    requests' own `timeout` bounds any single blocking socket read, but
    explicitly NOT the sum of many reads while streaming a body (see
    requests' docs: "not a time limit on the entire response download");
    this is the second, independent bound that makes "this one attempt
    takes at most `timeout` seconds, total" actually true rather than
    assumed.
    """


def _clamp_wait(wait_s, cap):
    """Floor at 0, ceiling at `cap`. `wait_s` comes from an untrusted
    server response (a Retry-After header or body); never hand it to
    time.sleep() unclamped -- a negative value raises ValueError there,
    and an absurdly large one stalls this single-threaded client (and
    therefore the whole collection loop) for as long as the server claims.
    """
    if wait_s < 0:
        wait_s = 0.0
    if wait_s > cap:
        wait_s = cap
    return wait_s


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

    Not clamped here -- clamping is the caller's policy (it owns the cap),
    this function only reports what the response claimed.
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


def _read_bounded_body(resp, max_bytes, deadline=None):
    """Read resp's body up to max_bytes, raising ResponseTooLarge if it
    doesn't fit. `resp` must have been fetched with stream=True so nothing
    is buffered before this runs.

    `deadline`, if given, is an absolute time.monotonic() timestamp:
    still reading past it raises ReadDeadlineExceeded. None (the default)
    means no such check -- the message-room path never passes one, so
    this is a no-op there, identical to before this parameter existed.
    """
    chunks = []
    total = 0
    for chunk in resp.iter_content(chunk_size=65536):
        if deadline is not None and time.monotonic() > deadline:
            raise ReadDeadlineExceeded(
                f"response body still being read past its {deadline!r} deadline"
            )
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLarge(
                f"response body exceeded {max_bytes} bytes before finishing, refused"
            )
        chunks.append(chunk)
    return b"".join(chunks)


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
        max_response_bytes=DEFAULT_MAX_RESPONSE_BYTES,
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
        self.max_response_bytes = max_response_bytes
        self._sleep = sleep_fn

    def _get(self, path, params=None, timeout=None, max_attempts=None, backoff_cap=None):
        """`timeout`/`max_attempts`/`backoff_cap`, when given, override
        this client's instance-level defaults for this call only --
        both get_rooms_overview() (snapshot budget) and get_room_page()
        (message-room budget) use this. When all three are None (a bare
        TechnocoreClient call passing neither), behavior is unchanged
        from before these parameters existed: self.timeout,
        self.max_429_retries/max_transient_retries, self.transient_backoff_cap,
        and an unbounded (deadline=None) body read.
        """
        url = f"{self.base_url}{path}"
        effective_timeout = self.timeout if timeout is None else timeout
        effective_backoff_cap = self.transient_backoff_cap if backoff_cap is None else backoff_cap
        if max_attempts is None:
            max_429_retries = self.max_429_retries
            max_transient_retries = self.max_transient_retries
        else:
            # "N retries" means N+1 total attempts, the same convention
            # max_429_retries/max_transient_retries already use below.
            max_429_retries = max_attempts - 1
            max_transient_retries = max_attempts - 1

        attempts_429 = 0
        attempts_transient = 0
        backoff = self.transient_backoff_base
        while True:
            # Only set (non-None) when max_attempts was given -- both
            # RoomsSnapshotter and RoomFollower always give one (their
            # own budgets), so this is active for real collection calls;
            # a bare TechnocoreClient call that omits max_attempts still
            # gets an unbounded (deadline=None) body read, unchanged.
            attempt_deadline = (
                time.monotonic() + effective_timeout if max_attempts is not None else None
            )
            try:
                resp = self.session.get(
                    url, params=params, timeout=effective_timeout, stream=True
                )
            except requests.exceptions.RequestException as exc:
                attempts_transient += 1
                if attempts_transient > max_transient_retries:
                    raise TransientFetchError(
                        f"GET {url} failed after {attempts_transient} attempts "
                        f"(connection error/timeout): {exc}",
                        cause=exc,
                    ) from exc
                self._sleep(min(backoff, effective_backoff_cap))
                backoff *= 2
                continue

            try:
                body_bytes = _read_bounded_body(
                    resp, self.max_response_bytes, deadline=attempt_deadline
                )
            except ReadDeadlineExceeded as exc:
                attempts_transient += 1
                if attempts_transient > max_transient_retries:
                    raise TransientFetchError(
                        f"GET {url} failed after {attempts_transient} attempts "
                        f"(response body read exceeded its {effective_timeout}s deadline)",
                        cause=exc,
                    ) from exc
                self._sleep(min(backoff, effective_backoff_cap))
                backoff *= 2
                continue
            except requests.exceptions.RequestException as exc:
                attempts_transient += 1
                if attempts_transient > max_transient_retries:
                    raise TransientFetchError(
                        f"GET {url} failed after {attempts_transient} attempts "
                        f"(connection error/timeout reading response body): {exc}",
                        cause=exc,
                    ) from exc
                self._sleep(min(backoff, effective_backoff_cap))
                backoff *= 2
                continue
            finally:
                resp.close()

            if resp.status_code == 429:
                attempts_429 += 1
                if attempts_429 > max_429_retries:
                    raise RateLimitExceeded(
                        f"GET {url} failed after {attempts_429} attempts "
                        f"(HTTP 429, still rate limited)"
                    )
                try:
                    json_body = json.loads(body_bytes)
                except ValueError:
                    json_body = None
                wait_s = parse_wait_seconds(
                    headers=resp.headers,
                    body_text=body_bytes.decode("utf-8", errors="replace"),
                    json_body=json_body,
                )
                wait_s = _clamp_wait(wait_s, effective_backoff_cap)
                self._sleep(wait_s)
                continue

            if resp.status_code in _RETRYABLE_STATUS:
                attempts_transient += 1
                if attempts_transient > max_transient_retries:
                    raise TransientFetchError(
                        f"GET {url} failed after {attempts_transient} attempts "
                        f"(HTTP {resp.status_code})",
                        cause=None,
                    )
                self._sleep(min(backoff, effective_backoff_cap))
                backoff *= 2
                continue

            if resp.status_code >= 400:
                raise NonRetryableFetchError(
                    f"GET {url} returned HTTP {resp.status_code} (not retried)"
                )

            return json.loads(body_bytes)

    def get_rooms_overview(self, timeout=None, max_attempts=None, backoff_cap=None):
        """GET /rooms?format=json

        Accepts its own fail-fast budget, separate from the message-room
        instance defaults get_room_page() uses -- RoomsSnapshotter always
        passes one (collector/config.py's DEFAULT_SNAPSHOT_TIMEOUT/
        DEFAULT_SNAPSHOT_MAX_ATTEMPTS/DEFAULT_SNAPSHOT_BACKOFF_CAP): the
        whole-commons snapshot is best-effort context, and a slow /rooms
        must never be able to freeze the single-threaded loop long enough
        to cost unrecoverable message-room capture. See that module for
        the worst-case wall-clock these numbers produce.
        """
        return self._get(
            "/rooms",
            params={"format": "json"},
            timeout=timeout,
            max_attempts=max_attempts,
            backoff_cap=backoff_cap,
        )

    def get_room_page(
        self, room, since=0, wait=0, limit=None, timeout=None, max_attempts=None, backoff_cap=None
    ):
        """GET /r/<room>?since=<seq>&format=json

        `room` may be the literal room name, or "events" for the
        service-wide room-discovery log at /r/events, which has the
        identical page shape and is followed with a cursor the same way.

        `limit` caps the page size (confirmed live: /r/lobby?since=...&
        limit=200&format=json returns at most 200 messages, oldest-first,
        starting at first_seq == since+1 when the ring hasn't dropped
        anything). Followers use it to drain a backlog page by page
        instead of losing everything past the first `limit` messages.

        `timeout`/`max_attempts`/`backoff_cap` are the message-room
        path's own fail-fast budget, the same mechanism
        get_rooms_overview() uses for the snapshot -- RoomFollower
        always passes one (collector/config.py's DEFAULT_MESSAGE_*): a
        stalled fetch here (confirmed live, blocked reading a response
        from an overloaded /r/events) must not be able to freeze the
        loop long enough to lose a fast room's ring-buffer history. See
        that module for the worst-case wall-clock these numbers produce,
        reasoned against lobby's ~20s ring cycle.
        """
        params = {"since": since, "format": "json"}
        if wait:
            params["wait"] = wait
        if limit:
            params["limit"] = limit
        return self._get(
            f"/r/{room}",
            params=params,
            timeout=timeout,
            max_attempts=max_attempts,
            backoff_cap=backoff_cap,
        )
