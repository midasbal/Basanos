from dataclasses import dataclass, field
from typing import List, Optional

DEFAULT_BASE_URL = "https://technocore.chat"
DEFAULT_ROOMS = ("lobby",)
DEFAULT_DATA_DIR = "data"
DEFAULT_LONG_POLL_WAIT = 10
DEFAULT_USER_AGENT = "basanos-collector/0.1 (+read-only measurement collector)"

# Two independent cadences (increment 3), replacing the single shared
# `poll_interval` of increments 1-2. A fast, small-retention room like
# lobby needs draining every few seconds or it loses data to ring
# eviction; the whole-commons /rooms snapshot needs nowhere near that.
DEFAULT_MESSAGE_INTERVAL = 5.0
DEFAULT_SNAPSHOT_INTERVAL = 300.0


@dataclass
class Config:
    base_url: str = DEFAULT_BASE_URL
    message_interval: float = DEFAULT_MESSAGE_INTERVAL
    snapshot_interval: float = DEFAULT_SNAPSHOT_INTERVAL
    rooms: List[str] = field(default_factory=lambda: list(DEFAULT_ROOMS))
    data_dir: str = DEFAULT_DATA_DIR
    # Also unused by run_loop as of increment 3: message-cadence drains now
    # fetch with wait=0 always (the fixed message_interval is the pacing
    # mechanism, not server-side long-poll). Kept for run_once callers that
    # still want to pass an explicit wait, and for backward-compat
    # construction.
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
