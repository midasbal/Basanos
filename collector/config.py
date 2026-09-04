from dataclasses import dataclass, field
from typing import List, Optional

DEFAULT_BASE_URL = "https://technocore.chat"
DEFAULT_ROOMS = ("lobby",)
DEFAULT_DATA_DIR = "data"
DEFAULT_LONG_POLL_WAIT = 0
DEFAULT_USER_AGENT = "basanos-collector/0.1 (+read-only measurement collector)"

# Two independent cadences (increment 3), replacing the single shared
# `poll_interval` of increments 1-2. A fast, small-retention room like
# lobby needs draining every few seconds or it loses data to ring
# eviction; the whole-commons /rooms snapshot needs nowhere near that.
DEFAULT_MESSAGE_INTERVAL = 5.0
DEFAULT_SNAPSHOT_INTERVAL = 300.0

# The whole-commons /rooms snapshot's OWN fail-fast budget, deliberately
# separate from the message-room fetch defaults in TechnocoreClient
# (timeout=30, effectively 6 attempts once max_transient_retries=5 is
# counted). Confirmed live: a slow /rooms under load burned that full
# budget -- ~6 attempts x 30s, up to ~180s of HTTP time alone, plus
# exponential-backoff sleeps between them -- and froze the whole
# single-threaded loop for minutes, during which lobby (a fast,
# small-retention room) lost traffic permanently to ring eviction. The
# snapshot is best-effort context; that loss is not recoverable, so it
# must never be able to starve message capture.
#
# Worst-case wall-clock this collector can spend on ONE snapshot() call
# under these defaults, provable from the numbers alone (see
# TechnocoreClient._get()'s use of these as timeout/max_attempts/
# backoff_cap overrides, and _read_bounded_body's deadline enforcement,
# which independently bounds a single attempt's total body-read time,
# not just the per-socket-read gap requests' own `timeout` covers):
#   DEFAULT_SNAPSHOT_MAX_ATTEMPTS attempts, each bounded at
#   DEFAULT_SNAPSHOT_TIMEOUT seconds, plus (attempts - 1) inter-attempt
#   sleeps each bounded at DEFAULT_SNAPSHOT_BACKOFF_CAP seconds:
#   2 * 4.0 + 1 * 1.0 = 9.0 seconds, worst case: single-digit seconds,
#   two orders of magnitude below the ~180s+ this replaces, and provably
#   bounded rather than open-ended.
DEFAULT_SNAPSHOT_TIMEOUT = 4.0
DEFAULT_SNAPSHOT_MAX_ATTEMPTS = 2
DEFAULT_SNAPSHOT_BACKOFF_CAP = 1.0

# The message-room fetch path's own fail-fast budget -- the same idea as
# the snapshot's, applied one layer down after a live incident: with the
# client's original defaults (timeout=30, max_transient_retries=5, i.e. 6
# total attempts), a single stalled get_room_page() call (confirmed live,
# blocked inside conn.getresponse() on /r/events) could burn the same
# ~180s+ the snapshot fix was built to avoid. This is not adversarial --
# ordinary service slowness under load produces it -- and unlike the
# snapshot, it is not recoverable context: it is lobby's actual traffic,
# permanently evicted from the ring while the loop is frozen.
#
# lobby's ring holds roughly window=200 messages and was observed live
# cycling in ~20s under load (see collector/core.py's DEFAULT_PAGE_LIMIT
# comment: ~114 new messages in ~11.6s between two live snapshots, i.e.
# close to 10 msgs/s -> a 200-message window turns over in ~20s). Any
# single stalled fetch that blocks anywhere near that long risks losing
# a full cycle's worth of lobby traffic outright; the budget below is
# chosen to stay comfortably under it.
#
# Kept more resilient than the snapshot on purpose -- message data is the
# thing this whole project measures, the snapshot is just best-effort
# context -- more attempts, but each bounded tightly enough that the
# worst case for ONE fetch still fits well inside one ring cycle.
#
# Worst case for a single get_room_page() call under these defaults,
# provable the same way the snapshot's is (see TechnocoreClient._get()
# and _read_bounded_body's deadline enforcement):
#   DEFAULT_MESSAGE_MAX_ATTEMPTS attempts, each bounded at
#   DEFAULT_MESSAGE_TIMEOUT seconds, plus (attempts - 1) inter-attempt
#   sleeps each bounded at DEFAULT_MESSAGE_BACKOFF_CAP seconds:
#   4 * 2.5 + 3 * 1.0 = 13.0 seconds -- about two thirds of lobby's ~20s
#   ring cycle (real margin, not a photo finish), twice the snapshot's
#   attempt count (more resilient, as intended), and well over an order
#   of magnitude below the ~180s+ this replaces.
#
# A single drain pass only pays this once per room per pass: a page
# fetch that ultimately fails breaks RoomFollower's drain loop
# immediately (it does not retry per-page on top of this), so this is
# the bound on one get_room_page() call, not a multiple of it within one
# room's fetch_and_store(). Multiple *independent* followers (events plus
# each configured room) can each hit their own worst case within the
# same pass; with the default single room this is at most two such
# blocks (events, lobby) in the pathological case where both stall.
#
# These are starting defaults, not a final tuning -- deliberately made
# configurable via Config/CLI (the same way the snapshot's are) since the
# right numbers depend on the real network path to the service, which is
# what gets tuned once this runs somewhere other than a local machine.
DEFAULT_MESSAGE_TIMEOUT = 2.5
DEFAULT_MESSAGE_MAX_ATTEMPTS = 4
DEFAULT_MESSAGE_BACKOFF_CAP = 1.0


