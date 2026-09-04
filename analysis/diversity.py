"""Per-key content diversity: the sixth measurement in the Basanos
measurement layer.

Read-only by construction: this module only reads a room's already-stored
`<data-dir>/rooms/<room>/messages.jsonl` and `<data-dir>/coverage.jsonl`.
It never writes to, or modifies, anything under `<data-dir>` except the
analysis output this module produces itself.

THE HEADLINE: the "one-and-done" rate -- the share of distinct signing
keys that post exactly one message, with exactly one distinct text, and
are never seen again in this window. A high one-and-done rate is
consistent with a large population of throwaway or single-use keys; a low
one is consistent with a smaller population of keys that keep posting.
Either way this is a statement about the shape of key activity, not a
verdict about any single poster (see the heartbeat caveat below, which
applies here just as it does to every other module: a key that posts once
and stops could just as easily be a legitimate participant who tried the
commons once).

WHY THIS NEEDS A CONTROL, NOT JUST A NUMBER: the collector's own ring
eviction could in principle manufacture a fake one-and-done signal --
if capture were spotty enough, a real multi-message key could look
one-and-done simply because only one of its messages was ever captured.
So the headline is reported STRATIFIED BY COVERAGE, using exactly the
per-hour coverage-differencing `analysis/diurnal.py` already established
(read `<data-dir>/coverage.jsonl`, sort this room's cumulative snapshots
by captured_at, difference consecutive pairs, treat a negative delta as a
collector restart and skip that interval -- see
`_difference_coverage_records` below, duplicated from diurnal.py's own
function on purpose, the same "duplicate rather than share a helper"
convention every module in this project already follows). Each key is
placed into a coverage band by the coverage of the hour it was FIRST seen
in. If the one-and-done rate stays high even in the best-captured hours,
the population is real, not a capture artifact: better capture is not
converting one-and-done keys into multi-message keys.

THE ONE HONEST ANOMALY TO EXPECT, STATED UP FRONT SO IT IS NOT MISREAD: the
lowest-coverage band tends to show a LOWER one-and-done rate than the
high-coverage bands, and this is not a contradiction. The heaviest-eviction
hours are exactly the hours where a high-volume repeat poster is likeliest
to have at least one message survive the ring, while a true one-and-done
key has exactly one chance to be captured at all -- so heavy eviction
selects, mechanically, for the survivors of multi-message keys, biasing
the lowest-coverage band toward looking less one-and-done than the room
really is. The trustworthy read is the high-coverage bands, not the low
one; the low band is reported for completeness, not as a rebuttal.

A "shared template" (the population `analysis/duplication.py`,
`analysis/coordination.py`, `analysis/synchrony.py`, and `analysis/nonce.py`
all rank by distinct-key count) has no role in this module at all: this is
a per-KEY measurement over every distinct signing key, not a per-text one.
This module does not import from or modify any of those, or
`analysis/diurnal.py` (each intentionally duplicates its own small
streaming/re-verify walk, to keep every module a single self-contained
read).

Deliberately out of scope for v1: any claim about WHY a key is
one-and-done (bot, human trying it once, a test script), any per-identity
output, and any near-duplicate text matching (the text-diversity and
most-common-text numbers below use exact byte-identical text, same as
every other module). Every number below is a count, a rate, or a fraction
over keys or texts, never a name.

Usage:
    python -m analysis.diversity --data-dir <dir> [--room lobby] [--out <path>]
"""

import argparse
import json
import math
import os
import re
from datetime import datetime, timezone

from collector.verify import MalformedRecord, UnsupportedKeyType, is_signed, verify_record

DISTINCT_TEXT_BUCKET_KEYS = ("1", "2-5", "6-10", "11-50", "51+")

COVERAGE_BAND_KEYS = ("high", "mid", "low")
COVERAGE_BAND_LABELS = {"high": ">=90%", "mid": "80-90%", "low": "<80%"}

CAVEAT = (
    "some rooms may intend heartbeat-style posting, so this is a statement "
    "about the shape of the traffic, not a verdict about any poster."
)

