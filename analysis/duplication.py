"""Cross-key duplication rate: the first measurement in the Basanos
measurement layer.

Read-only by construction: this module only reads a room's already-stored
`<data-dir>/rooms/<room>/messages.jsonl` and `<data-dir>/coverage_state.json`
(via `collector.coverage.CoverageTracker`, whose `counters()` method is
itself read-only). It never writes to, or modifies, anything under
`<data-dir>` except the analysis output this module produces itself.

METRIC (v1, exact match only): among SIGNED messages that re-verify their
own signature (via `collector.verify.verify_record`), the share whose
exact stored text (byte-identical) is also signed by at least one OTHER
distinct did:key in the window. This is a FLOOR on the true cross-key
duplication rate, not an estimate of it -- see `format_report`'s docstring
for the two reasons why.

Deliberately out of scope for v1 (later tiers): normalized-text matching,
near-duplicate/template detection, time-clustering, and any per-identity
output. Exact text match only, and never a report of what any single
identity did.

Usage:
    python -m analysis.duplication --data-dir <dir> [--room lobby] [--out <path>]
"""

import argparse
import json
import os
import re
from datetime import datetime, timezone

from collector.coverage import CoverageTracker
from collector.verify import MalformedRecord, UnsupportedKeyType, is_signed, verify_record

TOP_N = 20

CAVEAT = (
    "some rooms may intend heartbeat-style posting, so this is a statement "
    "about the shape of the traffic, not a verdict about any poster."
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


def compute_duplication_stats(data_dir, room="lobby"):
    """Stream `<data_dir>/rooms/<room>/messages.jsonl` and compute the
    cross-key duplication aggregates.

    Returns a dict with the raw counters and aggregates needed by both the
    human-readable report and the JSON output. Reads only; writes nothing.
    """
    _validate_room(room)
    messages_path = os.path.join(data_dir, "rooms", room, "messages.jsonl")

    checked = 0
    verified = 0
    failed = 0
    malformed_lines = 0
    distinct_dids = set()
    # text -> set of distinct signing DIDs that produced it (re-verified only)
    text_to_dids = {}
    # text -> count of re-verified messages carrying it (re-verified only)
    text_to_count = {}

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
            distinct_dids.add(did)
            text_to_count[text] = text_to_count.get(text, 0) + 1
            dids_for_text = text_to_dids.setdefault(text, set())
            dids_for_text.add(did)

    cross_key_texts = {t for t, dids in text_to_dids.items() if len(dids) >= 2}
    numerator = sum(text_to_count[t] for t in cross_key_texts)
    denominator = verified
    rate = (numerator / denominator) if denominator else None

    top_duplicated = sorted(
        ({"text": t, "distinct_keys": len(dids)} for t, dids in text_to_dids.items() if len(dids) >= 2),
        key=lambda e: (-e["distinct_keys"], e["text"]),
    )[:TOP_N]

    coverage = CoverageTracker(data_dir).counters(room)
    coverage_ratio = CoverageTracker.coverage_ratio(
        coverage.get("captured_total", 0), coverage.get("dropped_total", 0)
    )

    return {
        "room": room,
        "messages_file_found": found,
        "signed_checked": checked,
        "signed_reverified": verified,
        "signed_reverify_failed": failed,
        "malformed_lines_skipped": malformed_lines,
        "distinct_dids": len(distinct_dids),
        "distinct_texts": len(text_to_dids),
        "cross_key_duplicated_numerator": numerator,
        "cross_key_duplicated_denominator": denominator,
        "cross_key_duplication_rate": rate,
        "top_duplicated_texts": top_duplicated,
        "coverage_captured_total": coverage.get("captured_total", 0),
        "coverage_dropped_total": coverage.get("dropped_total", 0),
        "coverage_ratio": coverage_ratio,
    }


def format_report(stats):
    """Render the human-readable report for `stats` (as returned by
    `compute_duplication_stats`).

    The headline rate is stated as a FLOOR, never a point estimate, for
    two independent reasons: (a) it is measured only at the coverage ratio
    captured below -- gaps mean some traffic was never seen at all; and
    (b) evicted stretches are the burstiest ones (that's *why* the ring
    evicted them), and duplicates concentrate in bursts, so the messages
    this collector never saw are, if anything, more duplicate-heavy than
    the ones it did -- meaning the true rate is >= this one, not merely
    "unknown in either direction".
    """
    room = stats["room"]
    lines = []
    lines.append(f"Cross-key duplication -- room: {room}")
    lines.append("=" * (26 + len(room)))
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
        "(The rate below rests on re-verified signatures only, not trusted stored ones.)"
    )
    lines.append("")

    denom = stats["cross_key_duplicated_denominator"]
    if denom == 0:
        lines.append("No re-verified signed messages in this window -- no rate to report.")
        return "\n".join(lines)

    rate_pct = 100.0 * stats["cross_key_duplication_rate"]
    lines.append(
        f"At least {rate_pct:.1f}% of re-verified signed {room} messages are "
        f"cross-key duplicates ({stats['cross_key_duplicated_numerator']} of {denom})."
    )
    lines.append("This is a FLOOR, not a point estimate, because:")
    lines.append(f"  (a) it is measured only at the coverage ratio stated below, and")
    lines.append(
        "  (b) evicted stretches are the burstiest, where duplicates concentrate, "
        "so the true rate is >= this."
    )
    lines.append("")
    lines.append(f"Caveat: {CAVEAT}")
    lines.append("")

    lines.append("Supporting aggregates (aggregate only -- no individual DID is named):")
    lines.append(f"  distinct signing DIDs:        {stats['distinct_dids']}")
    lines.append(f"  distinct exact texts:         {stats['distinct_texts']}")

    coverage_ratio = stats["coverage_ratio"]
    ratio_str = f"{coverage_ratio:.4f}" if coverage_ratio is not None else "n/a"
    lines.append("")
    lines.append("Coverage:")
    lines.append(f"  captured_total: {stats['coverage_captured_total']}")
    lines.append(f"  dropped_total:  {stats['coverage_dropped_total']}")
    lines.append(f"  coverage ratio: {ratio_str}")

    lines.append("")
    lines.append(f"Top {TOP_N} most cross-key-duplicated texts (by distinct signing key count):")
    if not stats["top_duplicated_texts"]:
        lines.append("  (none -- no text was signed by more than one distinct key)")
    else:
        for entry in stats["top_duplicated_texts"]:
            lines.append(f"  [{entry['distinct_keys']} distinct keys] {entry['text']!r}")

    if stats["malformed_lines_skipped"]:
        lines.append("")
        lines.append(
            f"Note: {stats['malformed_lines_skipped']} unparseable line(s) in "
            "messages.jsonl were skipped."
        )

    return "\n".join(lines)


def default_out_path(data_dir, room):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return os.path.join(data_dir, "analysis", f"duplication_{room}_{ts}.json")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Cross-key duplication rate over a room's already-collected messages "
        "(read-only)."
    )
    parser.add_argument("--data-dir", required=True, help="collector data directory to read")
    parser.add_argument("--room", default="lobby", help="room to analyze (default: lobby)")
    parser.add_argument(
        "--out",
        default=None,
        help="path to write the JSON report to "
        "(default: <data-dir>/analysis/duplication_<room>_<ts>.json)",
    )
    args = parser.parse_args(argv)

    stats = compute_duplication_stats(args.data_dir, room=args.room)
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
