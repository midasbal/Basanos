"""Activity curve over time: the fourth measurement in the Basanos
measurement layer.

Read-only by construction: this module only reads a room's already-stored
`<data-dir>/rooms/<room>/messages.jsonl` and `<data-dir>/coverage.jsonl`.
It never writes to, or modifies, anything under `<data-dir>` except the
analysis output this module produces itself.

WHY TWO CURVES, NOT ONE: captured-count alone is a biased picture of
activity over time, because the collector loses more to ring eviction
exactly when traffic peaks (a fast room's ring turns over faster under
load, so a burst is exactly when the collector is most likely to fall
behind and drop a stretch it never got to read). Reporting captured alone
would flatten or even invert a real peak into a trough. So this module
reports two curves per time bin instead:

- captured: re-verified signed posts per bin -- the FLOOR, what was
  actually seen.
- estimated throughput: captured + estimated dropped per bin -- the
  collector's best estimate of true activity in that bin, using the
  ring-eviction counts `collector/coverage.py`'s CoverageTracker already
  records.

Per-bin coverage (captured / (captured + estimated dropped)) sits between
them, so a dip in the captured curve reads as either real quiet or a
capture gap, never silently as one or the other.

TWO DATA SOURCES, JOINED APPROXIMATELY: messages are binned by their
server `ts`; coverage deltas (see `_difference_coverage_records` below)
are binned by the collector's own `captured_at` on the periodic snapshot
that recorded them. These are two different clocks that normally track
each other closely, but diverge more right around a collector restart --
so per-bin coverage here is an APPROXIMATE join, stated as such in every
report this module produces, never presented as an exact reconciliation.

This module also produces the room-activity baseline (the captured and
estimated-throughput curves) a later revision of `analysis/synchrony.py`
is expected to consume, to normalize timing-synchrony findings against
overall room activity rather than treating every hour of the window as
equally likely to have carried a post in the first place. That
consumption does not exist yet; this module only produces the baseline.

Deliberately out of scope for v1: any claim about a specific timezone or
what a "day" means to a human population (everything here is plotted on
the UTC clock the server itself uses, with the shape left to speak for
itself), and any per-identity output -- every number below is a per-bin
or aggregate count, never a name.

Usage:
    python -m analysis.diurnal --data-dir <dir> [--room lobby] \\
        [--bucket-seconds 3600] [--out <path>]
"""

import argparse
import json
import os
from datetime import datetime, timezone

from collector.verify import MalformedRecord, UnsupportedKeyType, is_signed, verify_record

DEFAULT_BUCKET_SECONDS = 3600.0

CAVEAT = (
    "some rooms may intend heartbeat-style posting, so this is a statement "
    "about the shape of the traffic, not a verdict about any poster."
)

WINDOW_CAVEAT = (
    "a window of roughly a day or two is barely more than one cycle -- this is an "
    "activity curve, not yet a confirmed diurnal cycle, which needs several "
    "continuous days to distinguish a repeating pattern from a single quiet stretch."
)

JOIN_CAVEAT = (
    "messages are binned by server ts, coverage deltas by the collector's own "
    "captured_at -- these are two different clocks that normally track each other "
    "closely but diverge more right around a collector restart, so per-bin coverage "
    "here is an APPROXIMATE join, not an exact reconciliation."
)


def _iter_json_lines(path):
    """Stream a JSONL file one record at a time. Never loads the whole
    file into memory -- callers build only the aggregates they need as
    they go. A line that isn't valid JSON is skipped (tallied by the
    caller if it cares), never a crash.
    """
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _parse_ts_seconds(ts):
    """Parse a stored timestamp (ISO-8601 UTC, e.g.
    "2026-09-02T15:36:03.288522Z") into epoch seconds (a float).

    Returns None, never raises, on anything that isn't a parseable
    timestamp -- a missing or malformed timestamp costs that one record
    its place in the curve, not a crash. The trailing "Z" is normalized to
    "+00:00" by hand rather than relying on `datetime.fromisoformat`'s own
    "Z" support, which only exists from Python 3.11 -- this project's
    floor is 3.9.
    """
    if not isinstance(ts, str) or not ts:
        return None
    normalized = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def _iso_from_seconds(epoch_seconds):
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _read_room_coverage_records(coverage_path, room):
    """Stream `<data_dir>/coverage.jsonl` and collect this room's periodic
    cumulative snapshots as (captured_at_seconds, captured_total,
    dropped_total) tuples, sorted by captured_at.

    A line that isn't a dict, or a record for this room missing a
    parseable captured_at or integer captured_total/dropped_total, is
    skipped and tallied as malformed rather than crashing this pass --
    the same "never crash on a malformed record" discipline the other
    modules apply to messages.jsonl.
    """
    records = []
    malformed = 0
    for rec in _iter_json_lines(coverage_path):
        if not isinstance(rec, dict):
            malformed += 1
            continue
        if rec.get("room") != room:
            continue
        captured_at = _parse_ts_seconds(rec.get("captured_at"))
        captured_total = rec.get("captured_total")
        dropped_total = rec.get("dropped_total")
        if (
            captured_at is None
            or not isinstance(captured_total, int)
            or not isinstance(dropped_total, int)
        ):
            malformed += 1
            continue
        records.append((captured_at, captured_total, dropped_total))
    records.sort(key=lambda r: r[0])
    return records, malformed