@dataclass
class Config:
    base_url: str = DEFAULT_BASE_URL
    message_interval: float = DEFAULT_MESSAGE_INTERVAL
    snapshot_interval: float = DEFAULT_SNAPSHOT_INTERVAL
    # The snapshot's own fail-fast HTTP budget -- see the block comment
    # above. Deliberately separate from TechnocoreClient's message-room
    # defaults (timeout/max_429_retries/max_transient_retries), which are
    # unaffected by these.
    snapshot_timeout: float = DEFAULT_SNAPSHOT_TIMEOUT
    snapshot_max_attempts: int = DEFAULT_SNAPSHOT_MAX_ATTEMPTS
    snapshot_backoff_cap: float = DEFAULT_SNAPSHOT_BACKOFF_CAP
    # The message-room fetch path's own fail-fast budget -- see the block
    # comment above. Applied to every RoomFollower (both the configured
    # message rooms and the events log), via get_room_page()'s own
    # timeout/max_attempts/backoff_cap overrides -- the identical pattern
    # the snapshot fields above use for get_rooms_overview().
    message_timeout: float = DEFAULT_MESSAGE_TIMEOUT
    message_max_attempts: int = DEFAULT_MESSAGE_MAX_ATTEMPTS
    message_backoff_cap: float = DEFAULT_MESSAGE_BACKOFF_CAP
    rooms: List[str] = field(default_factory=lambda: list(DEFAULT_ROOMS))
    data_dir: str = DEFAULT_DATA_DIR
    # Unused by run_loop as of increment 3: message-cadence drains now
    # fetch with wait=0 always (the fixed message_interval is the pacing
    # mechanism, not server-side long-poll). cli.py's --once path passes
    # this straight into Collector.run_once(wait=...), so --long-poll-wait
    # controls the server-side long-poll on the first page of that one-shot
    # pass. Default is 0 (no long-poll), matching --once's own long-standing
    # default of an immediate, non-blocking pass -- this field was
    # previously accepted but silently never read by --once, so the
    # default here is chosen to match that already-observed behavior,
    # not to change it.
    long_poll_wait: int = DEFAULT_LONG_POLL_WAIT
    user_agent: str = DEFAULT_USER_AGENT
    # Deprecated (increment 3): run_loop used one shared interval for both
    # the snapshot and every room/events drain. Replaced by
    # message_interval/snapshot_interval above. Kept as an accepted-but-
    # unused field purely so old code that still does
    # Config(poll_interval=...) doesn't break at construction time; nothing
    # in this package reads it anymore. cli.py maps the old --interval flag
    # onto --snapshot-interval instead of onto this field, since that was
    # the closer match to what --interval actually paced before (see
    # run_loop's old docstring: "poll_interval paces the whole-commons
    # /rooms snapshot").
    poll_interval: Optional[float] = None
