"""Uncertainty annotation: a standalone layer over the Basanos measurement
modules, not an eighth measurement of its own.

This module reads the already-written JSON output of one of the seven
measurement modules (`analysis/duplication.py`, `coordination.py`,
`synchrony.py`, `diurnal.py`, `nonce.py`, `diversity.py`, `cohort.py`) and
adds a sampling confidence interval to whichever reported rates have a
clean integer numerator and denominator available in that file, plus a
stated coverage-floor direction for that measurement type. It does NOT
recompute anything: it never opens `messages.jsonl`, `coverage.jsonl`, or
any collector data at all, and it never modifies the measurement file it
reads. Its own output goes to a NEW file, never overwriting the input --
the input is somebody's already-published result.

TWO DIFFERENT, NON-ADDITIVE KINDS OF UNCERTAINTY, KEPT SEPARATE:

1. THE SAMPLING CONFIDENCE INTERVAL (two-sided, computed here): how much a
   reported proportion k/n could plausibly differ from its true value
   just from having observed only n trials, using a Wilson score interval
   (see `_wilson_interval` below for why Wilson and not the normal
   approximation). At the sample sizes this project's measurements
   actually reach, this interval is almost always negligible -- which is
   itself a useful, reportable fact: it means a headline rate is not a
   small-sample fluke, not a source of doubt to explain away.

2. THE COVERAGE-FLOOR DIRECTION (one-sided, STATED not computed): every
   measurement module in this project already states, in its own words,
   that its headline number is a FLOOR because the collector's ring
   eviction loses the burstiest, most-affected traffic first -- so the
   true value can only be pushed in one known direction by what was
   missed, never estimated in magnitude from the JSON alone. This module
   cannot compute that direction from the numbers (there is no way to
   measure what was never captured); it can only look up and restate, in
   one line, what each measurement module already says about its own
   coverage floor.

These are answers to two different questions ("how much could sampling
noise move this number" vs "which way does missing data bias this
number") and must never be added together, subtracted, or otherwise
combined into one interval -- the report and the JSON output both keep
them in clearly separate sections for exactly this reason.

WHICH NUMBERS GET AN INTERVAL: this module does not attempt to discover
every numeric field in a measurement's JSON and guess which ones are
proportions. It works from an explicit, small, per-measurement-type table
of (label, numerator field, denominator field) triples, each one picked
because it backs that measurement's own headline rate -- see
`PAIR_EXTRACTORS` below. A measurement type recognized but with none of
its listed pairs present in the given JSON is reported as such, plainly,
never guessed at from unrelated fields.

Usage:
    python -m analysis.uncertainty --input <path-to-measurement.json> \\
        [--confidence 0.95] [--measurement-type duplication] [--out <path>]
"""

import argparse
import json
import math
import os

# Two-sided z for the three supported confidence levels. Restricted to
# these three (rather than an inverse-normal computed for an arbitrary
# level) because the standard library has no inverse normal CDF, and
# these three cover every confidence level this project has any reason to
# report at -- adding a fourth is a one-line table edit, not a new
# dependency.
Z_VALUES = {
    0.90: 1.6448536270,
    0.95: 1.9599639845,
    0.99: 2.5758293035,
}

