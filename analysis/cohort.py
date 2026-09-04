"""Cohort persistence: the seventh measurement in the Basanos measurement
layer.

Read-only by construction: this module only reads a room's already-stored
`<data-dir>/rooms/<room>/messages.jsonl` and `<data-dir>/coverage.jsonl`.
It never writes to, or modifies, anything under `<data-dir>` except the
analysis output this module produces itself.

THE MEASUREMENT: two time windows on the same continuous capture, in
order and separated by a gap, all defined by server `ts`:

    window 1 (the cohort window):  [w1_start, w1_end)
    gap:                           [w1_end, w2_start)   -- no window here
    window 2 (the return window):  [w2_start, w2_end)

The cohort is every key whose FIRST-EVER appearance in the whole file
(its earliest re-verified `ts`, not just within window 1) falls inside
window 1. PERSISTENCE is the fraction of that cohort seen again at least
once in window 2. This upgrades `analysis/diversity.py`'s one-and-done
finding ("posted once within one window") toward "genuinely single-use
across time, with a real gap in between."

THE ANTI-CONFOUND, THE WHOLE POINT OF THIS MODULE: cohort membership
requires the key's EARLIEST ts anywhere in the file to fall in window 1,
not merely that the key posted at all during window 1. A key that was
already active before window 1 started is not a new key in window 1 --
counting it anyway would make this an "active in each half" comparison,
which is activity-weighted and biased toward whatever core of persistent
keys already exists, not a measurement of what happens to NEW keys after
they first show up. Excluding pre-window-1 keys from the cohort is what
keeps this a genuine cohort study rather than a repeat-rate on the wrong
population.

COVERAGE HONESTY: a cohort key that truly returned in window 2 could be
missed if window 2 itself was under-captured, which would overcount
non-return -- the exact inverse of `analysis/diversity.py`'s own control
(there, under-capture of a single window inflated the one-and-done rate;
here, under-capture of window 2 specifically inflates the appearance of
non-return). So window 2's own coverage is computed the same way
`analysis/diurnal.py` and `analysis/diversity.py` compute coverage --
reading `<data-dir>/coverage.jsonl`, differencing this room's consecutive
cumulative snapshots, treating a negative delta as a collector restart and
skipping that interval -- and reported beside the persistence number, so
a reader can weigh how much to trust it. The non-return rate is a FLOOR
only to the extent window 2's own capture is complete.

RETURN IS BOUNDED TO WINDOW 2: a cohort key absent from window 2 could
still have returned at some later time outside window 2 entirely --
"did not return in window 2" is not "never posted again." This is the
same honest bound `analysis/diversity.py`'s one-and-done rate carries
(bounded to its one window); here it applies to the return side of a
two-window comparison instead.

This module does not import from or modify `analysis/diversity.py`,
`analysis/diurnal.py`, or any other sibling (each intentionally duplicates
its own small streaming/re-verify walk and its own coverage-differencing,
to keep every module a single self-contained read). It measures no
per-text property at all -- window bounds and key presence only. Every
number below is a count or a rate over keys, never a name.

Usage:
    python -m analysis.cohort --data-dir <dir> [--room lobby] \\
        --w1-start <ISO> --w1-end <ISO> --w2-start <ISO> --w2-end <ISO> [--out <path>]
"""

import argparse
import json
import os
import re
from datetime import datetime, timezone

from collector.verify import MalformedRecord, UnsupportedKeyType, is_signed, verify_record

CAVEAT = (
    "some rooms may intend heartbeat-style posting, so this is a statement "
    "about the shape of the traffic, not a verdict about any poster."
)

RETURN_CAVEAT = (
    "return is bounded to window 2: a cohort key absent from window 2 could still "
    "return later, outside window 2 entirely -- this measures return within the "
    "observed window 2, not \"never posted again\"."
)

COVERAGE_CAVEAT = (
    "the non-return rate is a FLOOR only to the extent window 2's own capture is "
    "complete -- an under-captured window 2 would miss a real return and inflate "
    "non-return, the inverse of how under-capture inflates a single-window "
    "one-and-done rate."
)


