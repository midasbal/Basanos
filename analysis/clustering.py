"""Operator clustering: the eighth measurement in the Basanos measurement
layer, and the ceiling item -- the most it is willing to say about shared
origin, and no more.

Read-only by construction: this module only reads a room's already-stored
`<data-dir>/rooms/<room>/messages.jsonl` and `<data-dir>/coverage_state.json`
(via `collector.coverage.CoverageTracker`, whose `counters()` method is
itself read-only). It never writes to, or modifies, anything under
`<data-dir>` except the analysis output this module produces itself.

THE HARD INVARIANT, STATED FIRST BECAUSE IT OVERRIDES EVERYTHING ELSE IN
THIS FILE: the output is STRICTLY AGGREGATE, a cluster-SIZE DISTRIBUTION
only. No key-to-cluster membership, no cluster membership list, no
representative key, no count that would let a reader single out or even
confirm the existence of a specific key, may EVER appear in the returned
dict, the JSON output, or the human-readable report, on any code path,
including error paths. A reader of this module's output can learn how
many clusters of what sizes exist; they can learn nothing about which
keys are in any of them, or whether any particular key is in a cluster at
all. See `_cluster_sizes` below for exactly how this is enforced
structurally, not just by convention: the union-find parent map is a
local variable that goes out of scope before this function returns, and
the function's only return value is a plain list of integers (sizes),
which is the sole channel between the linkage computation and everything
downstream of it in this module.

WHAT IT MEASURES: two keys are LINKED if they both signed at least
`min_shared` of the same BOUNDED templates (exact byte-identical text,
same population `analysis/duplication.py` and `analysis/coordination.py`
use). A "bounded template" is a text signed by between 2 and `cap`
distinct keys -- a text signed by more than `cap` keys is too promiscuous
to be a meaningful linking signal (a room-wide catchphrase everyone uses
says nothing about shared origin) and is excluded from linkage entirely,
though it is still counted as a shared template by
`analysis/duplication.py` and `analysis/coordination.py` elsewhere.

WHY min_shared >= 2, NOT 1 -- THE ANTI-CHAINING FIX: linking any two keys
that share even a single bounded template produces one false giant
component through transitive chaining (confirmed on a real probe: a
21,400-key chain from single-shared-template linkage). Requiring at least
2 shared bounded templates before two keys are linked shatters that chain
back into real, dense clusters (confirmed: largest cluster shrinks to
roughly 1,243). This module reports the size distribution at BOTH
`min_shared` (the `--min-shared` flag, default 2) and `min_shared + 1`, in
the same run, specifically so a reader can see the cluster structure is
not an artifact of exactly which threshold was chosen.

HONEST SCOPE: this clusters ONLY the keys that participate in at least
one bounded shared template -- a minority of all distinct signing keys.
The majority of keys are singletons here because they share no bounded
template with anyone, which is consistent with `analysis/diversity.py`'s
finding that most keys are single-use, not a contradiction of it. A
cluster means "these keys each signed at least `min_shared` of the same
specific bounded lines" -- a shared-BEHAVIOR signal consistent with
shared origin (a common toolkit, script, or operator), never a proof of a
single operator, never a named or identifiable group, and never evidence
about any individual key.

Deliberately out of scope for v1: any attempt to name, count, or describe
what a cluster's shared templates actually say, any claim about who
operates a cluster, and any per-identity output of any kind. Every number
below is a count of clusters or keys, never a name, and never anything
that could be combined with other public information to identify one.

Usage:
    python -m analysis.clustering --data-dir <dir> [--room lobby] \\
        [--cap 200] [--min-shared 2] [--out <path>]
"""

import argparse
import json
import os
import re
from datetime import datetime, timezone
from itertools import combinations

from collector.coverage import CoverageTracker
from collector.verify import MalformedRecord, UnsupportedKeyType, is_signed, verify_record

DEFAULT_CAP = 200
DEFAULT_MIN_SHARED = 2

HISTOGRAM_BUCKETS = ("2", "3-5", "6-10", "11-50", "51-200", "201-1000", "1000+")

CAVEAT = (
    "some rooms may intend heartbeat-style posting, so this is a statement "
    "about the shape of the traffic, not a verdict about any poster."
)

SCOPE_CAVEAT = (
    "this clusters only the keys that participate in at least one bounded shared "
    "template, a minority of all distinct signing keys; the rest are singletons here "
    "because they share no bounded template with anyone, consistent with most keys "
    "being single-use, not a contradiction of it."
)