# One stated sentence per measurement type: which way the collector's own
# coverage floor pushes the true value, in that measurement's own words.
# Never computed -- there is no way to measure what was never captured;
# this is a restatement of what each module already says about itself.
COVERAGE_FLOOR_DIRECTIONS = {
    "duplication": (
        "the true cross-key duplication rate is at least this: unseen evicted traffic is "
        "the burstiest and most duplicate-heavy, so coverage loss can only raise it."
    ),
    "coordination": (
        "the true coordination concentration is at least this, for the same reason as "
        "duplication: evicted traffic is the burstiest and likeliest to belong to a "
        "coordinated burst, so coverage loss can only raise the reported concentration."
    ),
    "synchrony": (
        "a bursty finding is at least this bursty: the evicted traffic is itself the "
        "burstiest stretch a room produces, so missing it can only understate a "
        "dispersion ratio, never manufacture one that is not really there."
    ),
    "nonce": (
        "a reported divergence is at least this large: missing traffic is drawn from the "
        "same coordinated bursts the divergence is measuring, so its omission cannot "
        "manufacture a divergence that is not really there."
    ),
    "diurnal": (
        "the captured curve is a floor on activity; true activity in any bin is at least "
        "the estimated-throughput figure shown, and the estimated-throughput curve is "
        "itself only as good as the coverage ratio reported beside it."
    ),
    "diversity": (
        "the true one-and-done rate is at least the figure in the best-captured (high) "
        "coverage band; the figure in the LOW coverage band is biased DOWNWARD, not "
        "upward, because heavy eviction favors survival of repeat posters over a true "
        "one-and-done key's single chance to be captured at all -- read the high band as "
        "the floor, never the low one."
    ),
    "cohort": (
        "the non-return rate is a floor only to the extent window 2 itself was well "
        "captured: an under-captured window 2 can only inflate apparent non-return by "
        "missing a real return, never manufacture a return that did not happen."
    ),
}


def _wilson_interval(k, n, z):
    """Two-sided Wilson score confidence interval for a proportion k/n at
    the given z (see Z_VALUES for the three supported confidence levels).

        center = (p_hat + z^2/(2n)) / (1 + z^2/n)
        half   = (z / (1 + z^2/n)) * sqrt(p_hat*(1-p_hat)/n + z^2/(4n^2))
        lower  = center - half, upper = center + half, both clamped to [0, 1]

    Wilson, not the normal approximation, on purpose: several of this
    project's rates are extreme (0.92, 0.95, and similar) and close to the
    0 or 1 boundary, exactly where the normal approximation's symmetric
    interval around p_hat can extend past 0 or past 1 (a nonsensical
    probability) or collapse to a single point at p_hat=1 regardless of n.
    Wilson is centered away from p_hat toward 0.5 and derived from
    inverting the normal test statistic rather than assuming symmetry
    around p_hat, so it stays inside [0, 1] and still narrows sensibly as
    n grows even at p_hat=1 (see the tests for the k=n case). Standard
    library only (`math.sqrt`), no numpy or scipy.

    Returns (p_hat, lower, upper), or None if n is not positive (nothing
    to compute a proportion from).
    """
    if n <= 0:
        return None
    p_hat = k / n
    denom = 1.0 + (z * z) / n
    center = (p_hat + (z * z) / (2 * n)) / denom
    half = (z / denom) * math.sqrt((p_hat * (1 - p_hat)) / n + (z * z) / (4 * n * n))
    lower = max(0.0, center - half)
    upper = min(1.0, center + half)
    return p_hat, lower, upper


def _width_note(p_hat, half_width):
    """A plain-language judgment of whether an interval is negligible or
    not, relative to its own point estimate. At this project's sample
    sizes the interval is almost always negligible -- reported as a
    feature (the finding is not a small-sample fluke), not a hedge.
    """
    if half_width < 0.01:
        return "negligible (absolute half-width under 1 percentage point)"
    if p_hat > 0 and (half_width / p_hat) < 0.1:
        return "negligible relative to the point estimate (well under 10% of it)"
    return "not negligible relative to the point estimate -- treat this rate with more caution"


def _duplication_pairs(data):
    return [("cross-key duplication rate", data.get("cross_key_duplicated_numerator"), data.get("cross_key_duplicated_denominator"))]


def _coordination_pairs(data):
    return [
        (
            "coordinated share of messages",
            data.get("coordinated_share_messages_numerator"),
            data.get("coordinated_share_messages_denominator"),
        ),
        (
            "top-N concentration",
            data.get("concentration_top_n_numerator"),
            data.get("concentration_top_n_denominator"),
        ),
    ]


def _diurnal_pairs(data):
    captured = data.get("total_captured_posts")
    dropped = data.get("total_estimated_dropped")
    denominator = (captured + dropped) if isinstance(captured, int) and isinstance(dropped, int) else None
    return [("overall captured coverage", captured, denominator)]