_VALID_ROOM_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_room(room):
    """Reject a room name that could escape the intended directory when
    used in os.path.join (below, and in default_out_path) -- a room
    containing "/" or ".." would let --room build a path outside
    <data-dir>/rooms/ on read or outside <data-dir>/analysis/ on write.
    Every real room name (lobby, meta, fixture-room-... in the fixtures)
    matches this pattern; nothing valid is rejected. Raised before any
    path is built or any file is opened or created.
    """
    if not _VALID_ROOM_RE.match(room):
        raise ValueError(
            f"invalid room {room!r}: must match {_VALID_ROOM_RE.pattern} "
            "(letters, digits, underscore, hyphen only)"
        )


def _validate_windows(w1_start, w1_end, w2_start, w2_end):
    """Reject window bounds that are not a real, ordered, separated pair:
    window 1 must be non-empty and end no later than window 2 starts (a
    real gap, possibly zero-width but never negative), and window 2 must
    itself be non-empty. Raised before any path is built or any record is
    read.
    """
    if not (w1_start < w1_end <= w2_start < w2_end):
        raise ValueError(
            f"invalid windows: need w1_start < w1_end <= w2_start < w2_end, got "
            f"w1=[{w1_start}, {w1_end}), w2=[{w2_start}, {w2_end})"
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
    its place in this analysis, not a crash. The trailing "Z" is
    normalized to "+00:00" by hand rather than relying on
    `datetime.fromisoformat`'s own "Z" support, which only exists from
    Python 3.11 -- this project's floor is 3.9.
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

    Duplicated from `analysis/diurnal.py`'s function of the same name and
    behavior, per this project's established convention of each module
    owning its own self-contained read rather than sharing a helper.

    A line that isn't a dict, or a record for this room missing a
    parseable captured_at or integer captured_total/dropped_total, is
    skipped and tallied as malformed rather than crashing this pass.
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
    snapshot's captured_at as the interval's own timestamp.

    Duplicated from `analysis/diurnal.py`'s function of the same name and
    behavior. Cumulative counters reset to (0, 0) on every collector
    restart (see `collector.coverage.CoverageTracker`), so a negative
    delta in either counter means a restart happened somewhere between
    these two snapshots -- that interval's deltas describe a reset, not a
    drop in activity, and are meaningless as a delta. Such an interval is
    skipped entirely: never added as zero, never clamped to a positive
    guess, just excluded, with the count of skipped intervals returned
    separately so it is never silently absorbed into the totals.
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


def compute_cohort_stats(data_dir, room, w1_start, w1_end, w2_start, w2_end):
    """Stream `<data_dir>/rooms/<room>/messages.jsonl` and
    `<data_dir>/coverage.jsonl` and compute cohort persistence: of the
    keys whose first-ever appearance falls in window 1, the fraction that
    also appear at least once in window 2.

    `w1_start`, `w1_end`, `w2_start`, `w2_end` are epoch seconds (floats),
    already parsed from ISO-8601 UTC by the caller (`main`, below, via
    `_parse_ts_seconds`). Returns a dict with the raw counters and
    aggregates needed by both the human-readable report and the JSON
    output. Reads only; writes nothing. No did:key string appears
    anywhere in the returned structure -- keys are only ever counted,
    never named.
    """
    _validate_room(room)
    _validate_windows(w1_start, w1_end, w2_start, w2_end)
    messages_path = os.path.join(data_dir, "rooms", room, "messages.jsonl")
    coverage_path = os.path.join(data_dir, "coverage.jsonl")

    checked = 0
    verified = 0
    failed = 0
    malformed_lines = 0
    ts_unparsable = 0
    # did -> earliest re-verified ts seen anywhere in the whole file
    did_to_earliest_ts = {}
    # did -> at least one re-verified message with ts in window 2
    keys_in_w2 = set()

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
            except (UnsupportedKeyType, MalformedRecord, KeyError, TypeError):
                # TypeError covers a non-string sig (e.g. a bare number or a
                # JSON array/object): verify.py does base64 decoding on sig,
                # which raises TypeError rather than one of the exceptions
                # above for a non-string value. Treated the same as any
                # other re-verify failure, never a crash.
                ok = False
            if ok and not isinstance(record.get("text"), str):
                # Not central to this module (no text-keyed aggregate is
                # built), but kept for consistency with every sibling: a
                # genuinely valid signature over a non-string text is
                # counted as a re-verify failure like any other record
                # this analysis cannot safely include, never a crash.
                ok = False
            if not ok:
                failed += 1
                continue
            verified += 1
            did = record["from"]
            parsed_ts = _parse_ts_seconds(record.get("ts"))
            if parsed_ts is None:
                ts_unparsable += 1
                continue

            current = did_to_earliest_ts.get(did)
            if current is None or parsed_ts < current:
                did_to_earliest_ts[did] = parsed_ts

            if w2_start <= parsed_ts < w2_end:
                keys_in_w2.add(did)

    # The cohort: keys whose FIRST-EVER appearance (earliest ts anywhere in
    # the file, not merely a post during window 1) falls inside window 1.
    # A key already active before window 1 is not a new key in window 1
    # and is excluded -- see the module docstring's anti-confound section.
    cohort = {did for did, ts in did_to_earliest_ts.items() if w1_start <= ts < w1_end}
    cohort_size = len(cohort)
    returned = cohort & keys_in_w2
    returned_count = len(returned)
    persistence_rate = (returned_count / cohort_size) if cohort_size else None
    non_return_rate = (1.0 - persistence_rate) if persistence_rate is not None else None
    non_return_count = cohort_size - returned_count

    coverage_found = os.path.exists(coverage_path)
    coverage_malformed = 0
    restarts_skipped = 0
    w2_captured_total = 0
    w2_dropped_total = 0
    if coverage_found:
        coverage_records, coverage_malformed = _read_room_coverage_records(coverage_path, room)
        intervals, restarts_skipped = _difference_coverage_records(coverage_records)
        for interval_ts, captured_delta, dropped_delta in intervals:
            if w2_start <= interval_ts < w2_end:
                w2_captured_total += captured_delta
                w2_dropped_total += dropped_delta

    w2_denominator = w2_captured_total + w2_dropped_total
    w2_coverage_ratio = (w2_captured_total / w2_denominator) if w2_denominator else None

    return {
        "room": room,
        "w1_start": _iso_from_seconds(w1_start),
        "w1_end": _iso_from_seconds(w1_end),
        "w2_start": _iso_from_seconds(w2_start),
        "w2_end": _iso_from_seconds(w2_end),
        "gap_seconds": w2_start - w1_end,
        "messages_file_found": messages_found,
        "coverage_file_found": coverage_found,
        "signed_checked": checked,
        "signed_reverified": verified,
        "signed_reverify_failed": failed,
        "malformed_lines_skipped": malformed_lines,
        "ts_unparsable_skipped": ts_unparsable,
        "coverage_malformed_lines_skipped": coverage_malformed,
        "restart_intervals_skipped": restarts_skipped,
        "cohort_size": cohort_size,
        "returned_count": returned_count,
        "non_return_count": non_return_count,
        "persistence_rate": persistence_rate,
        "non_return_rate": non_return_rate,
        "w2_captured_total": w2_captured_total,
        "w2_dropped_total": w2_dropped_total,
        "w2_coverage_ratio": w2_coverage_ratio,
    }


def format_report(stats):
    """Render the human-readable report for `stats` (as returned by
    `compute_cohort_stats`).

    The non-return rate is stated as a FLOOR, bounded on two independent
    sides: it rests on re-verified signatures only, and it is only as
    trustworthy as window 2's own coverage (an under-captured window 2
    would miss a real return and inflate non-return, stated plainly
    alongside the number rather than left implicit). "Return" itself is
    bounded to window 2 -- absence there is not evidence a key never
    posted again. Aggregate only, and no individual DID is ever named.
    """
    room = stats["room"]
    lines = []
    lines.append(f"Cohort persistence -- room: {room}")
    lines.append("=" * (23 + len(room)))
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
        "(Every number below rests on re-verified signatures only, not trusted stored ones.)"
    )
    lines.append("")

    lines.append("Windows:")
    lines.append(f"  window 1 (cohort): [{stats['w1_start']}, {stats['w1_end']})")
    lines.append(f"  gap:               {stats['gap_seconds']:.0f}s")
    lines.append(f"  window 2 (return): [{stats['w2_start']}, {stats['w2_end']})")
    lines.append("")

    if stats["signed_reverified"] == 0:
        lines.append("No re-verified signed messages in this window -- nothing to report.")
        return "\n".join(lines)

    lines.append("Cohort persistence:")
    lines.append(f"  cohort size (first-ever seen in window 1): {stats['cohort_size']}")
    if stats["cohort_size"] == 0:
        lines.append("  no keys were first-ever seen in window 1 -- no rate to report.")
    else:
        lines.append(
            f"  returned in window 2: {stats['returned_count']} of {stats['cohort_size']} "
            f"({100.0 * stats['persistence_rate']:.1f}%)"
        )
        lines.append(
            f"  at least {100.0 * stats['non_return_rate']:.1f}% did not return in window 2 "
            f"({stats['non_return_count']} of {stats['cohort_size']})"
        )
    lines.append("")

    lines.append("Window 2 coverage (how much to trust the non-return figure above):")
    if not stats["coverage_file_found"]:
        lines.append(
            "  No coverage.jsonl found for this data dir -- persistence is still computed "
            "above, but window 2 coverage is not available."
        )
    else:
        ratio = stats["w2_coverage_ratio"]
        ratio_str = f"{100.0 * ratio:.1f}%" if ratio is not None else "n/a"
        lines.append(
            f"  {ratio_str} (captured {stats['w2_captured_total']} of "
            f"{stats['w2_captured_total'] + stats['w2_dropped_total']} estimated messages "
            f"in window 2)"
        )
        if ratio is not None and ratio < 0.8:
            lines.append(
                "  window 2 coverage is LOW: a real return could easily have been missed, "
                "so the non-return rate above should be read as weaker evidence than usual."
            )
        if stats["restart_intervals_skipped"]:
            lines.append(
                f"  {stats['restart_intervals_skipped']} coverage interval(s) spanned a "
                f"collector restart and were skipped rather than counted as a drop."
            )
    lines.append("")

    lines.append(f"Coverage caveat: {COVERAGE_CAVEAT}")
    lines.append(f"Return caveat: {RETURN_CAVEAT}")
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
                "missing or unparseable ts and were skipped."
            )
        if stats["coverage_malformed_lines_skipped"]:
            lines.append(
                f"Note: {stats['coverage_malformed_lines_skipped']} unparseable "
                "record(s) in coverage.jsonl were skipped."
            )

    return "\n".join(lines)