CLUSTER_MEANING_CAVEAT = (
    "a cluster means these keys each signed at least min_shared of the same specific "
    "bounded templates -- a shared-behavior signal consistent with shared origin, never "
    "a proof of a single operator and never a named or identifiable group."
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


class _UnionFind:
    """Disjoint-set over a fixed universe of items, path compression plus
    union by rank. A purely internal data structure: nothing outside
    `_cluster_sizes` ever sees an instance of this class or its parent
    map, which is exactly what keeps key-to-cluster membership from ever
    reaching this module's output -- see `_cluster_sizes`'s docstring.
    """

    def __init__(self, items):
        self.parent = {item: item for item in items}
        self.rank = {item: 0 for item in items}

    def find(self, x):
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


def _cluster_sizes(keys, pair_counts, threshold):
    """Union every pair of `keys` whose shared-bounded-template count in
    `pair_counts` is >= `threshold`, then return ONLY the resulting
    cluster sizes as a list of plain integers.

    THIS IS THE SINGLE POINT IN THE MODULE WHERE A KEY-TO-KEY LINKAGE
    DECISION IS MADE, and it is deliberately built so that decision cannot
    leak: `uf`, its parent map, and `root_to_size` (a dict whose KEYS are
    did:key strings, used only to group sizes) are all local variables
    that go out of scope the moment this function returns. The return
    value is `list(root_to_size.values())` -- plain integers, with no
    reference back to which key produced which size. Every caller in this
    module, and everything in the returned stats dict, is built from that
    list of integers alone.
    """
    uf = _UnionFind(keys)
    for (a, b), count in pair_counts.items():
        if count >= threshold:
            uf.union(a, b)
    root_to_size = {}
    for key in keys:
        root = uf.find(key)
        root_to_size[root] = root_to_size.get(root, 0) + 1
    return list(root_to_size.values())


def _bucket_for_size(size):
    if size <= 2:
        return "2"
    if size <= 5:
        return "3-5"
    if size <= 10:
        return "6-10"
    if size <= 50:
        return "11-50"
    if size <= 200:
        return "51-200"
    if size <= 1000:
        return "201-1000"
    return "1000+"


def _summarize_pass(sizes, min_shared):
    """Turn a plain list of cluster sizes (integers only, see
    `_cluster_sizes`) into the aggregate summary for one min_shared pass.
    """
    multi_key_sizes = [s for s in sizes if s >= 2]
    singleton_count = sum(1 for s in sizes if s == 1)
    histogram = {bucket: 0 for bucket in HISTOGRAM_BUCKETS}
    for size in multi_key_sizes:
        histogram[_bucket_for_size(size)] += 1
    return {
        "min_shared": min_shared,
        "cluster_count": len(sizes),
        "multi_key_cluster_count": len(multi_key_sizes),
        "singleton_count": singleton_count,
        "largest_cluster_size": max(sizes) if sizes else 0,
        "size_histogram": histogram,
    }


def compute_clustering_stats(data_dir, room="lobby", cap=DEFAULT_CAP, min_shared=DEFAULT_MIN_SHARED):
    """Stream `<data_dir>/rooms/<room>/messages.jsonl` and compute the
    cluster-size distribution of keys linked by shared bounded templates,
    at both `min_shared` and `min_shared + 1`.

    Returns a dict with the raw counters and aggregates needed by both the
    human-readable report and the JSON output. Reads only; writes nothing.
    No did:key string, and no key-to-cluster membership of any kind,
    appears anywhere in the returned structure -- see the module
    docstring's hard invariant and `_cluster_sizes`'s docstring for how
    that is enforced structurally, not just by convention.
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
            distinct_dids.add(did)
            text_to_dids.setdefault(text, set()).add(did)

    # Bounded templates: signed by between 2 and cap distinct keys. A text
    # signed by more than cap keys is excluded from linkage entirely (too
    # promiscuous to be a meaningful signal), and a text signed by fewer
    # than 2 keys is not shared at all.
    bounded_templates = {t: dids for t, dids in text_to_dids.items() if 2 <= len(dids) <= cap}
    excluded_promiscuous_templates = sum(1 for dids in text_to_dids.values() if len(dids) > cap)

    # Pair counting: for each bounded template, every pair of its signers
    # gets its shared-template count incremented once. Memory here is
    # bounded by the sum, over bounded templates only, of C(len(dids), 2)
    # -- capped per template at C(cap, 2), since cap excludes anything
    # larger before it ever reaches this loop.
    pair_counts = {}
    keys_in_bounded_templates = set()
    for dids in bounded_templates.values():
        ordered = sorted(dids)
        keys_in_bounded_templates.update(ordered)
        for a, b in combinations(ordered, 2):
            pair_counts[(a, b)] = pair_counts.get((a, b), 0) + 1

    keys_in_bounded_templates = sorted(keys_in_bounded_templates)
    passes = []
    for threshold in (min_shared, min_shared + 1):
        sizes = _cluster_sizes(keys_in_bounded_templates, pair_counts, threshold)
        passes.append(_summarize_pass(sizes, threshold))

    coverage = CoverageTracker(data_dir).counters(room)
    coverage_ratio = CoverageTracker.coverage_ratio(
        coverage.get("captured_total", 0), coverage.get("dropped_total", 0)
    )

    return {
        "room": room,
        "cap": cap,
        "min_shared": min_shared,
        "messages_file_found": found,
        "signed_checked": checked,
        "signed_reverified": verified,
        "signed_reverify_failed": failed,
        "malformed_lines_skipped": malformed_lines,
        "distinct_keys_overall": len(distinct_dids),
        "distinct_shared_templates": sum(1 for dids in text_to_dids.values() if len(dids) >= 2),
        "bounded_template_count": len(bounded_templates),
        "excluded_promiscuous_template_count": excluded_promiscuous_templates,
        "keys_in_bounded_templates_count": len(keys_in_bounded_templates),
        "passes": passes,
        "coverage_captured_total": coverage.get("captured_total", 0),
        "coverage_dropped_total": coverage.get("dropped_total", 0),
        "coverage_ratio": coverage_ratio,
    }


def format_report(stats):
    """Render the human-readable report for `stats` (as returned by
    `compute_clustering_stats`).

    The cluster-size distribution is a FLOOR, not a verdict: it rests on
    re-verified signatures only, on exact-text bounded templates only (no
    near-duplicate matching), and is measured only at the coverage ratio
    captured below. It clusters only the minority of keys that share a
    bounded template at all; a cluster is a shared-behavior signal, never
    a proof of a single operator, and no individual DID is ever named
    anywhere in this output.
    """
    room = stats["room"]
    lines = []
    lines.append(f"Operator clustering -- room: {room}")
    lines.append("=" * (24 + len(room)))
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
        f"Bounded templates (signed by 2-{stats['cap']} distinct keys): {stats['bounded_template_count']} "
        f"of {stats['distinct_shared_templates']} shared templates "
        f"({stats['excluded_promiscuous_template_count']} excluded as too promiscuous, > {stats['cap']} keys)."
    )
    lines.append(
        f"Keys participating in at least one bounded template: {stats['keys_in_bounded_templates_count']} "
        f"of {stats['distinct_keys_overall']} distinct keys overall."
    )
    lines.append("")

    for entry in stats["passes"]:
        lines.append(f"Cluster-size distribution at min_shared={entry['min_shared']}:")
        lines.append(f"  clusters: {entry['cluster_count']} total, {entry['multi_key_cluster_count']} multi-key")
        lines.append(f"  singletons (no link at this threshold): {entry['singleton_count']}")
        lines.append(f"  largest cluster size: {entry['largest_cluster_size']}")
        lines.append("  size histogram (multi-key clusters only):")
        for bucket in HISTOGRAM_BUCKETS:
            lines.append(f"    {bucket}: {entry['size_histogram'][bucket]}")
        lines.append("")

    lines.append(
        "This is a FLOOR: it rests on exact-text bounded templates only (no near-duplicate "
        "matching) and is measured only at the coverage ratio stated above."
    )
    lines.append(f"Scope: {SCOPE_CAVEAT}")
    lines.append(f"Meaning: {CLUSTER_MEANING_CAVEAT}")
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
    return os.path.join(data_dir, "analysis", f"clustering_{room}_{ts}.json")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Operator clustering by shared bounded templates: a cluster-size "
        "distribution only, no key membership ever reported (read-only)."
    )
    parser.add_argument("--data-dir", required=True, help="collector data directory to read")
    parser.add_argument("--room", default="lobby", help="room to analyze (default: lobby)")
    parser.add_argument(
        "--cap",
        type=int,
        default=DEFAULT_CAP,
        help=f"a template signed by more than this many distinct keys is excluded from "
        f"linkage as too promiscuous (default: {DEFAULT_CAP})",
    )
    parser.add_argument(
        "--min-shared",
        type=int,
        default=DEFAULT_MIN_SHARED,
        help=f"minimum shared bounded templates to link two keys; min_shared + 1 is always "
        f"also reported (default: {DEFAULT_MIN_SHARED})",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="path to write the JSON report to "
        "(default: <data-dir>/analysis/clustering_<room>_<ts>.json)",
    )
    args = parser.parse_args(argv)

    stats = compute_clustering_stats(args.data_dir, room=args.room, cap=args.cap, min_shared=args.min_shared)
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
