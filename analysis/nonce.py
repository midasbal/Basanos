"""Nonce fingerprint: the fifth measurement in the Basanos measurement layer.

Read-only by construction: this module only reads a room's already-stored
`<data-dir>/rooms/<room>/messages.jsonl` and `<data-dir>/coverage_state.json`
(via `collector.coverage.CoverageTracker`, whose `counters()` method is
itself read-only). It never writes to, or modifies, anything under
`<data-dir>` except the analysis output this module produces itself.

FINGERPRINT DEFINITION, strict and data-defined: the fingerprint of a
nonce is the DIGIT LENGTH of its stored string form. Nothing inferred, no
truncation-guessing about what a client library actually did -- length is
a byte-exact property anyone re-reading the same file can recompute
identically. Real nonces observed against the live service cluster into a
few dominant lengths (13 digits: millisecond-epoch; 16: microsecond-epoch;
19: nanosecond-epoch), each consistent with a specific, common
timestamp-precision choice a signing toolkit makes -- so a cluster of keys
that all produce the same nonce length is consistent with those keys
running the same or a related toolkit, independent of anything about the
message text itself. Every length that actually appears is counted (not
just 13/16/19); anything outside those three is grouped as "other", with
its own length-by-length breakdown reported separately since the tail is
itself a small finding, not noise to discard. A nonce that is missing or
not purely numeric is tallied as unusable and excluded from every band
statistic, never a crash.

A "shared template" is an exact stored text (byte-identical) signed by at
least two distinct did:keys -- the same population `analysis/duplication.py`
calls "cross-key duplicated" and `analysis/coordination.py` ranks by
distinct-key count for its core-bloc analysis. This module does not import
from or modify either of those, or `analysis/synchrony.py` (they each
intentionally duplicate the small streaming/re-verify walk rather than
share a helper, to keep each module a single self-contained read); it asks
a third, TEXT-INDEPENDENT question about the same top-N templates: do the
keys behind a heavily-duplicated template also share a nonce-generation
style, corroborating shared origin through a completely different signal
than the byte-identical text itself.

IS THIS CONFOUNDED THE WAY THE TIMING MEASUREMENT WAS? No, and for a
different reason than it might first appear. `analysis/synchrony.py` had
to build a room-weighted null model because it was comparing an OBSERVED
distribution (a template's post timing) against a RANDOM one (what
uniform-at-random posting would produce), and the room's own activity is
not uniform, so the naive random null was itself confounded by room
rhythm. Here, both sides of every comparison in this module are already
OBSERVED, measured distributions -- a template's own nonce-length mix
against the room's own nonce-length mix (or against the room's mix with
that template's own contribution removed) -- there is no random null
model anywhere in this module, so there is no room-rhythm-shaped confound
to control for. The one honest caveat that DOES apply: a template's own
keys are a subset of the room, so a template's messages contribute to the
very room baseline it gets compared against (self-inclusion) -- a large
enough template could partly "match itself." That is handled the same way
`analysis/synchrony.py` handles its own, different, self-inclusion
concern: report divergence against the whole room (intuitive) AND against
the room with that template's own nonces subtracted out first
(room-minus-self, the rigorous check) -- if a template is the entire room,
room-minus-self is degenerate and reported as None rather than a
misleading zero.

Deliberately out of scope for v1: any claim about which specific toolkit
or library a nonce length implies, any timing analysis, and any
per-identity output. Digit length only, and never a report of what any
single identity did -- every number below is a count or a distance over
bands/templates, never a name.

Usage:
    python -m analysis.nonce --data-dir <dir> [--room lobby] [--top-n 20] [--out <path>]
"""

import argparse
import json
import os
import re
import statistics
from datetime import datetime, timezone

from collector.coverage import CoverageTracker
from collector.verify import MalformedRecord, UnsupportedKeyType, is_signed, verify_record

DEFAULT_TOP_N = 20
DIVERGENCE_THRESHOLDS = (0.3, 0.5, 0.7)

