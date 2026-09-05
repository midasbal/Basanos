"""Scheduled analysis runner: a single round of the LONGITUDINAL Basanos
measurements, on comparable fixed-shape windows, archived so a real trend
can be read back over time.

THE DEFAULT SCHEDULED ROUND RUNS THE LONGITUDINAL MEASUREMENTS ONLY
(cohort, diurnal, selfaudit), NOT the five window-dependent ones
(duplication, coordination, nonce, clustering, diversity). Those five have
no bounded read at all: every one of their compute functions streams and
re-verifies the ENTIRE `messages.jsonl` for the room on every call (see
WINDOW-DEPENDENT below), which on a real, multi-million-record file is a
multi-hour, memory-heavy pass each -- clustering's pairwise counter over
every bounded template being the worst of the five. Running all eight
every round, as an earlier version of this module did, is wrong for a
SCHEDULED job: it turns a round meant to finish in minutes into a job that
can run for hours and risks memory pressure. The five window-dependent
measurements are still available, but only behind the explicit
`--include-snapshots` flag (`include_snapshots=True` to `run_once`); they
are never part of the default path. See COST below for the honest caveat
about the three longitudinal ones themselves.

COST, STATED HONESTLY: cohort, diurnal, and selfaudit are "bounded window"
measurements only in the sense that their OUTPUT is scoped to a fixed
window -- each one's own compute function still streams and re-verifies
the WHOLE `messages.jsonl` for the room every call, exactly like the five
window-dependent ones, and only filters to the window's ts range AFTER
that full pass (confirmed by reading each: `compute_cohort_stats`,
`compute_diurnal_stats`, and `compute_selfaudit_stats` all loop over
every record `_iter_json_lines(messages_path)` yields, unconditionally).
Excluding the five window-dependent measurements from the default round
cuts a round from 8 full-file re-verify passes to 3, which is the fix this
revision makes -- but it does NOT make the 3 remaining passes themselves
bounded. On a multi-million-record file, 3 full re-verify passes is far
better than 8, but each one is still a full-file pass and can still take
minutes, not the instant a truly windowed read would give. Making
cohort/diurnal/selfaudit's own compute functions read only their window
would require changing those modules, which is out of scope here (this
revision touches only `analysis/runner.py` and its own test file); it is
recorded here as a known remaining cost, not claimed as fixed.

Read-only by construction: this module only reads a room's already-stored
`<data-dir>/rooms/<room>/messages.jsonl`, `<data-dir>/coverage.jsonl`,
`<data-dir>/coverage_state.json` (via `collector.coverage.CoverageTracker`),
and `<data-dir>/rooms_snapshots.jsonl` -- exactly what the measurement
modules it calls already read. It writes to exactly one place:
`<out-dir>/<measurement>.jsonl` (default `<data-dir>/analysis/history/`),
append-only. It never writes to, or modifies, any collector data file, and
it touches no key material at all -- see KEY SAFETY below.

INVOCATION MODEL: this is a single-shot command, not a daemon. One call
does one round and exits; scheduling it (cron, a systemd timer) is the
caller's job, not this module's. This keeps it simple and testable: no
sleep loop, no signal handling, no in-process state carried between runs
except what is already on disk in the history files themselves.

Unlike every sibling analysis module (each of which intentionally
duplicates its own small streaming/re-verify walk, to stay a single
self-contained, independently-auditable read), this module's whole job is
orchestration: it imports and calls the sibling modules' real compute
functions directly, never reimplementing a measurement itself. It DOES
duplicate the small restart-detection / coverage-differencing helpers
below (the same functions `analysis/diurnal.py` and `analysis/cohort.py`
already carry, per this project's "duplicate rather than share" reading
convention) because span detection is this module's own job, not a
measurement, and needs no sibling's internals to do it.

THE CORE DESIGN PRINCIPLE, THE WHOLE POINT OF THIS MODULE: Basanos's
measurements split into two kinds, and this runner treats them
differently on purpose.

LONGITUDINAL (comparable across runs, on a FIXED window shape slid
forward in time as the run repeats): `analysis/cohort.py` (a fixed
cohort-window / gap / return-window shape), `analysis/diurnal.py` (a
fixed-length span), `analysis/selfaudit.py` (re-run as capture
accumulates -- its own per-snapshot window reconstruction is already
comparable run over run, with no shape parameter of this runner's own to
manage). Successive runs of these on the same fixed shape ARE a trend;
each archived record carries the exact window bounds used so that is
auditable later, not just asserted.

WINDOW-DEPENDENT (their headline numbers depend on how much of the
ever-growing file was read, so re-running them on the whole file and
calling the result a series would be exactly the error this project has
already corrected once in FINDINGS.md -- nested windows are not a time
series): `analysis/duplication.py`, `analysis/coordination.py`,
`analysis/nonce.py`, `analysis/clustering.py`, `analysis/diversity.py`.
None of these five compute functions accepts a window/time-bound
argument at all (confirmed by reading each one: every one of them streams
the whole `messages.jsonl` for the room, every call); building a
fixed-size trailing window for them without one would mean this runner
constructing its own filtered copy of the input files, including a
re-baselined `coverage_state.json` -- exactly the kind of bespoke,
measurement-adjacent reimplementation this project's modules exist to
avoid doing quietly. So when a caller explicitly opts in
(`--include-snapshots` / `include_snapshots=True`), this runner takes the
other option the task allows: it runs each of the five on the current
whole file, unmodified, through the sibling module's own real compute
function, and archives the result explicitly labeled
`"window_kind": "snapshot"` with an `"as_of"` timestamp -- a point-in-time
read, never presented as a comparable trend point (`SNAPSHOT_NOTE` states
this plainly in every such record). They are NOT run by default: a full
re-verify pass over a real, multi-million-record file, five times every
scheduled round, is the wrong cost to pay on a schedule meant to finish in
minutes (see COST above).

RESTART-SEAM DETECTION: a window must never straddle a collector restart,
because a restart is where the collector's own capture continuity broke,
not the underlying room's. This runner detects seams the exact way
`analysis/diurnal.py` and `analysis/cohort.py` already do: `coverage.jsonl`
holds periodic CUMULATIVE per-room counters that only ever grow within one
collector lifetime and reset to (0, 0) on every restart (see
`collector.coverage.CoverageTracker`), so a negative delta between two
consecutive snapshots means a restart happened between them. The
CONTINUOUS SPAN available to build fixed-shape windows in is bounded to
the run of coverage snapshots since the LAST such restart, up to the
latest snapshot -- never a span that crosses one. If a given
measurement's fixed shape does not fit inside that continuous span, this
round SKIPS that measurement and records why, rather than shrinking the
window to fit (a shrunk window is not the fixed shape any more, and would
silently break comparability with every other archived point). If
`coverage.jsonl` itself is not found, there is no restart information to
work from at all; this runner falls back to the plain ts range covered by
`messages.jsonl`, states that fallback plainly in the run summary, and
proceeds without restart-seam protection (there is nothing to detect a
seam from).

KEY SAFETY: this module never imports, reads, or references any private
key material, any PEM file, any passphrase, or a signing call. The only
things it imports are already-key-free compute functions from sibling
analysis modules (each of which only ever imports `Ed25519PublicKey` for
verification, never `Ed25519PrivateKey`). It has no function that
produces a signature.

AGGREGATE-ONLY: every archived record is exactly the already-audited
stats dict a sibling module's own compute function returns (each of those
is already covered by that module's own aggregate-only tests), wrapped in
this runner's own envelope (timestamp, window bounds or snapshot label,
coverage). This runner adds no new per-key field of its own anywhere.

Usage:
    python -m analysis.runner --data-dir <dir> [--room lobby] \\
        [--cohort-window-hours 4] [--cohort-gap-hours 12] \\
        [--diurnal-span-hours 24] [--out-dir <history-dir>]
"""

