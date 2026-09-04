"""Coordination concentration: the second measurement in the Basanos
measurement layer.

Read-only by construction: this module only reads a room's already-stored
`<data-dir>/rooms/<room>/messages.jsonl` and `<data-dir>/coverage_state.json`
(via `collector.coverage.CoverageTracker`, whose `counters()` method is
itself read-only). It never writes to, or modifies, anything under
`<data-dir>` except the analysis output this module produces itself.

A "shared template" is an exact stored text (byte-identical) signed by at
least two distinct did:keys -- the same population `analysis/duplication.py`
calls "cross-key duplicated". This module does not import from or modify
that one (they intentionally duplicate the small streaming/re-verify walk
rather than share a helper, to keep each module a single self-contained
read); it goes one step further: instead of just measuring the share of
traffic that is duplicated, it asks *how concentrated* that duplication is
among a small set of keys -- the "core bloc" question.

Deliberately out of scope for v1 (later tiers): a full key-linkage graph
or connected-components analysis, near-duplicate/template-variant merging,
timing analysis, and any per-identity output. Exact text match only, and
never a report of what any single identity did -- every number below is a
count or a fraction over keys/templates, never a name.

Usage:
    python -m analysis.coordination --data-dir <dir> [--room lobby] [--top-n 20] [--out <path>]
"""

import argparse
import json
import os
import re
from datetime import datetime, timezone
from itertools import combinations

from collector.coverage import CoverageTracker
from collector.verify import MalformedRecord, UnsupportedKeyType, is_signed, verify_record

DEFAULT_TOP_N = 20
MEMBERSHIP_THRESHOLDS = (1, 2, 3, 5, 10, 15, 20)
JACCARD_TOP_N = 5

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


def _jaccard(set_a, set_b):
    union = set_a | set_b
    if not union:
        return 0.0
    return len(set_a & set_b) / len(union)