# Length -> band key, for the three named, confirmed-common bands. Every
# other length falls into "other" (see _band_counts_from_lengths).
BAND_NAMES = {13: "13", 16: "16", 19: "19"}
BAND_KEYS = ("13", "16", "19", "other")
BAND_LABELS = {"13": "ms-epoch", "16": "us-epoch", "19": "ns-epoch", "other": "other"}

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


def _nonce_digit_length(nonce):
    """The digit length of a stored nonce, or None if it is unusable
    (missing, or not purely numeric digits). A byte-exact property: no
    parsing beyond `str.isdigit()`, no guessing at what precision a
    client meant, just the length of the string as stored.
    """
    if not isinstance(nonce, str) or not nonce:
        return None
    if not nonce.isdigit():
        return None
    return len(nonce)


def _band_counts_from_lengths(length_counts):
    """Fold a length -> count map into the four report bands (13/16/19/
    other), summing every length that isn't one of the three named ones
    into "other".
    """
    bands = {key: 0 for key in BAND_KEYS}
    for length, count in length_counts.items():
        bands[BAND_NAMES.get(length, "other")] += count
    return bands


def _band_fractions(band_counts):
    """Convert band counts to fractions of their own total. None when the
    total is 0 -- no usable nonces at all for this population, so there is
    no distribution to report or compare (the caller treats this the same
    way a degenerate room-minus-self is treated: no divergence to compute
    against nothing).
    """
    total = sum(band_counts.values())
    if total == 0:
        return None
    return {band: count / total for band, count in band_counts.items()}


def _total_variation_distance(dist_a, dist_b):
    """Total variation distance between two band-fraction distributions:
    0.5 * sum of absolute per-band fraction differences, bounded in [0, 1].

    This compares two OBSERVED distributions (a template's own band mix
    against the room's, or against room-minus-self), never an observed
    distribution against a random null model -- unlike
    `analysis/synchrony.py`'s dispersion ratio, there is no room-rhythm (or
    any other) confound to simulate away here, because both sides of the
    comparison are already real, measured band mixes over the same four
    bands. TVD is a simple, symmetric distance with a direct reading (0
    means an identical band mix, 1 means completely disjoint bands), and
    needs no seeded simulation or null model to define, unlike the
    null-model ratios elsewhere in this project.
    """
    bands = set(dist_a) | set(dist_b)
    return 0.5 * sum(abs(dist_a.get(b, 0.0) - dist_b.get(b, 0.0)) for b in bands)