import argparse
import json
import os
import re
from datetime import datetime, timezone

from analysis.clustering import compute_clustering_stats
from analysis.cohort import compute_cohort_stats
from analysis.coordination import compute_coordination_stats
from analysis.diurnal import DEFAULT_BUCKET_SECONDS, compute_diurnal_stats
from analysis.diversity import compute_diversity_stats
from analysis.duplication import compute_duplication_stats
from analysis.nonce import compute_nonce_stats
from analysis.selfaudit import compute_selfaudit_stats

DEFAULT_COHORT_WINDOW_HOURS = 4.0
DEFAULT_COHORT_GAP_HOURS = 12.0
DEFAULT_DIURNAL_SPAN_HOURS = 24.0

SNAPSHOT_NOTE = (
    "point-in-time snapshot over the whole captured file to date: this measurement's "
    "compute function has no window/time-bound argument, so its headline numbers depend "
    "on how much of the ever-growing file was read. This record is NOT a trend point and "
    "must never be compared against another snapshot of this same measurement as if the "
    "difference were a change over time -- see the module docstring's window-dependent "
    "section."
)

# name -> the sibling compute function, called with just (data_dir, room=room),
# i.e. every one of that sibling's own defaults for every other parameter.
_WINDOW_DEPENDENT_COMPUTE_FNS = {
    "duplication": compute_duplication_stats,
    "coordination": compute_coordination_stats,
    "nonce": compute_nonce_stats,
    "clustering": compute_clustering_stats,
    "diversity": compute_diversity_stats,
}

