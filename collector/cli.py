"""Command-line entry point.

    python -m collector --once
    python -m collector --rooms lobby meta --message-interval 5 --snapshot-interval 300

Read-only: only ever performs GET requests against the configured base
URL. The continuous loop (the default when --once isn't passed) is meant
to be started by the operator against the live service; it is never
invoked automatically by tests or this module's own build/import.
"""

import argparse
import sys

from .config import (
    Config,
    DEFAULT_BASE_URL,
    DEFAULT_DATA_DIR,
    DEFAULT_LONG_POLL_WAIT,
    DEFAULT_MESSAGE_INTERVAL,
    DEFAULT_ROOMS,
    DEFAULT_SNAPSHOT_INTERVAL,
)
from .core import Collector
from .http_client import TechnocoreClient


def build_arg_parser():
    p = argparse.ArgumentParser(description="Basanos v1 read-only Technocore collector")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Technocore base URL")
    p.add_argument(
        "--message-interval",
        type=float,
        default=DEFAULT_MESSAGE_INTERVAL,
        help="seconds between message-room/events drains in loop mode",
    )
    p.add_argument(
        "--snapshot-interval",
        type=float,
        default=DEFAULT_SNAPSHOT_INTERVAL,
        help="seconds between /rooms snapshots in loop mode",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=None,
        help="[deprecated] alias for --snapshot-interval, kept for old scripts",
    )
    p.add_argument(
        "--rooms",
        nargs="*",
        default=list(DEFAULT_ROOMS),
        help="room names to follow message-by-message",
    )
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="output directory")
    p.add_argument(
        "--long-poll-wait",
        type=int,
        default=DEFAULT_LONG_POLL_WAIT,
        help="[deprecated, unused by loop mode as of increment 3] wait= for --once",
    )
    p.add_argument("--once", action="store_true", help="single collection pass, then exit")
    return p


def main(argv=None):
    args = build_arg_parser().parse_args(argv)

    snapshot_interval = args.snapshot_interval
    if args.interval is not None:
        print(
            f"--interval is deprecated; mapping --interval {args.interval} "
            f"to --snapshot-interval (message drains now run on their own "
            f"--message-interval, default {DEFAULT_MESSAGE_INTERVAL}s)"
        )
        snapshot_interval = args.interval

    config = Config(
        base_url=args.base_url,
        message_interval=args.message_interval,
        snapshot_interval=snapshot_interval,
        rooms=args.rooms,
        data_dir=args.data_dir,
        long_poll_wait=args.long_poll_wait,
    )
    client = TechnocoreClient(config.base_url, user_agent=config.user_agent)
    collector = Collector(client, config)

    if args.once:
        results = collector.run_once(wait=0)
        snap = results["snapshot"]
        snap_summary = "snapshot failed" if snap["failed"] else "snapshot captured"

        def follower_summary(r):
            if r["failed"]:
                return f"{r['room']} FAILED: {r['error']}"
            flags = []
            if r["gap"]:
                flags.append("gap")
            if r["capped"]:
                flags.append("drain-capped")
            flag_str = f" ({', '.join(flags)})" if flags else ""
            return f"{r['room']} +{r['new_count']} in {r['pages_fetched']} page(s){flag_str}"

        print(
            snap_summary + ";",
            "events:", follower_summary(results["events"]) + ";",
            "; ".join(follower_summary(r) for r in results["rooms"]),
        )
        return 0

    print(
        f"starting continuous collection against {config.base_url} "
        f"(message_interval={config.message_interval}s, "
        f"snapshot_interval={config.snapshot_interval}s, rooms={config.rooms}); Ctrl-C to stop"
    )
    try:
        collector.run_loop()
    except KeyboardInterrupt:
        print("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