def compute_coordination_stats(data_dir, room="lobby", top_n=DEFAULT_TOP_N):
    """Stream `<data_dir>/rooms/<room>/messages.jsonl` and compute the
    coordination-concentration aggregates.

    Returns a dict with the raw counters and aggregates needed by both the
    human-readable report and the JSON output. Reads only; writes nothing.
    No did:key string appears anywhere in the returned structure -- keys
    are only ever counted, never named.
    """
    _validate_room(room)
    messages_path = os.path.join(data_dir, "rooms", room, "messages.jsonl")

    checked = 0
    verified = 0
    failed = 0
    malformed_lines = 0
    # text -> set of distinct signing DIDs that produced it (re-verified only)
    text_to_dids = {}
    # text -> count of re-verified messages carrying it (re-verified only)
    text_to_count = {}
    # did -> set of distinct texts that DID signed (re-verified only)
    did_to_texts = {}

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
            text_to_count[text] = text_to_count.get(text, 0) + 1
            text_to_dids.setdefault(text, set()).add(did)
            did_to_texts.setdefault(did, set()).add(text)

    distinct_dids = set(did_to_texts.keys())

    # A "shared template": exact text signed by >= 2 distinct DIDs.
    shared_templates = {t for t, dids in text_to_dids.items() if len(dids) >= 2}
    total_shared_messages = sum(text_to_count[t] for t in shared_templates)

    coordinated_share_messages = (total_shared_messages / verified) if verified else None

    # Among distinct signing DIDs, fraction that signed >=1/>=2/>=3 DISTINCT
    # shared templates (any shared template, not limited to the top-N).
    n_dids = len(distinct_dids)
    shared_template_counts_per_did = {
        did: len(texts & shared_templates) for did, texts in did_to_texts.items()
    }
    coordinated_share_dids = {}
    for threshold in (1, 2, 3):
        qualifying = sum(1 for c in shared_template_counts_per_did.values() if c >= threshold)
        coordinated_share_dids[f">={threshold}"] = {
            "count": qualifying,
            "fraction": (qualifying / n_dids) if n_dids else None,
        }

    # Overall ranking of shared templates by distinct-key count (ties broken
    # by text, for a deterministic order).
    ranked_templates = sorted(
        shared_templates, key=lambda t: (-len(text_to_dids[t]), t)
    )

    top_n_templates = ranked_templates[:top_n]
    top_n_message_total = sum(text_to_count[t] for t in top_n_templates)
    concentration = (
        (top_n_message_total / total_shared_messages) if total_shared_messages else None
    )

    # Core bloc: for each DID, how many of the top-N templates it signed.
    membership_counter = {}
    for t in top_n_templates:
        for did in text_to_dids[t]:
            membership_counter[did] = membership_counter.get(did, 0) + 1

    membership_curve = {}
    for m in MEMBERSHIP_THRESHOLDS:
        if m > top_n:
            continue  # capped at N -- a membership >N of N templates is impossible
        count = sum(1 for c in membership_counter.values() if c >= m)
        membership_curve[str(m)] = count

    if top_n_templates:
        intersection_all_top_n = set.intersection(*(text_to_dids[t] for t in top_n_templates))
    else:
        intersection_all_top_n = set()

    # Pairwise Jaccard overlap between the top 5 templates' key-sets --
    # fixed at 5 regardless of --top-n, a small, fully-printable cross-check.
    jaccard_pool = ranked_templates[:JACCARD_TOP_N]
    pairwise_jaccard = []
    for text_a, text_b in combinations(jaccard_pool, 2):
        pairwise_jaccard.append(
            {
                "text_a": text_a,
                "text_b": text_b,
                "jaccard": _jaccard(text_to_dids[text_a], text_to_dids[text_b]),
            }
        )

    top_n_report = [
        {"text": t, "distinct_keys": len(text_to_dids[t])} for t in top_n_templates
    ]

    coverage = CoverageTracker(data_dir).counters(room)
    coverage_ratio = CoverageTracker.coverage_ratio(
        coverage.get("captured_total", 0), coverage.get("dropped_total", 0)
    )

    return {
        "room": room,
        "top_n": top_n,
        "messages_file_found": found,
        "signed_checked": checked,
        "signed_reverified": verified,
        "signed_reverify_failed": failed,
        "malformed_lines_skipped": malformed_lines,
        "distinct_dids": n_dids,
        "distinct_shared_templates": len(shared_templates),
        "coordinated_share_messages": coordinated_share_messages,
        "coordinated_share_messages_numerator": total_shared_messages,
        "coordinated_share_messages_denominator": verified,
        "coordinated_share_dids": coordinated_share_dids,
        "concentration_top_n_fraction": concentration,
        "concentration_top_n_numerator": top_n_message_total,
        "concentration_top_n_denominator": total_shared_messages,
        "top_n_templates": top_n_report,
        "membership_curve": membership_curve,
        "intersection_all_top_n_size": len(intersection_all_top_n),
        "pairwise_jaccard_top5": pairwise_jaccard,
        "coverage_captured_total": coverage.get("captured_total", 0),
        "coverage_dropped_total": coverage.get("dropped_total", 0),
        "coverage_ratio": coverage_ratio,
    }


