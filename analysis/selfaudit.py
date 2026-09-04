"""Self-audit: the tenth analysis module in the Basanos measurement layer.

Read-only by construction: this module only reads a room's already-stored
`<data-dir>/rooms/<room>/messages.jsonl`, `<data-dir>/rooms_snapshots.jsonl`,
and `<data-dir>/coverage_state.json` (via `collector.coverage.CoverageTracker`,
whose `counters()` method is itself read-only). It never writes to, or
modifies, anything under `<data-dir>` except the analysis output this
module produces itself.

WHAT THIS MODULE IS FOR: every other module in this project measures the
commons. This one turns that same standard back on Basanos's own data
source: `RoomsSnapshotter` already captures the service's own `/rooms`
overview verbatim, including two numbers the platform publishes about
itself, `nick_diversity` and `zero_response_share`. Nothing before this
module ever checks whether those published numbers hold up against the
raw, re-verified record. This does, for the one of the two that can
actually be checked.

NICK_DIVERSITY: CONFIRMED DEFINITION, RECOMPUTABLE. Probed and confirmed
by exact match against a real snapshot: `nick_diversity` is distinct
`from` values divided by message count, over exactly the `window` most
recent messages as of that snapshot's `last_seq` -- the seqs in
`[last_seq - window + 1, last_seq]`. "Nick" here is the `from` field
itself (the signed did:key or unsigned nick string); the message schema
has no separate display-nick field. Because the definition is exact and
the inputs are exactly what this project already captures and
re-verifies, this number can be recomputed from scratch and compared.

ZERO_RESPONSE_SHARE: STRUCTURALLY UNAUDITABLE, STATED AS SUCH. This is
not a computation this module declines to do; it is a fact about the
data. "Response" is not a concept the captured message schema defines --
there is no reply-to, in-reply-to, or thread field anywhere in a stored
message. Whatever the service means by a "response" is not observable in
what Basanos captures, so there is no raw computation to compare the
published number against. This module reports that plainly, states the
reason, and reports the published values' own range for context only --
never a recomputed figure, because there is no way to compute one.

THE HONEST GUARD AGAINST A FALSE DIVERGENCE: `nick_diversity` is defined
over an exact window of `window` messages ending at a specific `last_seq`.
If this module's own capture is missing even one message from that
window, comparing against the published figure would use a smaller,
wrong denominator and could show a "divergence" that is really just a
capture gap, not a disagreement with the platform. So a snapshot is only
ever compared when every single seq in its window has a re-verified
message on record (window_coverage == 1.0, see `compute_selfaudit_stats`
below); every other snapshot is counted as skipped, not compared, and
never contributes to the divergence statistics.

FRAMING, BECAUSE THIS MODULE MAKES CLAIMS ABOUT THE PLATFORM AND HAS TO BE
SCRUPULOUSLY FAIR ABOUT IT: where the exact window can be reconstructed,
if the recomputed and published `nick_diversity` match, that is the
platform's number holding up, not an accusation, and the report states it
that way. `zero_response_share` being unauditable is a statement about
what can be checked, never a claim that the number is wrong. This module
does not interpret what `nick_diversity` means for the population behind
it (whether high nick diversity over a mostly single-use population is
misleading, or expected, or anything else) -- that interpretation, if it
belongs anywhere, belongs in FINDINGS.md's prose, written by a person, not
in this module's output. This module reports three things only: what was
published, what was recomputed, and whether they match. Aggregate only,
FLOOR framing on the coverage this compares against, and no individual
DID ever appears anywhere in this output.

Usage:
    python -m analysis.selfaudit --data-dir <dir> [--room lobby] [--out <path>]
"""

import argparse
import json
import os
import re
from datetime import datetime, timezone

from collector.coverage import CoverageTracker
from collector.verify import MalformedRecord, UnsupportedKeyType, is_signed, verify_record

EPSILON = 0.001

DIVERGENCE_BUCKET_KEYS = ("exact_match", "<0.01", "<0.05", ">=0.05")

CAVEAT = (
    "some rooms may intend heartbeat-style posting, so this is a statement "
    "about the shape of the traffic, not a verdict about any poster."
)