JOIN_CAVEAT = (
    "messages are binned by server ts, coverage deltas by the collector's own "
    "captured_at -- these are two different clocks that normally track each other "
    "closely but diverge more right around a collector restart, so placing a key's "
    "first-seen hour against a coverage band is an APPROXIMATE join, not an exact "
    "reconciliation."
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
    its place in the timing side of this analysis, not a crash. The
    trailing "Z" is normalized to "+00:00" by hand rather than relying on
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


def _hourly_coverage_from_intervals(intervals):
    """Fold per-interval coverage deltas into per-hour coverage ratios.

    `intervals`: (captured_at_seconds, captured_delta, dropped_delta)
    tuples, as returned by `_difference_coverage_records`. Hours are
    identified by an absolute UTC hour index (`int(ts // 3600)`), not by a
    window-relative bin the way `analysis/diurnal.py` bins for plotting --
    an absolute index is what lets a key's own first-seen hour be looked
    up directly, without needing to first establish a shared window
    anchor between two otherwise-independent computations (keys' first
    seen ts, and coverage intervals' captured_at).

    More than one interval can fall in the same hour (coverage snapshots
    can run more often than hourly); their deltas are summed before the
    ratio is taken, the same "sum first, then divide" order
    `analysis/diurnal.py` uses per bin.

    Returns a dict: hour index -> coverage ratio (captured / (captured +
    dropped)), or None for an hour whose summed denominator is 0 (no
    captured-or-dropped activity recorded for that hour at all -- nothing
    to compute a ratio from).
    """
    hour_captured = {}
    hour_dropped = {}
    for interval_ts, captured_delta, dropped_delta in intervals:
        hour = int(interval_ts // 3600)
        hour_captured[hour] = hour_captured.get(hour, 0) + captured_delta
        hour_dropped[hour] = hour_dropped.get(hour, 0) + dropped_delta
    hours = set(hour_captured) | set(hour_dropped)
    coverage_by_hour = {}
    for hour in hours:
        captured = hour_captured.get(hour, 0)
        dropped = hour_dropped.get(hour, 0)
        denom = captured + dropped
        coverage_by_hour[hour] = (captured / denom) if denom else None
    return coverage_by_hour


def _coverage_band(ratio):
    """Which of the three coverage bands a per-hour coverage ratio falls
    in. None in, None out (the caller excludes that key from every band
    rather than guessing).
    """
    if ratio is None:
        return None
    if ratio >= 0.9:
        return "high"
    if ratio >= 0.8:
        return "mid"
    return "low"


def _distinct_text_bucket(count):
    if count == 1:
        return "1"
    if count <= 5:
        return "2-5"
    if count <= 10:
        return "6-10"
    if count <= 50:
        return "11-50"
    return "51+"


def _shannon_entropy_bits(counts, total):
    """Shannon entropy, base 2, of a count distribution over `total`
    observations. None when there is nothing to measure (total is 0).
    """
    if total == 0:
        return None
    entropy = 0.0
    for count in counts:
        if count == 0:
            continue
        p = count / total
        entropy -= p * math.log2(p)
    return entropy


def compute_diversity_stats(data_dir, room="lobby"):
    """Stream `<data_dir>/rooms/<room>/messages.jsonl` and
    `<data_dir>/coverage.jsonl` and compute per-key content-diversity
    aggregates: the distinct-text-count distribution across keys, the
    one-and-done rate overall and stratified by the coverage of each
    key's first-seen hour, and room-wide text-diversity supporting stats.

    Returns a dict with the raw counters and aggregates needed by both the
    human-readable report and the JSON output. Reads only; writes nothing.
    No did:key string appears anywhere in the returned structure -- keys
    are only ever counted, never named.
    """
    _validate_room(room)
    messages_path = os.path.join(data_dir, "rooms", room, "messages.jsonl")
    coverage_path = os.path.join(data_dir, "coverage.jsonl")

    checked = 0
    verified = 0
    failed = 0
    malformed_lines = 0
    ts_unparsable = 0
    # did -> number of re-verified signed messages
    did_to_message_count = {}
    # did -> set of distinct texts that did signed (re-verified only)
    did_to_texts = {}
    # did -> earliest parseable ts seen for that did (re-verified only)
    did_to_first_ts = {}
    # text -> count of re-verified messages carrying it, room-wide
    text_to_message_count = {}

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
                # A genuinely valid signature over a non-string text (e.g.
                # a JSON array or object instead of a string) would crash
                # every text-keyed aggregate below with "unhashable type".
                # Counted as a re-verify failure like any other record this
                # analysis cannot safely include, not a crash.
                ok = False
            if not ok:
                failed += 1
                continue
            verified += 1
            did = record["from"]
            text = record.get("text")

            did_to_message_count[did] = did_to_message_count.get(did, 0) + 1
            did_to_texts.setdefault(did, set()).add(text)
            text_to_message_count[text] = text_to_message_count.get(text, 0) + 1

            parsed_ts = _parse_ts_seconds(record.get("ts"))
            if parsed_ts is None:
                ts_unparsable += 1
            else:
                current = did_to_first_ts.get(did)
                if current is None or parsed_ts < current:
                    did_to_first_ts[did] = parsed_ts

    total_distinct_keys = len(did_to_message_count)

    # 1. Per-key distinct-text distribution.
    distinct_text_buckets = {key: 0 for key in DISTINCT_TEXT_BUCKET_KEYS}
    distinct_text_level_counts = {}
    max_distinct_texts = None
    for did, texts in did_to_texts.items():
        level = len(texts)
        distinct_text_buckets[_distinct_text_bucket(level)] += 1
        distinct_text_level_counts[str(level)] = distinct_text_level_counts.get(str(level), 0) + 1
        if max_distinct_texts is None or level > max_distinct_texts:
            max_distinct_texts = level

    # 2. One-and-done: exactly one message AND exactly one distinct text.
    # One message necessarily implies one distinct text, but both are
    # checked explicitly so the definition reads unambiguously rather than
    # relying on that implication silently.
    one_and_done_dids = {
        did
        for did, count in did_to_message_count.items()
        if count == 1 and len(did_to_texts[did]) == 1
    }
    one_and_done_count = len(one_and_done_dids)
    one_and_done_rate = (one_and_done_count / total_distinct_keys) if total_distinct_keys else None

    # 3. Coverage-stratified one-and-done.
    coverage_found = os.path.exists(coverage_path)
    coverage_malformed = 0
    restarts_skipped = 0
    coverage_by_hour = {}
    if coverage_found:
        coverage_records, coverage_malformed = _read_room_coverage_records(coverage_path, room)
        intervals, restarts_skipped = _difference_coverage_records(coverage_records)
        coverage_by_hour = _hourly_coverage_from_intervals(intervals)

    coverage_bands = {
        band: {"key_count": 0, "one_and_done_count": 0} for band in COVERAGE_BAND_KEYS
    }
    keys_excluded_no_coverage_data = 0
    keys_excluded_no_parseable_ts = 0

    if coverage_found:
        for did in did_to_message_count:
            first_ts = did_to_first_ts.get(did)
            if first_ts is None:
                keys_excluded_no_parseable_ts += 1
                continue
            hour = int(first_ts // 3600)
            ratio = coverage_by_hour.get(hour)
            band = _coverage_band(ratio)
            if band is None:
                keys_excluded_no_coverage_data += 1
                continue
            coverage_bands[band]["key_count"] += 1
            if did in one_and_done_dids:
                coverage_bands[band]["one_and_done_count"] += 1

    for band in COVERAGE_BAND_KEYS:
        entry = coverage_bands[band]
        entry["one_and_done_rate"] = (
            (entry["one_and_done_count"] / entry["key_count"]) if entry["key_count"] else None
        )

    # 4. Room text-diversity supporting stats.
    total_room_messages = sum(text_to_message_count.values())
    text_entropy_bits = _shannon_entropy_bits(text_to_message_count.values(), total_room_messages)
    most_common_text_share = (
        (max(text_to_message_count.values()) / total_room_messages) if total_room_messages else None
    )

    return {
        "room": room,
        "messages_file_found": messages_found,
        "coverage_file_found": coverage_found,
        "signed_checked": checked,
        "signed_reverified": verified,
        "signed_reverify_failed": failed,
        "malformed_lines_skipped": malformed_lines,
        "ts_unparsable_skipped": ts_unparsable,
        "coverage_malformed_lines_skipped": coverage_malformed,
        "restart_intervals_skipped": restarts_skipped,
        "total_distinct_keys": total_distinct_keys,
        "distinct_text_buckets": distinct_text_buckets,
        "distinct_text_level_counts": distinct_text_level_counts,
        "max_distinct_texts": max_distinct_texts,
        "one_and_done_count": one_and_done_count,
        "one_and_done_rate": one_and_done_rate,
        "coverage_bands": coverage_bands,
        "keys_excluded_no_coverage_data": keys_excluded_no_coverage_data,
        "keys_excluded_no_parseable_ts": keys_excluded_no_parseable_ts,
        "distinct_texts_room_wide": len(text_to_message_count),
        "text_entropy_bits": text_entropy_bits,
        "most_common_text_share": most_common_text_share,
    }


def format_report(stats):
    """Render the human-readable report for `stats` (as returned by
    `compute_diversity_stats`).

    The one-and-done rate is stated as a FLOOR, and specifically NOT as a
    single point estimate: "at least X%, and Y% in the best-captured
    hours" -- the coverage-stratified number in the high band is the
    trustworthy one, precisely because it is measured where the collector
    is least likely to have manufactured the signal through eviction. The
    low-coverage band is expected to read lower for a structural reason
    (heavy eviction favors the survival of repeat posters' messages, not
    true one-and-done keys), stated explicitly so it is never misread as
    contradicting the high-coverage bands. Aggregate only, and no
    individual DID is ever named.
    """
    room = stats["room"]
    lines = []
    lines.append(f"Key diversity -- room: {room}")
    lines.append("=" * (18 + len(room)))
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

    if stats["signed_reverified"] == 0:
        lines.append("No re-verified signed messages in this window -- nothing to report.")
        return "\n".join(lines)

    if not stats["coverage_file_found"]:
        lines.append(
            "No coverage.jsonl found for this data dir -- overall one-and-done rate only, "
            "no coverage stratification available."
        )
        lines.append("")

    lines.append("1. Distinct-text distribution across keys:")
    for bucket in DISTINCT_TEXT_BUCKET_KEYS:
        lines.append(f"   {bucket} distinct text(s): {stats['distinct_text_buckets'][bucket]} key(s)")
    if stats["max_distinct_texts"] is not None:
        lines.append(f"   max distinct texts by a single key: {stats['max_distinct_texts']}")
    lines.append("")

    rate = stats["one_and_done_rate"]
    rate_str = f"{100.0 * rate:.1f}%" if rate is not None else "n/a"
    lines.append("2. One-and-done rate (headline)")
    lines.append(
        f"   At least {rate_str} of {stats['total_distinct_keys']} distinct signing keys "
        f"posted exactly one message with exactly one distinct text and were never seen "
        f"again ({stats['one_and_done_count']} key(s))."
    )
    lines.append("")

    lines.append("3. One-and-done rate stratified by coverage of the key's first-seen hour:")
    if not stats["coverage_file_found"]:
        lines.append("   not available (no coverage.jsonl).")
    else:
        for band in COVERAGE_BAND_KEYS:
            entry = stats["coverage_bands"][band]
            band_rate = entry["one_and_done_rate"]
            band_rate_str = f"{100.0 * band_rate:.1f}%" if band_rate is not None else "n/a"
            lines.append(
                f"   {COVERAGE_BAND_LABELS[band]} coverage: {band_rate_str} "
                f"({entry['one_and_done_count']} of {entry['key_count']} keys)"
            )
        lines.append(
            f"   excluded, no coverage data for that hour: {stats['keys_excluded_no_coverage_data']} key(s)"
        )
        lines.append(
            f"   excluded, no parseable ts at all: {stats['keys_excluded_no_parseable_ts']} key(s)"
        )
        if stats["restart_intervals_skipped"]:
            lines.append(
                f"   {stats['restart_intervals_skipped']} coverage interval(s) spanned a "
                f"collector restart and were skipped rather than counted as a drop."
            )
        high_rate = stats["coverage_bands"]["high"]["one_and_done_rate"]
        if high_rate is not None:
            lines.append(
                f"   The trustworthy figure: at least {100.0 * high_rate:.1f}% in the "
                f"best-captured (>=90% coverage) hours."
            )
        lines.append(
            "   The lowest-coverage band is expected to read LOWER, not higher: heavy "
            "eviction favors survival of repeat posters' messages over a true one-and-done "
            "key's single chance to be captured at all, so that band is structurally biased "
            "toward looking less one-and-done and must not be read as contradicting the "
            "high-coverage bands."
        )
    lines.append("")

    lines.append("4. Room text-diversity supporting stats:")
    lines.append(f"   distinct texts, room-wide: {stats['distinct_texts_room_wide']}")
    entropy = stats["text_entropy_bits"]
    lines.append(f"   text entropy (base 2): {entropy:.3f} bits" if entropy is not None else "   text entropy: n/a")
    share = stats["most_common_text_share"]
    lines.append(
        f"   most-common text's share of all messages: {100.0 * share:.1f}%" if share is not None else
        "   most-common text's share: n/a"
    )
    if stats["distinct_text_level_counts"]:
        lines.append("   distinct-text-count levels (key count per exact level, aggregate only):")
        for level_str in sorted(stats["distinct_text_level_counts"], key=int):
            lines.append(f"     {level_str}: {stats['distinct_text_level_counts'][level_str]} key(s)")
    lines.append("")

    lines.append(
        "This is a FLOOR: it rests on re-verified signatures only and is measured only at "
        "the coverage stated above."
    )
    lines.append(f"Join caveat: {JOIN_CAVEAT}")
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
                "missing or unparseable ts."
            )
        if stats["coverage_malformed_lines_skipped"]:
            lines.append(
                f"Note: {stats['coverage_malformed_lines_skipped']} unparseable "
                "record(s) in coverage.jsonl were skipped."
            )

    return "\n".join(lines)


def default_out_path(data_dir, room):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return os.path.join(data_dir, "analysis", f"diversity_{room}_{ts}.json")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Per-key content diversity and the one-and-done rate over a room's "
        "already-collected messages (read-only)."
    )
    parser.add_argument("--data-dir", required=True, help="collector data directory to read")
    parser.add_argument("--room", default="lobby", help="room to analyze (default: lobby)")
    parser.add_argument(
        "--out",
        default=None,
        help="path to write the JSON report to "
        "(default: <data-dir>/analysis/diversity_<room>_<ts>.json)",
    )
    args = parser.parse_args(argv)

    stats = compute_diversity_stats(args.data_dir, room=args.room)
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
