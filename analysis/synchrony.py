"""Timing synchrony: the third measurement in the Basanos measurement layer.

Read-only by construction: this module only reads a room's already-stored
`<data-dir>/rooms/<room>/messages.jsonl` and `<data-dir>/coverage_state.json`
(via `collector.coverage.CoverageTracker`, whose `counters()` method is
itself read-only). It never writes to, or modifies, anything under
`<data-dir>` except the analysis output this module produces itself.

A "shared template" is an exact stored text (byte-identical) signed by at
least two distinct did:keys -- the same population `analysis/duplication.py`
calls "cross-key duplicated" and `analysis/coordination.py` ranks by
distinct-key count for its core-bloc analysis. This module does not import
from or modify either of those (they each intentionally duplicate the
small streaming/re-verify walk rather than share a helper, to keep each
module a single self-contained read); it asks a different question about
the same top-N templates: not who signs them or how concentrated the
signers are, but WHEN the posts of a shared template arrive. Posts that
land in a tight time burst are consistent with a shared scheduler; posts
spread evenly across the window are consistent with independent,
uncoordinated heartbeat posting.

`ts` (the server timestamp on every message) is the event clock this
module measures against, never `captured_at` (a collector-local sanity
field, not the server's own record of when a message was posted). `ts`
has confirmed benign local reordering of up to roughly 12 seconds between
adjacent posts, from concurrent server-side ingestion -- this is why the
headline bucket width defaults to 10 seconds and is never made finer than
that: bucketing below the noise floor would just measure ingestion jitter,
not posting behavior.

THREE NULL MODELS, not one: a template's posts are compared against three
increasingly rigorous baselines, because the lobby's own activity is not
flat -- it swings noticeably over a window -- so a template that merely
tracks overall room activity would read bursty against a naive uniform
null even though it is only following the crowd.

- uniform: every 10s bucket equally likely. The naive baseline, kept only
  to show how much the room-weighting below corrects it.
- room: this template's posts compared against a null weighted by the
  ROOM'S OWN per-bucket activity (every re-verified signed post in the
  room, not just this template's), over the same bucket range. A template
  that merely rides the room's rhythm reads near 1 here.
- room-minus-self: the same room-weighted null, but with this template's
  own posts subtracted out of the weights first, so a template is never
  compared against a baseline partly built from itself. This is the
  HEADLINE: a template only indicates timing coordination beyond the room
  if it is elevated here, not just against uniform or room.

Deliberately out of scope for v1 (later tiers): a full key-linkage graph
or connected-components analysis, near-duplicate/template-variant merging,
and any per-identity output. Exact text match only, and never a report of
what any single identity did -- every number below is a count or a ratio
over templates/buckets, never a name.

Usage:
    python -m analysis.synchrony --data-dir <dir> [--room lobby] [--top-n 20] \\
        [--bucket-seconds 10] [--out <path>]
"""

import argparse
import hashlib
import json
import os
import random
import statistics
from datetime import datetime, timezone

from collector.coverage import CoverageTracker
from collector.verify import MalformedRecord, UnsupportedKeyType, is_signed, verify_record

DEFAULT_TOP_N = 20
DEFAULT_BUCKET_SECONDS = 10.0
NULL_MODEL_TRIALS = 500
RATIO_THRESHOLDS = (2.0, 5.0, 10.0)