def _difference_coverage_records(records):
    """Turn `room`'s sorted cumulative coverage snapshots into per-interval
    deltas: a list of (interval_captured_at_seconds, captured_delta,
    dropped_delta), one per pair of consecutive snapshots, using the LATER
    snapshot's captured_at as the interval's own timestamp (see the module
    docstring's join caveat).

    Cumulative counters reset to (0, 0) on every collector restart (see
    `collector.coverage.CoverageTracker`), so a negative delta in either
    counter means a restart happened somewhere between these two
    snapshots -- that interval's deltas describe a reset, not a drop in
    activity, and are meaningless as a delta. Such an interval is skipped
    entirely: never added as zero, never clamped to a positive guess, just
    excluded, with the count of skipped intervals returned separately so
    it is never silently absorbed into the totals.
    """
    intervals = []
    restarts_skipped = 0
    for (_, c0, d0), (t1, c1, d1) in zip(records, records[1:]):
        captured_delta = c1 - c0
        dropped_delta = d1 - d0
        if captured_delta < 0 or dropped_delta < 0:
            restarts_skipped += 1
            continue
        intervals.append((t1, captured_delta, dropped_delta))
    return intervals, restarts_skipped


def _bin_index(ts, earliest, bucket_seconds, bin_count):
    """Bin index for `ts` within [earliest, earliest + bin_count *
    bucket_seconds), clamped into range. Clamping only matters for a
    coverage interval that falls slightly outside the message-defined
    window (see compute_diurnal_stats's window comment) -- ordinary
    messages never need it, since the window's own latest bound is always
    at least the latest message ts.
    """
    idx = int((ts - earliest) // bucket_seconds)
    return max(0, min(idx, bin_count - 1))


def compute_diurnal_stats(data_dir, room="lobby", bucket_seconds=DEFAULT_BUCKET_SECONDS):
    """Stream `<data_dir>/rooms/<room>/messages.jsonl` and
    `<data_dir>/coverage.jsonl` and compute the room's activity curve:
    captured posts and estimated dropped posts per absolute-time bin,
    plus the aggregate shape of the curve.

    Returns a dict with the raw counters and aggregates needed by both the
    human-readable report and the JSON output. Reads only; writes nothing.
    No did:key string appears anywhere in the returned structure -- keys
    are only ever counted, never named.
    """
    messages_path = os.path.join(data_dir, "rooms", room, "messages.jsonl")
    coverage_path = os.path.join(data_dir, "coverage.jsonl")

    checked = 0
    verified = 0
    failed = 0
    malformed_lines = 0
    ts_unparsable = 0
    ts_values = []

    messages_found = os.path.exists(messages_path)
    if messages_found:
        for record in _iter_json_lines(messages_path):
            if not isinstance(record, dict):
                malformed_lines += 1
                continue
            if not is_signed(record):
                continue  # unsigned nicks are excluded from the population entirely
            checked += 1
            try:
                ok = verify_record(record)
            except (UnsupportedKeyType, MalformedRecord, KeyError):
                ok = False
            if not ok:
                failed += 1
                continue
            verified += 1
            parsed_ts = _parse_ts_seconds(record.get("ts"))
            if parsed_ts is None:
                ts_unparsable += 1
            else:
                ts_values.append(parsed_ts)

    coverage_found = os.path.exists(coverage_path)
    coverage_malformed = 0
    intervals = []
    restarts_skipped = 0
    if coverage_found:
        coverage_records, coverage_malformed = _read_room_coverage_records(coverage_path, room)
        intervals, restarts_skipped = _difference_coverage_records(coverage_records)

    bins = []
    if ts_values:
        # window_earliest_ts is defined strictly from re-verified signed
        # posts (the spec this module implements); window_latest_ts
        # extends far enough to also cover the latest coverage interval,
        # so a drop recorded slightly after the last captured message is
        # never silently discarded from the curve.
        earliest = min(ts_values)
        latest = max(ts_values)
        if intervals:
            latest = max(latest, max(t for t, _, _ in intervals))

        bin_count = int((latest - earliest) // bucket_seconds) + 1

        captured_counts = [0] * bin_count
        for ts in ts_values:
            captured_counts[_bin_index(ts, earliest, bucket_seconds, bin_count)] += 1

        dropped_counts = [0] * bin_count
        coverage_captured_crosscheck = [0] * bin_count
        for interval_ts, captured_delta, dropped_delta in intervals:
            idx = _bin_index(interval_ts, earliest, bucket_seconds, bin_count)
            dropped_counts[idx] += dropped_delta
            coverage_captured_crosscheck[idx] += captured_delta

        for i in range(bin_count):
            captured = captured_counts[i]
            dropped = dropped_counts[i]
            throughput = captured + dropped
            coverage_ratio = (captured / throughput) if throughput else None
            bins.append(
                {
                    "bin_index": i,
                    "bin_start_ts": _iso_from_seconds(earliest + i * bucket_seconds),
                    "captured_posts": captured,
                    "estimated_dropped": dropped,
                    "estimated_throughput": throughput,
                    "coverage_ratio": coverage_ratio,
                    # A cross-check only: the coverage counters' own captured
                    # delta for this bin, expected to differ slightly from
                    # `captured_posts` (a different clock, see the join
                    # caveat), not an error when it does.
                    "coverage_captured_crosscheck": coverage_captured_crosscheck[i],
                }
            )

    total_captured = sum(b["captured_posts"] for b in bins)
    total_estimated_dropped = sum(b["estimated_dropped"] for b in bins)
    overall_denominator = total_captured + total_estimated_dropped
    overall_coverage_ratio = (total_captured / overall_denominator) if overall_denominator else None

    non_empty_captured = [b["captured_posts"] for b in bins if b["captured_posts"] > 0]
    if non_empty_captured:
        max_captured = max(non_empty_captured)
        min_captured = min(non_empty_captured)
        shape_ratio = max_captured / min_captured
    else:
        max_captured = None
        min_captured = None
        shape_ratio = None

    window_span_seconds = (max(ts_values) - min(ts_values)) if ts_values else None

    return {
        "room": room,
        "bucket_seconds": bucket_seconds,
        "messages_file_found": messages_found,
        "coverage_file_found": coverage_found,
        "signed_checked": checked,
        "signed_reverified": verified,
        "signed_reverify_failed": failed,
        "malformed_lines_skipped": malformed_lines,
        "ts_unparsable_skipped": ts_unparsable,
        "coverage_malformed_lines_skipped": coverage_malformed,
        "restart_intervals_skipped": restarts_skipped,
        "bins": bins,
        "num_bins": len(bins),
        "window_span_seconds": window_span_seconds,
        "total_captured_posts": total_captured,
        "total_estimated_dropped": total_estimated_dropped,
        "overall_coverage_ratio": overall_coverage_ratio,
        "max_captured_in_a_bin": max_captured,
        "min_captured_in_a_bin": min_captured,
        "captured_shape_ratio": shape_ratio,
    }


def format_report(stats):
    """Render the human-readable report for `stats` (as returned by
    `compute_diurnal_stats`).

    The captured curve is a FLOOR (what was actually seen); the estimated
    throughput curve adds the ring-eviction drops the collector itself
    recorded, an ESTIMATE of true activity, not a second ground truth.
    Coverage is stated at every level (per bin and overall) so a dip in
    the captured curve reads as either real quiet or a capture gap, never
    silently as one or the other. The message/coverage join is
    approximate (see the module docstring); the window is UTC only, never
    claimed to be any particular local time or "human" pattern; and no
    individual DID is ever named.
    """
    room = stats["room"]
    bucket_seconds = stats["bucket_seconds"]
    lines = []
    lines.append(f"Activity curve -- room: {room}")
    lines.append("=" * (19 + len(room)))
    lines.append("")

    if not stats["messages_file_found"]:
        lines.append(f"No messages.jsonl found for room {room!r}; nothing to measure.")
        return "\n".join(lines)

    lines.append(
        f"Re-verify stats: {stats['signed_checked']} signed messages checked, "
        f"{stats['signed_reverified']} re-verified, "
        f"{stats['signed_reverify_failed']} failed to re-verify."
    )
    lines.append(
        "(Every curve below rests on re-verified signatures only, not trusted stored ones.)"
    )
    lines.append("")

    if stats["signed_reverified"] == 0:
        lines.append("No re-verified signed messages in this window -- nothing to report.")
        return "\n".join(lines)

    if not stats["coverage_file_found"]:
        lines.append(
            "No coverage.jsonl found for this data dir -- captured curve only, no "
            "estimated-dropped curve, no coverage ratio."
        )
        lines.append("")

    lines.append(
        f"Bucket width: {bucket_seconds:g}s, {stats['num_bins']} bin(s) across a "
        f"{stats['window_span_seconds']:.0f}s window (UTC clock; no timezone or "
        f"human-pattern claim is made -- the shape speaks for itself)."
    )
    if stats["restart_intervals_skipped"]:
        lines.append(
            f"  {stats['restart_intervals_skipped']} coverage interval(s) spanned a "
            f"collector restart (a cumulative counter reset) and were skipped rather "
            f"than counted as a drop in activity."
        )
    lines.append("")

    lines.append("Per-bin curve (captured floor / estimated throughput / coverage):")
    for b in stats["bins"]:
        ratio_str = f"{b['coverage_ratio']:.3f}" if b["coverage_ratio"] is not None else "n/a"
        lines.append(
            f"  [{b['bin_index']}] {b['bin_start_ts']}  captured={b['captured_posts']} "
            f"estimated_dropped={b['estimated_dropped']} throughput={b['estimated_throughput']} "
            f"coverage={ratio_str}"
        )
    lines.append("")

    overall_ratio = stats["overall_coverage_ratio"]
    overall_ratio_str = f"{overall_ratio:.4f}" if overall_ratio is not None else "n/a"
    lines.append("Aggregate:")
    lines.append(f"  total captured posts:      {stats['total_captured_posts']}")
    lines.append(f"  total estimated dropped:   {stats['total_estimated_dropped']}")
    lines.append(f"  overall coverage ratio:    {overall_ratio_str}")
    if stats["captured_shape_ratio"] is not None:
        lines.append(
            f"  captured curve shape: min={stats['min_captured_in_a_bin']}, "
            f"max={stats['max_captured_in_a_bin']} in a non-empty bin, "
            f"ratio={stats['captured_shape_ratio']:.2f}x (near 1x is flat, a large "
            f"ratio is a strong peak/dip in the captured curve)."
        )
    else:
        lines.append("  captured curve shape: no non-empty bin -- nothing to compare.")
    lines.append("")

    lines.append(
        "This is a FLOOR on the captured curve: it is what was actually seen, at the "
        "coverage stated above; the estimated-throughput curve adds the collector's own "
        "recorded ring-eviction drops as a best estimate, not a second ground truth."
    )
    lines.append(f"Join caveat: {JOIN_CAVEAT}")
    lines.append(f"Window caveat: {WINDOW_CAVEAT}")
    lines.append("")
    lines.append(f"Caveat: {CAVEAT}")

    if stats["malformed_lines_skipped"] or stats["ts_unparsable_skipped"] or stats["coverage_malformed_lines_skipped"]:
        lines.append("")
        if stats["malformed_lines_skipped"]:
            lines.append(
                f"Note: {stats['malformed_lines_skipped']} unparseable line(s) in "
                "messages.jsonl were skipped."
            )
        if stats["ts_unparsable_skipped"]:
            lines.append(
                f"Note: {stats['ts_unparsable_skipped']} re-verified post(s) had a "
                "missing or unparseable ts and were skipped from the curve."
            )
        if stats["coverage_malformed_lines_skipped"]:
            lines.append(
                f"Note: {stats['coverage_malformed_lines_skipped']} unparseable "
                "record(s) in coverage.jsonl were skipped."
            )

    return "\n".join(lines)


def default_out_path(data_dir, room):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return os.path.join(data_dir, "analysis", f"diurnal_{room}_{ts}.json")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Room activity curve over absolute time, bounded by coverage "
        "(read-only)."
    )
    parser.add_argument("--data-dir", required=True, help="collector data directory to read")
    parser.add_argument("--room", default="lobby", help="room to analyze (default: lobby)")
    parser.add_argument(
        "--bucket-seconds",
        type=float,
        default=DEFAULT_BUCKET_SECONDS,
        help=f"time bucket width in seconds (default: {DEFAULT_BUCKET_SECONDS})",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="path to write the JSON report to "
        "(default: <data-dir>/analysis/diurnal_<room>_<ts>.json)",
    )
    args = parser.parse_args(argv)

    stats = compute_diurnal_stats(args.data_dir, room=args.room, bucket_seconds=args.bucket_seconds)
    print(format_report(stats))

    out_path = args.out or default_out_path(args.data_dir, args.room)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
