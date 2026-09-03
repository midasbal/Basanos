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


def _expected_dispersion_analytic(bucket_count):
    """Closed-form large-sample expectation of the index of dispersion for
    `post_count` posts scattered uniformly at random across `bucket_count`
    buckets: approximately (1 - 1/bucket_count), independent of post_count.
    Approaches 1 as bucket_count grows. None when undefined (fewer than 2
    buckets, same condition `_index_of_dispersion` uses).
    """
    if bucket_count < 2:
        return None
    return 1.0 - (1.0 / bucket_count)


def _expected_dispersion_simulated(post_count, bucket_count):
    """The index of dispersion a uniformly-random null model would
    actually produce at this specific, possibly small, post_count: `post_count`
    posts scattered independently and uniformly at random across
    `bucket_count` buckets, averaged over NULL_MODEL_TRIALS trials of the
    identical `_index_of_dispersion` measurement.

    The analytic (1 - 1/bucket_count) figure above is a large-sample
    approximation; at small post_count (exactly the regime a handful of
    signing keys produces) the finite-sample expectation can differ
    meaningfully, so this simulated value, not the analytic one, is what
    `dispersion_ratio` divides by. Seeded deterministically from
    (post_count, bucket_count) alone, the same scheme and same
    NULL_MODEL_TRIALS as before -- no OS randomness, no wall clock, so the
    same template shape always reproduces the exact same expectation, run
    to run, machine to machine.
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


def compute_template_synchrony(ts_values, bucket_seconds):
    """The per-template timing-synchrony numbers for one shared template's
    re-verified posts.

    `ts_values`: a non-empty list of epoch-second floats (one per
    re-verified post carrying that template's text). Returns a dict with
    the active span, bucketing, the observed index of dispersion of posts
    across ALL buckets (including empty ones), both the analytic and
    simulated null-model expectations for the same shape, their ratio
    (the headline: `dispersion_ratio`), and a plain descriptive secondary
    stat, `busiest_bucket_fraction`.

    `dispersion_ratio` >> 1 is bursty (coordination-consistent); ~1 is
    random-consistent; < 1 is MORE even than random, i.e.
    metronomic/heartbeat-consistent -- unlike a single-busiest-bucket
    measure alone, this also catches perfectly-even posting, which reads
    near or below the busiest-bucket floor exactly the same way random
    posting can.
    """
    post_count = len(ts_values)
    earliest = min(ts_values)
    latest = max(ts_values)
    active_span = latest - earliest

    bucket_count = int(active_span // bucket_seconds) + 1

    bucket_counts_all = [0] * bucket_count
    for t in ts_values:
        idx = int((t - earliest) // bucket_seconds)
        bucket_counts_all[idx] += 1

    observed_dispersion = _index_of_dispersion(bucket_counts_all, bucket_count)
    expected_analytic = _expected_dispersion_analytic(bucket_count)
    expected_simulated = _expected_dispersion_simulated(post_count, bucket_count)
    if observed_dispersion is not None and expected_simulated:
        dispersion_ratio = observed_dispersion / expected_simulated
    else:
        dispersion_ratio = None

    busiest_bucket_fraction = max(bucket_counts_all) / post_count

    return {
        "post_count": post_count,
        "active_span_seconds": active_span,
        "bucket_count": bucket_count,
        "occupied_bucket_count": sum(1 for c in bucket_counts_all if c > 0),
        "observed_dispersion": observed_dispersion,
        "expected_dispersion_analytic": expected_analytic,
        "expected_dispersion_simulated": expected_simulated,
        "dispersion_ratio": dispersion_ratio,
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
            except (UnsupportedKeyType, MalformedRecord, KeyError):
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

    # A "shared template": exact text signed by >= 2 distinct DIDs, ranked by
    # distinct-key count -- the same population and ordering coordination.py
    # uses for its own top-N.
    shared_templates = {t for t, dids in text_to_dids.items() if len(dids) >= 2}
    ranked_templates = sorted(shared_templates, key=lambda t: (-len(text_to_dids[t]), t))
    top_n_templates = ranked_templates[:top_n]

    template_reports = []
    ratios = []
    for t in top_n_templates:
        ts_values = text_to_ts.get(t, [])
        if not ts_values:
            continue  # no parseable timestamp for any post of this template
        synchrony = compute_template_synchrony(ts_values, bucket_seconds)
        template_reports.append({"text": t, "distinct_keys": len(text_to_dids[t]), **synchrony})
        # dispersion_ratio is None only when a template's own active span
        # is too short to have more than one bucket -- excluded from the
        # aggregate rather than treated as 0 or skewing the count.
        if synchrony["dispersion_ratio"] is not None:
            ratios.append(synchrony["dispersion_ratio"])

    median_ratio = statistics.median(ratios) if ratios else None
    max_ratio = max(ratios) if ratios else None
    ratio_threshold_counts = {
        str(threshold): sum(1 for r in ratios if r > threshold) for threshold in RATIO_THRESHOLDS
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
        "median_dispersion_ratio": median_ratio,
        "max_dispersion_ratio": max_ratio,
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
    coverage ratio captured below. A dispersion ratio much greater than 1
    is evidence of coordinated timing, consistent with a shared scheduler;
    a ratio near 1 is consistent with independent random-timed posting;
    a ratio below 1 is MORE even than random, consistent with metronomic
    heartbeat posting. Either way this is a statement about the timing
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
            ratio = entry["dispersion_ratio"]
            ratio_str = f"{ratio:.2f}x" if ratio is not None else "n/a (span too short for a second bucket)"
            lines.append(
                f"    observed dispersion: {entry['observed_dispersion']:.3f}, "
                f"expected (simulated null model): {entry['expected_dispersion_simulated']:.3f}, "
                f"expected (analytic, large-sample): {entry['expected_dispersion_analytic']:.3f}, "
                f"dispersion ratio: {ratio_str}"
            )
            lines.append(
                f"    fraction of posts in the single busiest {bucket_seconds:g}s window: "
                f"{entry['busiest_bucket_fraction']:.3f}"
            )
    lines.append("")

    lines.append("Aggregate across the top-N templates:")
    median_ratio = stats["median_dispersion_ratio"]
    max_ratio = stats["max_dispersion_ratio"]
    if median_ratio is None:
        lines.append("  no templates measured -- no aggregate to report.")
    else:
        lines.append(f"  median dispersion ratio: {median_ratio:.2f}x")
        lines.append(f"  max dispersion ratio:    {max_ratio:.2f}x")
        for threshold in RATIO_THRESHOLDS:
            count = stats["ratio_threshold_counts"][str(threshold)]
            lines.append(
                f"  {count} of {len(stats['templates'])} top templates have a dispersion "
                f"ratio above {threshold:g}x"
            )
    lines.append("")

    lines.append(
        "This is a FLOOR: it rests on ts (confirmed ~10s benign local reordering, see "
        "the bucket-width note above) and is measured only at the coverage ratio stated above."
    )
    lines.append(
        "The dispersion ratio is the headline: much greater than 1 is bursty, evidence of "
        "coordinated timing consistent with a shared scheduler; near 1 is consistent with "
        "independent random-timed posting; below 1 is more even than random, consistent "
        "with metronomic heartbeat posting. The busiest-bucket fraction above is a plain "
        "descriptive secondary number, not a null-model comparison. Either way this is a "
        "statement about the timing shape of the traffic, not a verdict about any poster."
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