CAVEAT = (
    "some rooms may intend heartbeat-style posting, so this is a statement "
    "about the shape of the traffic, not a verdict about any poster."
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
    """Parse a stored `ts` (ISO-8601 UTC, e.g.
    "2026-09-02T15:36:03.288522Z") into epoch seconds (a float).

    Returns None, never raises, on anything that isn't a parseable
    timestamp -- a missing or malformed `ts` costs that one post its place
    in the timing analysis, not a crash. The trailing "Z" is normalized to
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


def _index_of_dispersion(bucket_counts_all, bucket_count):
    """Index of dispersion (population variance / mean) of `bucket_counts_all`,
    a list of length `bucket_count` giving the post count of EVERY bucket in
    the active span, including empty ones.

    Empty buckets matter here: they are what separates a bursty template
    (a handful of full buckets, the rest empty) from a metronomic one
    (every bucket equally, evenly full) even when both have the same
    single busiest bucket. Population variance (divide by `bucket_count`,
    not `bucket_count - 1`) since `bucket_counts_all` is the complete set
    of buckets for this template's own active span, not a sample of it.

    Returns None when the index is not defined: fewer than 2 buckets (no
    spread to measure at all), or a mean of 0 (only possible with zero
    posts, which never reaches here since a shared template has >= 2).
    """
    if bucket_count < 2:
        return None
    mean = sum(bucket_counts_all) / bucket_count
    if mean == 0:
        return None
    variance = sum((c - mean) ** 2 for c in bucket_counts_all) / bucket_count
    return variance / mean


def _expected_dispersion_uniform(post_count, bucket_count):
    """The index of dispersion a UNIFORM-random null model would produce:
    `post_count` posts scattered independently and uniformly at random
    across `bucket_count` buckets (every bucket equally likely), averaged
    over NULL_MODEL_TRIALS trials of the identical `_index_of_dispersion`
    measurement. The naive baseline -- kept only so the room-weighted
    nulls below can show how much they correct it, never the headline.

    Seeded deterministically from (post_count, bucket_count) alone -- no
    OS randomness, no wall clock -- so the same template shape always
    reproduces the exact same expectation, run to run, machine to machine.
    None when undefined (fewer than 2 buckets).
    """
    if bucket_count < 2:
        return None
    rng = random.Random(post_count * 1_000_003 + bucket_count)
    total = 0.0
    for _ in range(NULL_MODEL_TRIALS):
        bucket_counts_all = [0] * bucket_count
        for _ in range(post_count):
            b = rng.randrange(bucket_count)
            bucket_counts_all[b] += 1
        total += _index_of_dispersion(bucket_counts_all, bucket_count)
    return total / NULL_MODEL_TRIALS


def _weighted_null_seed(post_count, weights):
    """A deterministic seed derived from `post_count` and a hash of the
    exact weight vector -- not just post_count and bucket_count, since two
    templates that happen to share both but sit against differently-shaped
    room activity must not share a null draw. `weights` are always
    non-negative integers (post counts or their differences) here, so
    `repr(tuple(weights))` is an exact, stable string to hash -- no
    floating-point formatting instability. No OS randomness, no wall
    clock: the same (post_count, weights) pair always reproduces the same
    seed, run to run, machine to machine.
    """
    digest = hashlib.sha256(repr(tuple(weights)).encode("utf-8")).hexdigest()
    return post_count * 1_000_003 + int(digest[:15], 16)


def _expected_dispersion_weighted(post_count, weights):
    """The index of dispersion a WEIGHTED-random null model would produce:
    `post_count` posts scattered independently at random across
    `len(weights)` buckets, each post landing in bucket i with probability
    proportional to `weights[i]` (a weighted categorical draw, not a
    uniform one), averaged over NULL_MODEL_TRIALS trials of the identical
    `_index_of_dispersion` measurement.

    Used for both the whole-room null (weights = the room's own per-bucket
    activity over this template's active span) and the room-minus-self
    null (weights = that same room activity with this template's own
    posts subtracted out, bucket by bucket) -- the only difference between
    the two calls is which weight vector is passed in.

    Returns None when undefined: fewer than 2 buckets, or every weight is
    0 -- nothing to weight toward. For the minus-self null specifically,
    an all-zero vector means this template's own posts account for ALL
    re-verified signed activity across its own active span, so there is
    no "rest of the room" left to compare against; the caller reports that
    ratio as None rather than dividing by a baseline that doesn't exist.

    Seeded via `_weighted_null_seed`, deterministic in (post_count,
    weights) alone -- no OS randomness, no wall clock.
    """
    bucket_count = len(weights)
    if bucket_count < 2:
        return None
    if sum(weights) <= 0:
        return None
    rng = random.Random(_weighted_null_seed(post_count, weights))
    total = 0.0
    for _ in range(NULL_MODEL_TRIALS):
        draws = rng.choices(range(bucket_count), weights=weights, k=post_count)
        bucket_counts_all = [0] * bucket_count
        for b in draws:
            bucket_counts_all[b] += 1
        total += _index_of_dispersion(bucket_counts_all, bucket_count)
    return total / NULL_MODEL_TRIALS


def compute_template_synchrony(ts_values, bucket_seconds, window_earliest, room_bucket_counts, global_bucket_count):
    """The per-template timing-synchrony numbers for one shared template's
    re-verified posts.

    `ts_values`: a non-empty list of epoch-second floats (one per
    re-verified post carrying that template's text). `window_earliest`,
    `room_bucket_counts` (the room-wide re-verified-signed-post count per
    10s bucket across the WHOLE window, built once per call to
    `compute_synchrony_stats`), and `global_bucket_count` anchor this
    template's own bucketing to the exact same grid the room curve uses.

    IMPORTANT: this template's own bucket range (its active span, from its
    earliest to its latest post) is computed as a SLICE of indices into
    that same global grid, and the room-weighted nulls below are built
    from the identical slice of `room_bucket_counts` -- observed and
    expected are always measured over the same bucket set, never a
    template-local grid compared against a differently-anchored room
    grid, or the ratio between them would be meaningless.

    Returns a dict with the active span, bucketing, the observed index of
    dispersion across ALL buckets in that span (including empty ones),
    three null-model expectations for the same shape (uniform, room, and
    room-minus-self) and their three ratios, and a plain descriptive
    secondary stat, `busiest_bucket_fraction`.

    The HEADLINE is `dispersion_ratio_room_minus_self`: only an elevation
    there indicates timing coordination beyond the room's own rhythm.
    `dispersion_ratio_uniform` and `dispersion_ratio_room` are kept
    alongside it -- a template can read high on uniform (or even on room)
    and still read near 1 on room-minus-self, which means it merely
    tracks the room's own activity rather than showing extra coordination.
    """
    post_count = len(ts_values)
    earliest = min(ts_values)
    latest = max(ts_values)
    active_span = latest - earliest

    def _global_bucket(ts):
        idx = int((ts - window_earliest) // bucket_seconds)
        return max(0, min(idx, global_bucket_count - 1))

    start_bucket = min(_global_bucket(ts) for ts in ts_values)
    end_bucket = max(_global_bucket(ts) for ts in ts_values)
    bucket_count = end_bucket - start_bucket + 1

    bucket_counts_all = [0] * bucket_count
    for ts in ts_values:
        bucket_counts_all[_global_bucket(ts) - start_bucket] += 1

    # The template's own bucket range, sliced out of the room-wide curve on
    # the identical global grid -- this is what makes the room-weighted
    # nulls below comparable to the observed dispersion at all.
    room_slice = room_bucket_counts[start_bucket : end_bucket + 1]
    minus_self_weights = [
        max(0, room_count - own_count) for room_count, own_count in zip(room_slice, bucket_counts_all)
    ]

    observed_dispersion = _index_of_dispersion(bucket_counts_all, bucket_count)
    expected_uniform = _expected_dispersion_uniform(post_count, bucket_count)
    expected_room = _expected_dispersion_weighted(post_count, room_slice)
    expected_room_minus_self = _expected_dispersion_weighted(post_count, minus_self_weights)

    def _ratio(expected):
        if observed_dispersion is not None and expected:
            return observed_dispersion / expected
        return None

    dispersion_ratio_uniform = _ratio(expected_uniform)
    dispersion_ratio_room = _ratio(expected_room)
    dispersion_ratio_room_minus_self = _ratio(expected_room_minus_self)

    busiest_bucket_fraction = max(bucket_counts_all) / post_count

    return {
        "post_count": post_count,
        "active_span_seconds": active_span,
        "bucket_count": bucket_count,
        "occupied_bucket_count": sum(1 for c in bucket_counts_all if c > 0),
        "observed_dispersion": observed_dispersion,
        "expected_dispersion_uniform": expected_uniform,
        "expected_dispersion_room": expected_room,
        "expected_dispersion_room_minus_self": expected_room_minus_self,
        "dispersion_ratio_uniform": dispersion_ratio_uniform,
        "dispersion_ratio_room": dispersion_ratio_room,
        "dispersion_ratio_room_minus_self": dispersion_ratio_room_minus_self,
        "busiest_bucket_fraction": busiest_bucket_fraction,
    }


def compute_synchrony_stats(data_dir, room="lobby", top_n=DEFAULT_TOP_N, bucket_seconds=DEFAULT_BUCKET_SECONDS):
    """Stream `<data_dir>/rooms/<room>/messages.jsonl` and compute the
    timing-synchrony aggregates for the top-N shared templates.

    Returns a dict with the raw counters and aggregates needed by both the
    human-readable report and the JSON output. Reads only; writes nothing.
    No did:key string appears anywhere in the returned structure -- keys
    are only ever counted, never named.
    """
    messages_path = os.path.join(data_dir, "rooms", room, "messages.jsonl")

    checked = 0
    verified = 0
    failed = 0
    malformed_lines = 0
    ts_unparsable = 0
    # text -> set of distinct signing DIDs that produced it (re-verified only)
    text_to_dids = {}
    # text -> list of epoch-second floats, one per re-verified post (re-verified only)
    text_to_ts = {}
    # every re-verified signed post's ts, any text -- builds the room-wide
    # activity curve the weighted nulls are measured against
    all_ts = []

    found = os.path.exists(messages_path)
    if found:
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
            text_to_dids.setdefault(text, set()).add(did)
            parsed_ts = _parse_ts_seconds(record.get("ts"))
            if parsed_ts is None:
                ts_unparsable += 1
            else:
                text_to_ts.setdefault(text, []).append(parsed_ts)
                all_ts.append(parsed_ts)

    # The room-wide activity curve: every re-verified signed post (any
    # text), binned on one grid anchored at the window's own earliest ts.
    # Each top-N template's own bucketing below is a slice of THIS exact
    # grid, never a separately-anchored one -- see compute_template_synchrony.
    if all_ts:
        window_earliest = min(all_ts)
        window_latest = max(all_ts)
        global_bucket_count = int((window_latest - window_earliest) // bucket_seconds) + 1
        room_bucket_counts = [0] * global_bucket_count
        for ts in all_ts:
            idx = int((ts - window_earliest) // bucket_seconds)
            idx = max(0, min(idx, global_bucket_count - 1))
            room_bucket_counts[idx] += 1
    else:
        window_earliest = None
        global_bucket_count = 0
        room_bucket_counts = []

    # A "shared template": exact text signed by >= 2 distinct DIDs, ranked by
    # distinct-key count -- the same population and ordering coordination.py
    # uses for its own top-N.
    shared_templates = {t for t, dids in text_to_dids.items() if len(dids) >= 2}
    ranked_templates = sorted(shared_templates, key=lambda t: (-len(text_to_dids[t]), t))
    top_n_templates = ranked_templates[:top_n]

    template_reports = []
    ratios_uniform = []
    ratios_room = []
    ratios_room_minus_self = []
    for t in top_n_templates:
        ts_values = text_to_ts.get(t, [])
        if not ts_values:
            continue  # no parseable timestamp for any post of this template
        synchrony = compute_template_synchrony(
            ts_values, bucket_seconds, window_earliest, room_bucket_counts, global_bucket_count
        )
        template_reports.append({"text": t, "distinct_keys": len(text_to_dids[t]), **synchrony})
        # A None ratio (span too short for a second bucket, or an all-zero
        # minus-self weight vector) is excluded from its aggregate rather
        # than treated as 0 or skewing the count.
        if synchrony["dispersion_ratio_uniform"] is not None:
            ratios_uniform.append(synchrony["dispersion_ratio_uniform"])
        if synchrony["dispersion_ratio_room"] is not None:
            ratios_room.append(synchrony["dispersion_ratio_room"])
        if synchrony["dispersion_ratio_room_minus_self"] is not None:
            ratios_room_minus_self.append(synchrony["dispersion_ratio_room_minus_self"])

    def _median_max(values):
        return (statistics.median(values), max(values)) if values else (None, None)

    median_ratio_uniform, max_ratio_uniform = _median_max(ratios_uniform)
    median_ratio_room, max_ratio_room = _median_max(ratios_room)
    median_ratio_room_minus_self, max_ratio_room_minus_self = _median_max(ratios_room_minus_self)

    ratio_threshold_counts = {
        str(threshold): sum(1 for r in ratios_room_minus_self if r > threshold)
        for threshold in RATIO_THRESHOLDS
    }

    coverage = CoverageTracker(data_dir).counters(room)
    coverage_ratio = CoverageTracker.coverage_ratio(
        coverage.get("captured_total", 0), coverage.get("dropped_total", 0)
    )

    return {
        "room": room,
        "top_n": top_n,
        "bucket_seconds": bucket_seconds,
        "null_model_trials": NULL_MODEL_TRIALS,
        "messages_file_found": found,
        "signed_checked": checked,
        "signed_reverified": verified,
        "signed_reverify_failed": failed,
        "malformed_lines_skipped": malformed_lines,
        "ts_unparsable_skipped": ts_unparsable,
        "distinct_shared_templates": len(shared_templates),
        "templates": template_reports,
        "median_dispersion_ratio_uniform": median_ratio_uniform,
        "max_dispersion_ratio_uniform": max_ratio_uniform,
        "median_dispersion_ratio_room": median_ratio_room,
        "max_dispersion_ratio_room": max_ratio_room,
        "median_dispersion_ratio_room_minus_self": median_ratio_room_minus_self,
        "max_dispersion_ratio_room_minus_self": max_ratio_room_minus_self,
        "ratio_threshold_counts": ratio_threshold_counts,
        "coverage_captured_total": coverage.get("captured_total", 0),
        "coverage_dropped_total": coverage.get("dropped_total", 0),
        "coverage_ratio": coverage_ratio,
    }


def format_report(stats):
    """Render the human-readable report for `stats` (as returned by
    `compute_synchrony_stats`).

    The dispersion ratio is stated as a FLOOR, not a verdict: it rests on
    `ts`, which has roughly 10 seconds of confirmed benign local
    reordering between adjacent posts (why the bucket width defaults to
    10 seconds and is never made finer), and is measured only at the
    coverage ratio captured below. The HEADLINE is the room-minus-self
    ratio: much greater than 1 there is evidence of coordinated timing
    beyond the room's own rhythm; near 1 is consistent with the template
    simply following the room, or with independent random-timed posting;
    below 1 is MORE even than either, consistent with metronomic
    heartbeat posting. A template that reads high on uniform or room but
    near 1 on room-minus-self is not bursty, it is only tracking the
    room's own activity. Either way this is a statement about the timing
    SHAPE of the traffic, never a verdict about any poster, and no
    individual DID is ever named.
    """
    room = stats["room"]
    top_n = stats["top_n"]
    bucket_seconds = stats["bucket_seconds"]
    lines = []
    lines.append(f"Timing synchrony -- room: {room}")
    lines.append("=" * (20 + len(room)))
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

    coverage_ratio = stats["coverage_ratio"]
    ratio_str = f"{coverage_ratio:.4f}" if coverage_ratio is not None else "n/a"
    lines.append("Coverage:")
    lines.append(f"  captured_total: {stats['coverage_captured_total']}")
    lines.append(f"  dropped_total:  {stats['coverage_dropped_total']}")
    lines.append(f"  coverage ratio: {ratio_str}")
    lines.append("")

    lines.append(
        f"Bucket width: {bucket_seconds}s (ts has roughly 10s of confirmed benign local "
        f"reordering between adjacent posts from concurrent server ingestion; the bucket is "
        f"never made finer than that noise floor)."
    )
    lines.append("")

    def _ratio_str(ratio):
        return f"{ratio:.2f}x" if ratio is not None else "n/a"

    lines.append(f"Per-template timing (top-{top_n} shared templates by distinct-key count):")
    if not stats["templates"]:
        lines.append("  (none -- no shared template had a parseable timestamp)")
    else:
        for entry in stats["templates"]:
            lines.append(f"  [{entry['distinct_keys']} distinct keys] {entry['text']!r}")
            lines.append(
                f"    posts: {entry['post_count']}, active span: "
                f"{entry['active_span_seconds']:.1f}s, buckets: {entry['bucket_count']} "
                f"({entry['occupied_bucket_count']} occupied)"
            )
            lines.append(f"    observed dispersion: {entry['observed_dispersion']:.3f}")
            lines.append(
                f"    dispersion ratio -- uniform (naive): {_ratio_str(entry['dispersion_ratio_uniform'])}, "
                f"room: {_ratio_str(entry['dispersion_ratio_room'])}, "
                f"room-minus-self (HEADLINE): {_ratio_str(entry['dispersion_ratio_room_minus_self'])}"
            )
            lines.append(
                f"    fraction of posts in the single busiest {bucket_seconds:g}s window: "
                f"{entry['busiest_bucket_fraction']:.3f}"
            )
    lines.append("")

    lines.append("Aggregate across the top-N templates:")
    median_headline = stats["median_dispersion_ratio_room_minus_self"]
    max_headline = stats["max_dispersion_ratio_room_minus_self"]
    if median_headline is None:
        lines.append("  no templates measured -- no aggregate to report.")
    else:
        lines.append(
            f"  median dispersion ratio, room-minus-self (HEADLINE): {median_headline:.2f}x "
            f"(max: {max_headline:.2f}x)"
        )
        lines.append(
            f"  median dispersion ratio, room:    "
            f"{_ratio_str(stats['median_dispersion_ratio_room'])} "
            f"(max: {_ratio_str(stats['max_dispersion_ratio_room'])})"
        )
        lines.append(
            f"  median dispersion ratio, uniform (naive): "
            f"{_ratio_str(stats['median_dispersion_ratio_uniform'])} "
            f"(max: {_ratio_str(stats['max_dispersion_ratio_uniform'])})"
        )
        for threshold in RATIO_THRESHOLDS:
            count = stats["ratio_threshold_counts"][str(threshold)]
            lines.append(
                f"  {count} of {len(stats['templates'])} top templates have a room-minus-self "
                f"dispersion ratio above {threshold:g}x"
            )
    lines.append("")

    lines.append(
        "This is a FLOOR: it rests on ts (confirmed ~10s benign local reordering, see "
        "the bucket-width note above) and is measured only at the coverage ratio stated above."
    )
    lines.append(
        "Three null models, in increasing rigor: uniform (every bucket equally likely, the "
        "naive baseline), room (weighted by the room's own per-bucket activity, so a "
        "template that merely rides the crowd reads near 1), and room-minus-self (the same "
        "room weighting with this template's own posts subtracted out first, so it is never "
        "compared against a baseline partly built from itself). The room-minus-self ratio is "
        "the HEADLINE: only an elevation there indicates timing coordination beyond the "
        "room's own rhythm. If room and room-minus-self agree, the finding is robust; a "
        "template that reads high on uniform or room but near 1 on room-minus-self is not "
        "bursty, it is only tracking the room. Below 1 on room-minus-self is more even than "
        "even the room's own rhythm, consistent with metronomic heartbeat posting. The "
        "busiest-bucket fraction above is a plain descriptive secondary number, not a "
        "null-model comparison. Either way this is a statement about the timing shape of "
        "the traffic, not a verdict about any poster."
    )
    lines.append("")
    lines.append(f"Caveat: {CAVEAT}")

    if stats["malformed_lines_skipped"] or stats["ts_unparsable_skipped"]:
        lines.append("")
        if stats["malformed_lines_skipped"]:
            lines.append(
                f"Note: {stats['malformed_lines_skipped']} unparseable line(s) in "
                "messages.jsonl were skipped."
            )
        if stats["ts_unparsable_skipped"]:
            lines.append(
                f"Note: {stats['ts_unparsable_skipped']} re-verified post(s) had a "
                "missing or unparseable ts and were skipped from the timing analysis."
            )

    return "\n".join(lines)


def default_out_path(data_dir, room):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return os.path.join(data_dir, "analysis", f"synchrony_{room}_{ts}.json")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Timing synchrony (burst vs. spread) over a room's already-collected "
        "messages (read-only)."
    )
    parser.add_argument("--data-dir", required=True, help="collector data directory to read")
    parser.add_argument("--room", default="lobby", help="room to analyze (default: lobby)")
    parser.add_argument(
        "--top-n",
        type=int,
        default=DEFAULT_TOP_N,
        help=f"number of top shared templates to analyze (default: {DEFAULT_TOP_N})",
    )
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
        "(default: <data-dir>/analysis/synchrony_<room>_<ts>.json)",
    )
    args = parser.parse_args(argv)

    stats = compute_synchrony_stats(
        args.data_dir, room=args.room, top_n=args.top_n, bucket_seconds=args.bucket_seconds
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