LONGITUDINAL_MEASUREMENTS = ("cohort", "diurnal", "selfaudit")
WINDOW_DEPENDENT_MEASUREMENTS = tuple(_WINDOW_DEPENDENT_COMPUTE_FNS)


_VALID_ROOM_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_room(room):
    """Reject a room name that could escape the intended directory when
    used in os.path.join -- a room containing "/" or ".." would let
    --room build a path outside <data-dir>/rooms/ on read or outside
    <out-dir>/ on write. Every real room name (lobby, meta,
    fixture-room-... in the fixtures) matches this pattern; nothing valid
    is rejected. Raised before any path is built or any file is opened or
    created.
    """
    if not _VALID_ROOM_RE.match(room):
        raise ValueError(
            f"invalid room {room!r}: must match {_VALID_ROOM_RE.pattern} "
            "(letters, digits, underscore, hyphen only)"
        )


def _iter_json_lines(path):
    """Stream a JSONL file one record at a time. Never loads the whole
    file into memory. A line that isn't valid JSON is skipped, never a
    crash -- this also means a partial final line from a file the
    collector is concurrently appending to (this runner tolerates reading
    while the collector writes; a torn last line is simply not yet valid
    JSON and is skipped, exactly like any other malformed line, and will
    be picked up whole on the next scheduled run) never causes a crash.
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
    "2026-09-02T15:36:03.288522Z") into epoch seconds (a float). Returns
    None, never raises, on anything that isn't parseable. The trailing
    "Z" is normalized to "+00:00" by hand rather than relying on
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
    dropped_total) tuples, sorted by captured_at. Duplicated from
    `analysis/diurnal.py` and `analysis/cohort.py`'s function of the same
    name and behavior, per this project's convention of each module owning
    its own self-contained read.
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


def determine_continuous_span(data_dir, room):
    """The continuous, seam-free span available right now to build
    fixed-shape windows inside, in epoch seconds.

    Reads `<data_dir>/coverage.jsonl`'s cumulative per-room snapshots for
    `room`: a negative delta between two consecutive snapshots (in either
    counter) means a collector restart happened between them, the same
    detection `analysis/diurnal.py` and `analysis/cohort.py` already apply
    to individual intervals. Here it is applied across the whole history
    to find the LATEST unbroken run: the span starts right after the most
    recent restart (or at the very first snapshot if there was none) and
    ends at the latest snapshot.

    If `coverage.jsonl` is not found, or has fewer than two snapshots for
    this room, there is no restart information to work from. Falls back
    to the plain ts range covered by re-verifiable data in
    `<data_dir>/rooms/<room>/messages.jsonl` -- every message's `ts`,
    parsed, regardless of signature status, since this is only used to
    bound a window in time, not to compute anything about a specific
    poster. That fallback carries no restart protection at all, which is
    stated in the returned dict's `span_source` rather than left silent.

    Returns a dict: span_start_seconds, span_end_seconds (both None if no
    usable data exists at all), span_source ("coverage",
    "messages_fallback", or "no_data"), coverage_file_found,
    restarts_detected (None when span_source is not "coverage").
    """
    coverage_path = os.path.join(data_dir, "coverage.jsonl")
    messages_path = os.path.join(data_dir, "rooms", room, "messages.jsonl")
    coverage_found = os.path.exists(coverage_path)

    if coverage_found:
        records, _malformed = _read_room_coverage_records(coverage_path, room)
        if len(records) >= 2:
            run_start = records[0][0]
            restarts_detected = 0
            for (_, c0, d0), (t1, c1, d1) in zip(records, records[1:]):
                if (c1 - c0) < 0 or (d1 - d0) < 0:
                    restarts_detected += 1
                    run_start = t1
            span_end = records[-1][0]
            if span_end > run_start:
                return {
                    "span_start_seconds": run_start,
                    "span_end_seconds": span_end,
                    "span_source": "coverage",
                    "coverage_file_found": True,
                    "restarts_detected": restarts_detected,
                }

    if os.path.exists(messages_path):
        ts_values = []
        for record in _iter_json_lines(messages_path):
            if not isinstance(record, dict):
                continue
            parsed = _parse_ts_seconds(record.get("ts"))
            if parsed is not None:
                ts_values.append(parsed)
        if ts_values:
            return {
                "span_start_seconds": min(ts_values),
                "span_end_seconds": max(ts_values),
                "span_source": "messages_fallback",
                "coverage_file_found": coverage_found,
                "restarts_detected": None,
            }

    return {
        "span_start_seconds": None,
        "span_end_seconds": None,
        "span_source": "no_data",
        "coverage_file_found": coverage_found,
        "restarts_detected": None,
    }


def _position_cohort_windows(span, window_hours, gap_hours):
    """Position the cohort measurement's fixed shape (a window-hours-sized
    cohort window, a gap-hours-sized gap, then a window-hours-sized return
    window -- symmetric, both windows the same fixed size) as late as
    possible within `span`, so successive scheduled runs slide the same
    shape forward as the continuous span grows.

    Returns (w1_start, w1_end, w2_start, w2_end, required_span_seconds) if
    the shape fits inside span; returns None if it does not (the caller
    skips this round for this measurement rather than shrinking the
    shape).
    """
    window_seconds = window_hours * 3600.0
    gap_seconds = gap_hours * 3600.0
    required_span_seconds = 2 * window_seconds + gap_seconds

    span_start = span["span_start_seconds"]
    span_end = span["span_end_seconds"]
    if span_start is None or span_end is None:
        return None

    w2_end = span_end
    w2_start = w2_end - window_seconds
    w1_end = w2_start - gap_seconds
    w1_start = w1_end - window_seconds

    if w1_start < span_start:
        return None
    return w1_start, w1_end, w2_start, w2_end, required_span_seconds


def _position_diurnal_window(span, span_hours):
    """Position the diurnal measurement's fixed-length span as late as
    possible within the available continuous `span`. Returns (d_start,
    d_end, required_span_seconds) if it fits, None if it does not.
    """
    span_seconds = span_hours * 3600.0
    span_start = span["span_start_seconds"]
    span_end = span["span_end_seconds"]
    if span_start is None or span_end is None:
        return None

    d_end = span_end
    d_start = d_end - span_seconds
    if d_start < span_start:
        return None
    return d_start, d_end, span_seconds


def _diurnal_window_summary(data_dir, room, d_start, d_end, bucket_seconds):
    """Run the real `compute_diurnal_stats` over the whole file (its own
    compute function has no window argument, so this is the entire
    already-verified, already-binned curve), then select and aggregate
    only the bins whose `bin_start_ts` falls inside [d_start, d_end).

    This is composition, not reimplementation: every bin's own numbers
    (captured/dropped counts, coverage ratio) come from
    `analysis/diurnal.py`'s own per-message re-verification and per-bin
    coverage-differencing, untouched; this function only sums an
    already-computed slice and recomputes the one ratio that summing
    implies.

    Returns (full_stats, window_summary). window_summary is None if there
    were no bins at all (messages file missing, or nothing re-verified) --
    the caller treats that the same as any other empty result.
    """
    full_stats = compute_diurnal_stats(data_dir, room=room, bucket_seconds=bucket_seconds)
    if not full_stats["bins"]:
        return full_stats, None

    selected_bins = [
        b for b in full_stats["bins"] if d_start <= _parse_ts_seconds(b["bin_start_ts"]) < d_end
    ]
    total_captured = sum(b["captured_posts"] for b in selected_bins)
    total_dropped = sum(b["estimated_dropped"] for b in selected_bins)
    denominator = total_captured + total_dropped
    coverage_ratio = (total_captured / denominator) if denominator else None

    window_summary = {
        "bucket_seconds": bucket_seconds,
        "num_bins": len(selected_bins),
        "total_captured_posts": total_captured,
        "total_estimated_dropped": total_dropped,
        "overall_coverage_ratio": coverage_ratio,
        "bins": selected_bins,
    }
    return full_stats, window_summary


def _archive_record(out_dir, measurement, record):
    """Append `record` as one JSON line to `<out_dir>/<measurement>.jsonl`,
    creating `out_dir` if needed. Append-only: an existing file is never
    truncated or rewritten, only ever grown by one line per call, so the
    history accumulates into a readable trend across scheduled runs.
    """
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{measurement}.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
        f.write("\n")
    return path


def run_once(
    data_dir,
    room="lobby",
    cohort_window_hours=DEFAULT_COHORT_WINDOW_HOURS,
    cohort_gap_hours=DEFAULT_COHORT_GAP_HOURS,
    diurnal_span_hours=DEFAULT_DIURNAL_SPAN_HOURS,
    out_dir=None,
    include_snapshots=False,
):
    """Run one scheduled round: the longitudinal measurements on their
    fixed-shape windows (skipping any that do not fit in the current
    continuous span). Archives each result and returns a summary dict of
    what ran, what was skipped, and where each was archived -- the same
    dict `format_run_report` renders and the CLI prints, and what the
    tests assert against directly.

    The five window-dependent measurements (duplication, coordination,
    nonce, clustering, diversity) are NOT run by default -- each one's
    compute function re-verifies the whole `messages.jsonl` for the room
    with no bounded read at all, making them the wrong thing to run every
    scheduled round (see the module docstring's COST section). Pass
    `include_snapshots=True` to also run and archive them, each labeled
    `"window_kind": "snapshot"`, exactly as before this revision.

    Read-only against every collector data file; writes only under
    `out_dir` (default `<data_dir>/analysis/history`).
    """
    _validate_room(room)
    if out_dir is None:
        out_dir = os.path.join(data_dir, "analysis", "history")

    run_timestamp = datetime.now(timezone.utc)
    run_timestamp_iso = run_timestamp.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"

    span = determine_continuous_span(data_dir, room)

    ran = []
    skipped = []

    # --- cohort (longitudinal) ---
    cohort_window = _position_cohort_windows(span, cohort_window_hours, cohort_gap_hours)
    if cohort_window is None:
        available_seconds = (
            (span["span_end_seconds"] - span["span_start_seconds"])
            if span["span_start_seconds"] is not None
            else 0.0
        )
        required_seconds = 2 * cohort_window_hours * 3600.0 + cohort_gap_hours * 3600.0
        skipped.append(
            {
                "measurement": "cohort",
                "run_timestamp": run_timestamp_iso,
                "status": "skipped",
                "reason": "insufficient continuous span for the fixed cohort/gap/return shape",
                "required_span_hours": required_seconds / 3600.0,
                "available_span_hours": available_seconds / 3600.0,
                "span_source": span["span_source"],
            }
        )
    else:
        w1_start, w1_end, w2_start, w2_end, _required = cohort_window
        result = compute_cohort_stats(data_dir, room, w1_start, w1_end, w2_start, w2_end)
        record = {
            "measurement": "cohort",
            "run_timestamp": run_timestamp_iso,
            "window_kind": "fixed_shape",
            "w1_start": _iso_from_seconds(w1_start),
            "w1_end": _iso_from_seconds(w1_end),
            "w2_start": _iso_from_seconds(w2_start),
            "w2_end": _iso_from_seconds(w2_end),
            "result": result,
        }
        path = _archive_record(out_dir, "cohort", record)
        ran.append({"measurement": "cohort", "kind": "fixed_shape", "archive_path": path})

    # --- diurnal (longitudinal) ---
    diurnal_window = _position_diurnal_window(span, diurnal_span_hours)
    if diurnal_window is None:
        available_seconds = (
            (span["span_end_seconds"] - span["span_start_seconds"])
            if span["span_start_seconds"] is not None
            else 0.0
        )
        skipped.append(
            {
                "measurement": "diurnal",
                "run_timestamp": run_timestamp_iso,
                "status": "skipped",
                "reason": "insufficient continuous span for the fixed diurnal span",
                "required_span_hours": diurnal_span_hours,
                "available_span_hours": available_seconds / 3600.0,
                "span_source": span["span_source"],
            }
        )
    else:
        d_start, d_end, _required = diurnal_window
        full_stats, window_summary = _diurnal_window_summary(
            data_dir, room, d_start, d_end, DEFAULT_BUCKET_SECONDS
        )
        if window_summary is None:
            skipped.append(
                {
                    "measurement": "diurnal",
                    "run_timestamp": run_timestamp_iso,
                    "status": "skipped",
                    "reason": "no re-verified messages available to bin",
                    "required_span_hours": diurnal_span_hours,
                    "available_span_hours": (
                        (span["span_end_seconds"] - span["span_start_seconds"]) / 3600.0
                        if span["span_start_seconds"] is not None
                        else 0.0
                    ),
                    "span_source": span["span_source"],
                }
            )
        else:
            record = {
                "measurement": "diurnal",
                "run_timestamp": run_timestamp_iso,
                "window_kind": "fixed_shape",
                "window_start": _iso_from_seconds(d_start),
                "window_end": _iso_from_seconds(d_end),
                "coverage_file_found": full_stats["coverage_file_found"],
                "messages_file_found": full_stats["messages_file_found"],
                "result": window_summary,
            }
            path = _archive_record(out_dir, "diurnal", record)
            ran.append({"measurement": "diurnal", "kind": "fixed_shape", "archive_path": path})

    # --- selfaudit (longitudinal, no fixed shape of its own -- re-run as
    # capture accumulates; its own per-snapshot window reconstruction is
    # already comparable run over run) ---
    selfaudit_result = compute_selfaudit_stats(data_dir, room=room)
    selfaudit_record = {
        "measurement": "selfaudit",
        "run_timestamp": run_timestamp_iso,
        "window_kind": "cumulative",
        "result": selfaudit_result,
    }
    path = _archive_record(out_dir, "selfaudit", selfaudit_record)
    ran.append({"measurement": "selfaudit", "kind": "cumulative", "archive_path": path})

    # --- window-dependent measurements: NOT part of the default round (see
    # the module docstring and run_once's own docstring). Each one streams
    # and re-verifies the whole file with no bounded read at all, which is
    # the wrong cost to pay every scheduled round. Only run, and only
    # archived as a labeled snapshot, never as a trend point, when the
    # caller explicitly opts in. ---
    if include_snapshots:
        as_of_iso = (
            _iso_from_seconds(span["span_end_seconds"])
            if span["span_end_seconds"] is not None
            else run_timestamp_iso
        )
        for name, compute_fn in _WINDOW_DEPENDENT_COMPUTE_FNS.items():
            result = compute_fn(data_dir, room=room)
            record = {
                "measurement": name,
                "run_timestamp": run_timestamp_iso,
                "window_kind": "snapshot",
                "as_of": as_of_iso,
                "note": SNAPSHOT_NOTE,
                "result": result,
            }
            path = _archive_record(out_dir, name, record)
            ran.append({"measurement": name, "kind": "snapshot", "archive_path": path})

    return {
        "run_timestamp": run_timestamp_iso,
        "room": room,
        "out_dir": out_dir,
        "span": span,
        "ran": ran,
        "skipped": skipped,
        "include_snapshots": include_snapshots,
    }


def format_run_report(summary):
    """Render the human-readable summary of one `run_once` call: what ran,
    what was skipped and why, and where each result was archived.
    """
    lines = []
    lines.append(f"Scheduled analysis run -- room: {summary['room']}")
    lines.append("=" * (26 + len(summary["room"])))
    lines.append("")
    lines.append(f"Run timestamp: {summary['run_timestamp']}")

    span = summary["span"]
    if span["span_source"] == "no_data":
        lines.append("Continuous span: no data found at all (no messages, no coverage log).")
    else:
        start = _iso_from_seconds(span["span_start_seconds"])
        end = _iso_from_seconds(span["span_end_seconds"])
        lines.append(f"Continuous span: [{start}, {end}] (source: {span['span_source']})")
        if span["span_source"] == "messages_fallback":
            lines.append(
                "  no coverage.jsonl found -- this span carries no restart-seam protection, "
                "it is simply the ts range messages.jsonl covers."
            )
        elif span["restarts_detected"]:
            lines.append(
                f"  {span['restarts_detected']} restart(s) detected in the full coverage "
                "history; the span above starts after the most recent one."
            )
    lines.append("")

    lines.append(
        "Default scope: the longitudinal trend measurements only "
        "(cohort, diurnal, selfaudit)."
    )
    if summary["include_snapshots"]:
        lines.append(
            "--include-snapshots was passed: the five window-dependent measurements "
            "(duplication, coordination, nonce, clustering, diversity) also ran this round, "
            "each a full re-verify pass over the whole file -- expensive, and archived below "
            "as point-in-time snapshots, never as a trend."
        )
    else:
        lines.append(
            "Window-dependent snapshots (duplication, coordination, nonce, clustering, "
            "diversity) were NOT run -- pass --include-snapshots to also run them; each is "
            "a full re-verify pass over the whole messages.jsonl and is expensive on a large "
            "file, so it is opt-in, never part of the default scheduled round."
        )
    lines.append("")

    lines.append(f"Ran ({len(summary['ran'])}):")
    for entry in summary["ran"]:
        lines.append(f"  {entry['measurement']} ({entry['kind']}) -> {entry['archive_path']}")
    lines.append("")

    lines.append(f"Skipped ({len(summary['skipped'])}):")
    for entry in summary["skipped"]:
        lines.append(
            f"  {entry['measurement']}: {entry['reason']} "
            f"(needed {entry['required_span_hours']:.1f}h, had "
            f"{entry['available_span_hours']:.1f}h)"
        )

    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run one scheduled round of the Basanos longitudinal measurements "
        "(cohort, diurnal, selfaudit) on fixed-shape windows (read-only against collector "
        "data; single-shot, not a daemon -- schedule repeated calls externally). The five "
        "window-dependent measurements (duplication, coordination, nonce, clustering, "
        "diversity) are expensive full-file re-verify passes and are NOT run unless "
        "--include-snapshots is passed."
    )
    parser.add_argument("--data-dir", required=True, help="collector data directory to read")
    parser.add_argument("--room", default="lobby", help="room to analyze (default: lobby)")
    parser.add_argument(
        "--cohort-window-hours",
        type=float,
        default=DEFAULT_COHORT_WINDOW_HOURS,
        help=f"cohort/return window size in hours, both windows the same fixed size "
        f"(default: {DEFAULT_COHORT_WINDOW_HOURS})",
    )
    parser.add_argument(
        "--cohort-gap-hours",
        type=float,
        default=DEFAULT_COHORT_GAP_HOURS,
        help=f"gap between the cohort and return windows in hours (default: {DEFAULT_COHORT_GAP_HOURS})",
    )
    parser.add_argument(
        "--diurnal-span-hours",
        type=float,
        default=DEFAULT_DIURNAL_SPAN_HOURS,
        help=f"fixed diurnal span length in hours (default: {DEFAULT_DIURNAL_SPAN_HOURS})",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="history directory to archive into (default: <data-dir>/analysis/history)",
    )
    parser.add_argument(
        "--include-snapshots",
        action="store_true",
        help="also run and archive the five window-dependent measurements (duplication, "
        "coordination, nonce, clustering, diversity) as labeled point-in-time snapshots. "
        "Each is a full re-verify pass over the whole messages.jsonl and is expensive on a "
        "large file; opt-in only, never part of the default scheduled round.",
    )
    args = parser.parse_args(argv)

    summary = run_once(
        args.data_dir,
        room=args.room,
        cohort_window_hours=args.cohort_window_hours,
        cohort_gap_hours=args.cohort_gap_hours,
        diurnal_span_hours=args.diurnal_span_hours,
        out_dir=args.out_dir,
        include_snapshots=args.include_snapshots,
    )
    print(format_run_report(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