ZERO_RESPONSE_UNAUDITABLE_REASON = (
    "zero_response_share cannot be recomputed from captured data: the message schema has "
    "no reply-to, in-reply-to, or thread field, so \"response\" is not defined anywhere in "
    "what Basanos captures. This states what cannot be checked, not a claim that the "
    "published number is wrong."
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


def _divergence_bucket(divergence):
    if divergence <= EPSILON:
        return "exact_match"
    if divergence < 0.01:
        return "<0.01"
    if divergence < 0.05:
        return "<0.05"
    return ">=0.05"


def compute_selfaudit_stats(data_dir, room="lobby"):
    """Stream `<data_dir>/rooms/<room>/messages.jsonl` and
    `<data_dir>/rooms_snapshots.jsonl` and compare the platform's own
    published `nick_diversity` against a recomputation from re-verified
    raw messages, on the snapshots where the exact window can be
    reconstructed. `zero_response_share` is reported as unauditable, with
    the stated reason, never computed.

    Returns a dict with the raw counters and aggregates needed by both the
    human-readable report and the JSON output. Reads only; writes nothing.
    No did:key string, and no per-snapshot detail, appears anywhere in the
    returned structure -- every number below is a count, a rate, or a
    stated reason, never a name.
    """
    _validate_room(room)
    messages_path = os.path.join(data_dir, "rooms", room, "messages.jsonl")
    snapshots_path = os.path.join(data_dir, "rooms_snapshots.jsonl")

    checked = 0
    verified = 0
    failed = 0
    malformed_lines = 0
    seq_unusable_skipped = 0
    # seq -> from, for re-verified signed messages only
    seq_to_did = {}

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
                # Not used by this module's own computation (nick_diversity
                # never looks at text), but kept for consistency with every
                # sibling: a genuinely valid signature over a non-string
                # text is counted as a re-verify failure like any other
                # record this analysis cannot safely include, not a crash.
                ok = False
            if not ok:
                failed += 1
                continue
            verified += 1
            seq = record.get("seq")
            if not isinstance(seq, int):
                seq_unusable_skipped += 1
                continue
            seq_to_did[seq] = record["from"]

    total_snapshots_seen = 0
    snapshots_with_room_entry = 0
    snapshot_malformed_entries_skipped = 0
    room_snapshot_records = []

    snapshots_found = os.path.exists(snapshots_path)
    if snapshots_found:
        for snapshot in _iter_json_lines(snapshots_path):
            if not isinstance(snapshot, dict):
                snapshot_malformed_entries_skipped += 1
                continue
            total_snapshots_seen += 1
            payload = snapshot.get("payload")
            rooms = payload.get("rooms") if isinstance(payload, dict) else None
            if not isinstance(rooms, list):
                continue
            room_entry = None
            for entry in rooms:
                if isinstance(entry, dict) and entry.get("room") == room:
                    room_entry = entry
                    break
            if room_entry is None:
                continue
            snapshots_with_room_entry += 1

            last_seq = room_entry.get("last_seq")
            window = room_entry.get("window")
            nick_diversity = room_entry.get("nick_diversity")
            zero_response_share = room_entry.get("zero_response_share")
            if (
                not isinstance(last_seq, int)
                or not isinstance(window, int)
                or window <= 0
                or not isinstance(nick_diversity, (int, float))
            ):
                snapshot_malformed_entries_skipped += 1
                continue

            room_snapshot_records.append(
                {
                    "last_seq": last_seq,
                    "window": window,
                    "published_nick_diversity": float(nick_diversity),
                    "published_zero_response_share": (
                        float(zero_response_share)
                        if isinstance(zero_response_share, (int, float))
                        else None
                    ),
                }
            )

    # For each snapshot, attempt to reconstruct its exact nick_diversity
    # window: the seqs [last_seq - window + 1, last_seq]. Only a snapshot
    # whose ENTIRE window has a re-verified message on record is compared
    # -- window_coverage < 1.0 means the true denominator (window) does
    # not match what we could actually count, and comparing anyway could
    # report a "divergence" that is really just our own capture gap, not
    # a disagreement with the platform. See the module docstring's guard
    # section.
    divergences = []
    snapshots_skipped_incomplete_window = 0
    for rec in room_snapshot_records:
        window = rec["window"]
        window_start = rec["last_seq"] - window + 1
        window_dids = []
        for seq in range(window_start, rec["last_seq"] + 1):
            did = seq_to_did.get(seq)
            if did is not None:
                window_dids.append(did)
        window_coverage = len(window_dids) / window
        if window_coverage < 1.0:
            snapshots_skipped_incomplete_window += 1
            continue
        recomputed_nick_diversity = len(set(window_dids)) / window
        divergence = abs(recomputed_nick_diversity - rec["published_nick_diversity"])
        divergences.append(divergence)

    exact_match_count = sum(1 for d in divergences if d <= EPSILON)
    max_divergence = max(divergences) if divergences else None
    mean_divergence = (sum(divergences) / len(divergences)) if divergences else None
    divergence_histogram = {key: 0 for key in DIVERGENCE_BUCKET_KEYS}
    for d in divergences:
        divergence_histogram[_divergence_bucket(d)] += 1

    zero_response_values = [
        rec["published_zero_response_share"]
        for rec in room_snapshot_records
        if rec["published_zero_response_share"] is not None
    ]

    coverage = CoverageTracker(data_dir).counters(room)
    coverage_ratio = CoverageTracker.coverage_ratio(
        coverage.get("captured_total", 0), coverage.get("dropped_total", 0)
    )

    return {
        "room": room,
        "messages_file_found": messages_found,
        "snapshots_file_found": snapshots_found,
        "signed_checked": checked,
        "signed_reverified": verified,
        "signed_reverify_failed": failed,
        "malformed_lines_skipped": malformed_lines,
        "seq_unusable_skipped": seq_unusable_skipped,
        "snapshot_malformed_entries_skipped": snapshot_malformed_entries_skipped,
        "total_snapshots_seen": total_snapshots_seen,
        "snapshots_with_room_entry": snapshots_with_room_entry,
        "snapshots_fully_reconstructable": len(divergences),
        "snapshots_skipped_incomplete_window": snapshots_skipped_incomplete_window,
        "nick_diversity_audit": {
            "compared_count": len(divergences),
            "exact_match_count": exact_match_count,
            "max_absolute_divergence": max_divergence,
            "mean_absolute_divergence": mean_divergence,
            "divergence_histogram": divergence_histogram,
        },
        "zero_response_share_audit": {
            "auditable": False,
            "reason": ZERO_RESPONSE_UNAUDITABLE_REASON,
            "published_min": min(zero_response_values) if zero_response_values else None,
            "published_mean": (
                sum(zero_response_values) / len(zero_response_values) if zero_response_values else None
            ),
            "published_max": max(zero_response_values) if zero_response_values else None,
            "snapshot_count": len(zero_response_values),
        },
        "coverage_captured_total": coverage.get("captured_total", 0),
        "coverage_dropped_total": coverage.get("dropped_total", 0),
        "coverage_ratio": coverage_ratio,
    }


def format_report(stats):
    """Render the human-readable report for `stats` (as returned by
    `compute_selfaudit_stats`).

    Where nick_diversity matches, that is stated as the platform's own
    number holding up, not as a suspicion confirmed; where snapshots
    cannot be compared, that is stated as a limit on what could be
    checked, not as evidence either way; zero_response_share's
    unauditability is stated as a fact about the data, never a claim that
    the number is wrong. Aggregate only, and no individual DID is ever
    named.
    """
    room = stats["room"]
    lines = []
    lines.append(f"Self-audit -- room: {room}")
    lines.append("=" * (14 + len(room)))
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

    coverage_ratio = stats["coverage_ratio"]
    ratio_str = f"{coverage_ratio:.4f}" if coverage_ratio is not None else "n/a"
    lines.append("Coverage:")
    lines.append(f"  captured_total: {stats['coverage_captured_total']}")
    lines.append(f"  dropped_total:  {stats['coverage_dropped_total']}")
    lines.append(f"  coverage ratio: {ratio_str}")
    lines.append("")

    if not stats["snapshots_file_found"]:
        lines.append("No rooms_snapshots.jsonl found for this data dir -- nothing to audit against.")
        lines.append("")
        lines.append(f"Caveat: {CAVEAT}")
        return "\n".join(lines)

    lines.append("Snapshot reconstruction:")
    lines.append(f"  total snapshots seen: {stats['total_snapshots_seen']}")
    lines.append(f"  snapshots with a {room!r} entry: {stats['snapshots_with_room_entry']}")
    lines.append(f"  fully reconstructable (complete window on record): {stats['snapshots_fully_reconstructable']}")
    lines.append(
        f"  skipped, incomplete window (never compared, would be a false divergence): "
        f"{stats['snapshots_skipped_incomplete_window']}"
    )
    lines.append("")

    audit = stats["nick_diversity_audit"]
    lines.append("1. nick_diversity: published vs. recomputed from re-verified raw messages")
    if audit["compared_count"] == 0:
        lines.append("   no fully reconstructable snapshot -- nothing to compare.")
    else:
        lines.append(
            f"   compared on {audit['compared_count']} fully reconstructable snapshot(s): "
            f"{audit['exact_match_count']} matched within {EPSILON} (the platform's own number "
            f"holding up, not an accusation)."
        )
        lines.append(
            f"   mean absolute divergence: {audit['mean_absolute_divergence']:.6f}, "
            f"max absolute divergence: {audit['max_absolute_divergence']:.6f}"
        )
        lines.append("   divergence histogram:")
        for bucket in DIVERGENCE_BUCKET_KEYS:
            lines.append(f"     {bucket}: {audit['divergence_histogram'][bucket]}")
    lines.append("")

    zra = stats["zero_response_share_audit"]
    lines.append("2. zero_response_share: structurally unauditable")
    lines.append(f"   {zra['reason']}")
    if zra["snapshot_count"] > 0:
        lines.append(
            f"   published values across {zra['snapshot_count']} snapshot(s), for context only, "
            f"never recomputed: min={zra['published_min']:.4f}, mean={zra['published_mean']:.4f}, "
            f"max={zra['published_max']:.4f}"
        )
    else:
        lines.append("   no published zero_response_share values were found to report for context.")
    lines.append("")

    lines.append(
        "This is a FLOOR: the comparison above rests on re-verified signatures only, is "
        "limited to snapshots where the exact window could be reconstructed, and is measured "
        "only at the coverage ratio stated above."
    )
    lines.append(f"Caveat: {CAVEAT}")

    if (
        stats["malformed_lines_skipped"]
        or stats["seq_unusable_skipped"]
        or stats["snapshot_malformed_entries_skipped"]
    ):
        lines.append("")
        if stats["malformed_lines_skipped"]:
            lines.append(
                f"Note: {stats['malformed_lines_skipped']} unparseable line(s) in "
                "messages.jsonl were skipped."
            )
        if stats["seq_unusable_skipped"]:
            lines.append(
                f"Note: {stats['seq_unusable_skipped']} re-verified post(s) had a "
                "missing or non-integer seq and were skipped."
            )
        if stats["snapshot_malformed_entries_skipped"]:
            lines.append(
                f"Note: {stats['snapshot_malformed_entries_skipped']} unparseable or "
                "incomplete record(s) in rooms_snapshots.jsonl were skipped."
            )

    return "\n".join(lines)


def default_out_path(data_dir, room):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return os.path.join(data_dir, "analysis", f"selfaudit_{room}_{ts}.json")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Self-audit: recompute the platform's own published nick_diversity from "
        "re-verified raw messages and compare (read-only)."
    )
    parser.add_argument("--data-dir", required=True, help="collector data directory to read")
    parser.add_argument("--room", default="lobby", help="room to analyze (default: lobby)")
    parser.add_argument(
        "--out",
        default=None,
        help="path to write the JSON report to "
        "(default: <data-dir>/analysis/selfaudit_<room>_<ts>.json)",
    )
    args = parser.parse_args(argv)

    stats = compute_selfaudit_stats(args.data_dir, room=args.room)
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