def _diversity_pairs(data):
    pairs = [("one-and-done rate (overall)", data.get("one_and_done_count"), data.get("total_distinct_keys"))]
    bands = data.get("coverage_bands")
    if isinstance(bands, dict):
        for band in ("high", "mid", "low"):
            entry = bands.get(band)
            if isinstance(entry, dict):
                pairs.append(
                    (
                        f"one-and-done rate ({band} coverage band)",
                        entry.get("one_and_done_count"),
                        entry.get("key_count"),
                    )
                )
    return pairs


def _cohort_pairs(data):
    return [
        ("persistence rate (returned)", data.get("returned_count"), data.get("cohort_size")),
        ("non-return rate", data.get("non_return_count"), data.get("cohort_size")),
    ]


# Measurement type -> a function that pulls its candidate (label, k, n)
# triples out of that measurement's JSON. nonce and synchrony are
# deliberately absent: neither reports a clean single binary k/n
# proportion at the top level (nonce's bands are a four-way split, not a
# yes/no proportion; synchrony's dispersion ratios have no stored
# numerator/denominator pair backing them) -- rather than force a
# proportion out of numbers that are not one, they are recognized as
# measurement types (for the coverage-floor direction) but contribute no
# intervals.
PAIR_EXTRACTORS = {
    "duplication": _duplication_pairs,
    "coordination": _coordination_pairs,
    "diurnal": _diurnal_pairs,
    "diversity": _diversity_pairs,
    "cohort": _cohort_pairs,
}


def detect_measurement_type(data):
    """Identify which measurement module produced `data` from a field
    unique to that module's own output shape. Returns None if nothing
    recognizable is found -- never a guess.
    """
    if "cross_key_duplication_rate" in data:
        return "duplication"
    if "coordinated_share_messages" in data:
        return "coordination"
    if "median_dispersion_ratio_room_minus_self" in data:
        return "synchrony"
    if "room_band_fractions" in data:
        return "nonce"
    if "num_bins" in data and "bucket_seconds" in data:
        return "diurnal"
    if "coverage_bands" in data:
        return "diversity"
    if "persistence_rate" in data:
        return "cohort"
    return None


def compute_uncertainty(data, measurement_type=None, confidence=0.95):
    """Compute sampling confidence intervals for every recognized
    proportion in `data` (a measurement module's already-loaded JSON
    output), plus that measurement type's stated coverage-floor
    direction.

    `measurement_type`, if given, overrides auto-detection (see
    `detect_measurement_type`). `confidence` must be one of the levels in
    `Z_VALUES` (0.90, 0.95, 0.99); anything else raises ValueError rather
    than silently approximating a z value.

    Returns a dict with the raw inputs, the list of computed intervals,
    the list of recognized-but-unavailable pairs (present in the pair
    table for this measurement type but missing or unusable in this
    particular JSON), and the coverage-floor direction. Reads only the
    passed-in dict; performs no file I/O and touches no key material --
    every number handled here is a count or a rate, never a name.
    """
    if confidence not in Z_VALUES:
        raise ValueError(
            f"unsupported confidence level {confidence!r}; supported: {sorted(Z_VALUES)}"
        )
    z = Z_VALUES[confidence]

    detected_type = measurement_type or detect_measurement_type(data)

    intervals = []
    unavailable_pairs = []

    extractor = PAIR_EXTRACTORS.get(detected_type) if detected_type else None
    if extractor is not None:
        for label, k, n in extractor(data):
            if not isinstance(k, int) or not isinstance(n, int) or n <= 0:
                unavailable_pairs.append(
                    {"label": label, "reason": "numerator/denominator not both available as positive integers"}
                )
                continue
            p_hat, lower, upper = _wilson_interval(k, n, z)
            half_width = (upper - lower) / 2.0
            intervals.append(
                {
                    "label": label,
                    "k": k,
                    "n": n,
                    "p_hat": p_hat,
                    "confidence": confidence,
                    "lower": lower,
                    "upper": upper,
                    "half_width": half_width,
                    "width_note": _width_note(p_hat, half_width),
                }
            )

    if detected_type is None:
        coverage_floor_direction = "not available: measurement type not recognized from this JSON"
    else:
        coverage_floor_direction = COVERAGE_FLOOR_DIRECTIONS.get(
            detected_type, f"no stated coverage-floor direction on file for measurement type {detected_type!r}"
        )

    return {
        "detected_measurement_type": detected_type,
        "confidence": confidence,
        "z": z,
        "intervals": intervals,
        "unavailable_pairs": unavailable_pairs,
        "coverage_floor_direction": coverage_floor_direction,
        "note": (
            "the sampling confidence interval(s) above and the coverage-floor direction "
            "below come from two different sources (observed-sample noise vs. a stated, "
            "uncomputed bias from missed traffic) and must never be combined or added "
            "together."
        ),
    }