def compute_nonce_stats(data_dir, room="lobby", top_n=DEFAULT_TOP_N):
    """Stream `<data_dir>/rooms/<room>/messages.jsonl` and compute the
    room-wide nonce-length band distribution plus, for the top-N shared
    templates, each one's own band composition and how it diverges from
    the room's.

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
    unusable_nonces = 0
    # text -> set of distinct signing DIDs that produced it (re-verified only)
    text_to_dids = {}
    # nonce digit length -> count, over every re-verified signed message
    # with a usable nonce, room-wide
    room_length_counts = {}
    # text -> {nonce digit length -> count}, re-verified and usable only
    text_to_length_counts = {}

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
                # the text-keyed aggregate below with "unhashable type".
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

            length = _nonce_digit_length(record.get("nonce"))
            if length is None:
                unusable_nonces += 1
            else:
                room_length_counts[length] = room_length_counts.get(length, 0) + 1
                per_text = text_to_length_counts.setdefault(text, {})
                per_text[length] = per_text.get(length, 0) + 1

    # A "shared template": exact text signed by >= 2 distinct DIDs, ranked by
    # distinct-key count -- the same population and ordering coordination.py
    # uses for its own top-N.
    shared_templates = {t for t, dids in text_to_dids.items() if len(dids) >= 2}
    ranked_templates = sorted(shared_templates, key=lambda t: (-len(text_to_dids[t]), t))
    top_n_templates = ranked_templates[:top_n]

    room_band_counts = _band_counts_from_lengths(room_length_counts)
    room_band_fractions = _band_fractions(room_band_counts)
    other_length_breakdown = {
        str(length): count
        for length, count in sorted(room_length_counts.items())
        if length not in BAND_NAMES
    }

    template_reports = []
    minus_self_divergences = []
    for t in top_n_templates:
        own_length_counts = text_to_length_counts.get(t, {})
        own_band_counts = _band_counts_from_lengths(own_length_counts)
        own_band_fractions = _band_fractions(own_band_counts)

        # room-minus-self: the room's band counts with this template's own
        # contribution removed first, so a template is never compared
        # against a baseline partly built from itself (the self-inclusion
        # caveat described in the module docstring). Clamped at 0 per
        # length defensively; it should never go negative, since
        # room_length_counts already includes this template's own nonces.
        minus_self_length_counts = {
            length: max(0, room_length_counts.get(length, 0) - own_length_counts.get(length, 0))
            for length in set(room_length_counts) | set(own_length_counts)
        }
        minus_self_band_counts = _band_counts_from_lengths(minus_self_length_counts)
        minus_self_band_fractions = _band_fractions(minus_self_band_counts)

        if own_band_fractions is not None and room_band_fractions is not None:
            divergence_vs_room = _total_variation_distance(own_band_fractions, room_band_fractions)
        else:
            divergence_vs_room = None

        if own_band_fractions is not None and minus_self_band_fractions is not None:
            divergence_vs_room_minus_self = _total_variation_distance(
                own_band_fractions, minus_self_band_fractions
            )
        else:
            # Degenerate exactly when this template accounts for every
            # usable nonce in the room (or there are none at all): there is
            # no "rest of the room" left to compare against.
            divergence_vs_room_minus_self = None

        template_reports.append(
            {
                "text": t,
                "distinct_keys": len(text_to_dids[t]),
                "usable_nonce_count": sum(own_band_counts.values()),
                "band_counts": own_band_counts,
                "band_fractions": own_band_fractions,
                "divergence_vs_room": divergence_vs_room,
                "divergence_vs_room_minus_self": divergence_vs_room_minus_self,
            }
        )
        if divergence_vs_room_minus_self is not None:
            minus_self_divergences.append(divergence_vs_room_minus_self)

    median_divergence = statistics.median(minus_self_divergences) if minus_self_divergences else None
    max_divergence = max(minus_self_divergences) if minus_self_divergences else None
    divergence_threshold_counts = {
        str(threshold): sum(1 for d in minus_self_divergences if d > threshold)
        for threshold in DIVERGENCE_THRESHOLDS
    }

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
        "unusable_nonces": unusable_nonces,
        "distinct_shared_templates": len(shared_templates),
        "room_band_counts": room_band_counts,
        "room_band_fractions": room_band_fractions,
        "room_other_length_breakdown": other_length_breakdown,
        "templates": template_reports,
        "median_divergence_vs_room_minus_self": median_divergence,
        "max_divergence_vs_room_minus_self": max_divergence,
        "divergence_threshold_counts": divergence_threshold_counts,
        "coverage_captured_total": coverage.get("captured_total", 0),
        "coverage_dropped_total": coverage.get("dropped_total", 0),
        "coverage_ratio": coverage_ratio,
    }


def format_report(stats):
    """Render the human-readable report for `stats` (as returned by
    `compute_nonce_stats`).

    The band distribution is a FLOOR, not a verdict: it rests on
    re-verified signatures only, and is measured only at the coverage
    ratio captured below. A high divergence-vs-room-minus-self for a
    template is corroborating evidence of shared origin, via a signal
    completely independent of the byte-identical text itself (nonce
    generation style, not message content) -- strong evidence, not a
    definitive census, and no individual DID is ever named.
    """
    room = stats["room"]
    top_n = stats["top_n"]
    lines = []
    lines.append(f"Nonce fingerprint -- room: {room}")
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

    lines.append(f"Unusable nonces (missing or non-numeric, excluded from bands): {stats['unusable_nonces']}")
    lines.append("")

    lines.append("1. Room-wide nonce-length band distribution:")
    room_fractions = stats["room_band_fractions"]
    if room_fractions is None:
        lines.append("   No usable nonces in this window -- no distribution to report.")
    else:
        for band in BAND_KEYS:
            count = stats["room_band_counts"][band]
            frac = room_fractions[band]
            lines.append(f"   {band} ({BAND_LABELS[band]}): {100.0 * frac:.1f}% ({count})")
        if stats["room_other_length_breakdown"]:
            lines.append("   other-length breakdown:")
            for length_str, count in stats["room_other_length_breakdown"].items():
                lines.append(f"     {length_str} digits: {count}")
    lines.append("")

    lines.append(f"2. Per-template band composition vs the room (top-{top_n} shared templates):")
    if not stats["templates"]:
        lines.append("   (none -- no shared template had a usable nonce)")
    else:
        for entry in stats["templates"]:
            lines.append(f"   [{entry['distinct_keys']} distinct keys] {entry['text']!r}")
            if entry["band_fractions"] is None:
                lines.append("     no usable nonces for this template")
            else:
                band_str = ", ".join(
                    f"{band}={100.0 * entry['band_fractions'][band]:.0f}%" for band in BAND_KEYS
                )
                lines.append(f"     bands: {band_str} ({entry['usable_nonce_count']} usable)")
            vs_room = entry["divergence_vs_room"]
            vs_minus_self = entry["divergence_vs_room_minus_self"]
            vs_room_str = f"{vs_room:.3f}" if vs_room is not None else "n/a"
            vs_minus_self_str = f"{vs_minus_self:.3f}" if vs_minus_self is not None else "n/a"
            lines.append(
                f"     divergence vs whole room: {vs_room_str}, "
                f"vs room-minus-self (HEADLINE): {vs_minus_self_str}"
            )
    lines.append("")

    lines.append("Aggregate across the top-N templates:")
    median_divergence = stats["median_divergence_vs_room_minus_self"]
    max_divergence = stats["max_divergence_vs_room_minus_self"]
    if median_divergence is None:
        lines.append("  no templates measured -- no aggregate to report.")
    else:
        lines.append(f"  median divergence vs room-minus-self: {median_divergence:.3f}")
        lines.append(f"  max divergence vs room-minus-self:    {max_divergence:.3f}")
        for threshold in DIVERGENCE_THRESHOLDS:
            count = stats["divergence_threshold_counts"][str(threshold)]
            lines.append(
                f"  {count} of {len(stats['templates'])} top templates have a room-minus-self "
                f"divergence above {threshold:g}"
            )
    lines.append("")

    lines.append(
        "This is a FLOOR: it rests on re-verified signatures only and is measured only at "
        "the coverage ratio stated above."
    )
    lines.append(
        "Divergence vs room-minus-self is the headline: a high value means a template's keys "
        "share a nonce-generation style far more than the rest of the room does, corroborating "
        "shared origin through a signal independent of the shared text itself. Divergence vs "
        "the whole room is the intuitive version, kept alongside it; the two usually agree, and "
        "when they do not it is because a large template pulls the whole-room baseline toward "
        "its own mix (self-inclusion), which room-minus-self removes."
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
    return os.path.join(data_dir, "analysis", f"nonce_{room}_{ts}.json")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Nonce-length fingerprint over a room's already-collected messages "
        "(read-only)."
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
        "--out",
        default=None,
        help="path to write the JSON report to "
        "(default: <data-dir>/analysis/nonce_<room>_<ts>.json)",
    )
    args = parser.parse_args(argv)

    stats = compute_nonce_stats(args.data_dir, room=args.room, top_n=args.top_n)
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