def format_report(stats):
    """Render the human-readable report for `stats` (as returned by
    `compute_coordination_stats`).

    The core-bloc finding is stated as both a FLOOR and as LINKAGE, not a
    verdict: it is measured only at the coverage ratio captured below and
    on exact-text matches only (a floor, in the same sense
    `analysis/duplication.py`'s rate is), and byte-identical text shared
    across distinct keys is strong evidence of shared origin -- coordinated
    posting, a shared template, or a common operator -- but is not by
    itself a definitive census of who operates what.
    """
    room = stats["room"]
    top_n = stats["top_n"]
    lines = []
    lines.append(f"Coordination concentration -- room: {room}")
    lines.append("=" * (28 + len(room)))
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

    verified = stats["signed_reverified"]
    if verified == 0:
        lines.append("No re-verified signed messages in this window -- nothing to report.")
        return "\n".join(lines)

    coverage_ratio = stats["coverage_ratio"]
    ratio_str = f"{coverage_ratio:.4f}" if coverage_ratio is not None else "n/a"
    lines.append("Coverage:")
    lines.append(f"  captured_total: {stats['coverage_captured_total']}")
    lines.append(f"  dropped_total:  {stats['coverage_dropped_total']}")
    lines.append(f"  coverage ratio: {ratio_str}")
    lines.append("")

    lines.append("1. Coordinated share")
    share = stats["coordinated_share_messages"]
    if share is None:
        lines.append("   No re-verified signed messages -- no share to report.")
    else:
        lines.append(
            f"   {100.0 * share:.1f}% of re-verified signed {room} messages carry a "
            f"shared template ({stats['coordinated_share_messages_numerator']} of "
            f"{stats['coordinated_share_messages_denominator']}) -- this should match "
            f"analysis/duplication.py's cross-key duplication rate for the same window "
            f"(cross-check)."
        )
    for threshold in (">=1", ">=2", ">=3"):
        entry = stats["coordinated_share_dids"][threshold]
        frac = entry["fraction"]
        frac_str = f"{100.0 * frac:.1f}%" if frac is not None else "n/a"
        lines.append(
            f"   {frac_str} of distinct signing DIDs ({entry['count']} of "
            f"{stats['distinct_dids']}) signed {threshold} distinct shared templates."
        )
    lines.append("")

    lines.append(f"2. Concentration (top-{top_n} templates by distinct-key count)")
    conc = stats["concentration_top_n_fraction"]
    if conc is None:
        lines.append("   No shared templates in this window -- no concentration to report.")
    else:
        lines.append(
            f"   The top-{top_n} templates account for {100.0 * conc:.1f}% of all "
            f"cross-key-duplicated messages ({stats['concentration_top_n_numerator']} of "
            f"{stats['concentration_top_n_denominator']})."
        )
    lines.append("")

    lines.append(f"3. Core bloc (membership across the top-{top_n} templates)")
    if not stats["membership_curve"]:
        lines.append("   No shared templates in this window -- no bloc to report.")
    else:
        for m_str, count in stats["membership_curve"].items():
            lines.append(
                f"   at least {count} keys each sign >={m_str} of the top-{top_n} templates"
            )
        lines.append(
            f"   Intersection of all top-{top_n} templates' key-sets (signed EVERY one "
            f"of them): {stats['intersection_all_top_n_size']} distinct keys."
        )
        lines.append(
            "   Each such threshold is a coordinated bloc; this measures behavioral "
            "linkage (byte-identical text shared across keys), strong evidence of "
            "shared origin but not a definitive operator census."
        )
    lines.append(
        "   This is a FLOOR: it rests on exact-text matches only (no near-duplicate "
        "matching), and is measured only at the coverage ratio stated above."
    )
    lines.append("")

    lines.append(f"Top {top_n} shared templates (aggregate only -- no individual DID is named):")
    if not stats["top_n_templates"]:
        lines.append("  (none -- no text was signed by more than one distinct key)")
    else:
        for entry in stats["top_n_templates"]:
            lines.append(f"  [{entry['distinct_keys']} distinct keys] {entry['text']!r}")
    lines.append("")

    lines.append(f"Pairwise Jaccard overlap, top {JACCARD_TOP_N} templates' key-sets:")
    if not stats["pairwise_jaccard_top5"]:
        lines.append("  (fewer than 2 shared templates -- nothing to pair)")
    else:
        for entry in stats["pairwise_jaccard_top5"]:
            lines.append(
                f"  {entry['jaccard']:.3f}  {entry['text_a']!r} <-> {entry['text_b']!r}"
            )

    lines.append("")
    lines.append(f"Caveat: {CAVEAT}")

    if stats["malformed_lines_skipped"]:
        lines.append("")
        lines.append(
            f"Note: {stats['malformed_lines_skipped']} unparseable line(s) in "
            "messages.jsonl were skipped."
        )

    return "\n".join(lines)


def default_out_path(data_dir, room):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return os.path.join(data_dir, "analysis", f"coordination_{room}_{ts}.json")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Coordination concentration ('core bloc') over a room's already-collected "
        "messages (read-only)."
    )
    parser.add_argument("--data-dir", required=True, help="collector data directory to read")
    parser.add_argument("--room", default="lobby", help="room to analyze (default: lobby)")
    parser.add_argument(
        "--top-n",
        type=int,
        default=DEFAULT_TOP_N,
        help=f"number of top shared templates to use for the core-bloc analysis "
        f"(default: {DEFAULT_TOP_N})",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="path to write the JSON report to "
        "(default: <data-dir>/analysis/coordination_<room>_<ts>.json)",
    )
    args = parser.parse_args(argv)

    stats = compute_coordination_stats(args.data_dir, room=args.room, top_n=args.top_n)
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