def format_report(stats, input_path):
    """Render the human-readable report for `stats` (as returned by
    `compute_uncertainty`).
    """
    lines = []
    lines.append(f"Uncertainty annotation -- input: {input_path}")
    lines.append("=" * (26 + len(input_path)))
    lines.append("")

    measurement_type = stats["detected_measurement_type"]
    if measurement_type is None:
        lines.append("Measurement type not recognized from this JSON -- no intervals to compute.")
        lines.append("")
        lines.append(f"Coverage-floor direction: {stats['coverage_floor_direction']}")
        return "\n".join(lines)

    lines.append(f"Detected measurement type: {measurement_type}")
    lines.append(f"Confidence level: {stats['confidence']:.2f} (z = {stats['z']:.4f})")
    lines.append("")

    lines.append("1. Sampling confidence intervals (Wilson score, two-sided):")
    if not stats["intervals"]:
        lines.append("   none of this measurement type's known rate pairs were available in this JSON.")
    else:
        for entry in stats["intervals"]:
            lines.append(
                f"   {entry['label']}: {entry['k']}/{entry['n']} = {100.0 * entry['p_hat']:.2f}%, "
                f"{100.0 * stats['confidence']:.0f}% CI [{100.0 * entry['lower']:.2f}%, "
                f"{100.0 * entry['upper']:.2f}%]"
            )
            lines.append(f"     {entry['width_note']}")
    if stats["unavailable_pairs"]:
        lines.append("   recognized but unavailable in this JSON:")
        for entry in stats["unavailable_pairs"]:
            lines.append(f"     {entry['label']}: {entry['reason']}")
    lines.append("")

    lines.append("2. Coverage-floor direction (stated, not computed, one-sided, separate from the above):")
    lines.append(f"   {stats['coverage_floor_direction']}")
    lines.append("")

    lines.append(f"Note: {stats['note']}")

    return "\n".join(lines)


def default_out_path(input_path):
    directory = os.path.dirname(input_path) or "."
    basename = os.path.basename(input_path)
    return os.path.join(directory, f"uncertainty_{basename}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Add sampling confidence intervals and a stated coverage-floor "
        "direction to an existing Basanos measurement JSON file (read-only; never "
        "recomputes the measurement, never overwrites the input)."
    )
    parser.add_argument("--input", required=True, help="path to a measurement module's JSON output")
    parser.add_argument(
        "--confidence",
        type=float,
        default=0.95,
        help=f"confidence level, one of {sorted(Z_VALUES)} (default: 0.95)",
    )
    parser.add_argument(
        "--measurement-type",
        default=None,
        choices=sorted(set(PAIR_EXTRACTORS) | set(COVERAGE_FLOOR_DIRECTIONS)),
        help="override auto-detection of the measurement type",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="path to write the JSON report to (default: uncertainty_<input filename>, "
        "in the same directory as --input; never overwrites --input)",
    )
    args = parser.parse_args(argv)

    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)

    stats = compute_uncertainty(data, measurement_type=args.measurement_type, confidence=args.confidence)
    print(format_report(stats, args.input))

    out_path = args.out or default_out_path(args.input)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