def default_out_path(data_dir, room):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return os.path.join(data_dir, "analysis", f"cohort_{room}_{ts}.json")


def _iso_arg(value):
    parsed = _parse_ts_seconds(value)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"not a parseable ISO-8601 UTC timestamp: {value!r}")
    return parsed


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Cohort persistence: of the keys first-ever seen in window 1, the "
        "fraction that return in window 2 (read-only)."
    )
    parser.add_argument("--data-dir", required=True, help="collector data directory to read")
    parser.add_argument("--room", default="lobby", help="room to analyze (default: lobby)")
    parser.add_argument("--w1-start", required=True, type=_iso_arg, help="window 1 start, ISO-8601 UTC")
    parser.add_argument("--w1-end", required=True, type=_iso_arg, help="window 1 end, ISO-8601 UTC")
    parser.add_argument("--w2-start", required=True, type=_iso_arg, help="window 2 start, ISO-8601 UTC")
    parser.add_argument("--w2-end", required=True, type=_iso_arg, help="window 2 end, ISO-8601 UTC")
    parser.add_argument(
        "--out",
        default=None,
        help="path to write the JSON report to "
        "(default: <data-dir>/analysis/cohort_<room>_<ts>.json)",
    )
    args = parser.parse_args(argv)

    try:
        _validate_windows(args.w1_start, args.w1_end, args.w2_start, args.w2_end)
    except ValueError as exc:
        parser.error(str(exc))

    stats = compute_cohort_stats(
        args.data_dir,
        room=args.room,
        w1_start=args.w1_start,
        w1_end=args.w1_end,
        w2_start=args.w2_start,
        w2_end=args.w2_end,
    )
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
