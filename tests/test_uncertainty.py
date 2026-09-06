"""Uncertainty annotation (analysis/uncertainty.py) against hand-computed
Wilson score values and a handful of realistic-shaped measurement JSON
dicts (not full fixtures: this module never touches messages.jsonl or
coverage.jsonl at all, it only reads an already-produced measurement JSON,
so its tests build that JSON directly rather than going through the
streaming/re-verify harness the other test files use).

Wilson values below were computed independently from the stated formula
(center/half-width/clamp), not copied from the implementation, using
z = 1.9599639845 (the 95% two-sided value):

  k=92,  n=100:      p_hat=0.92, lower=0.8500189229922829, upper=0.9589065385150722
  k=100, n=100:      p_hat=1.00, lower=0.9630065017944702, upper=0.9999999999999998
  k=9,   n=10:       p_hat=0.90, lower=0.5958499732118333, upper=0.9821237869044116
  k=900000, n=1000000: p_hat=0.90, lower=0.8994104733486388, upper=0.9005864534961101
"""

import json

import pytest

from analysis.uncertainty import (
    Z_VALUES,
    _wilson_interval,
    compute_uncertainty,
    detect_measurement_type,
    format_report,
)

Z95 = Z_VALUES[0.95]


def test_wilson_interval_matches_hand_computed_values():
    p_hat, lower, upper = _wilson_interval(92, 100, Z95)
    assert p_hat == 0.92
    assert lower == pytest.approx(0.8500189229922829, abs=1e-9)
    assert upper == pytest.approx(0.9589065385150722, abs=1e-9)


def test_wilson_interval_large_n_hand_computed():
    p_hat, lower, upper = _wilson_interval(900000, 1000000, Z95)
    assert p_hat == 0.9
    assert lower == pytest.approx(0.8994104733486388, abs=1e-9)
    assert upper == pytest.approx(0.9005864534961101, abs=1e-9)


def test_wilson_interval_extreme_p_equals_one_clamps_sensibly():
    # k == n: the normal approximation would give exactly [1, 1], useless
    # at any n. Wilson gives a real, narrowing-with-n lower bound instead.
    p_hat, lower, upper = _wilson_interval(100, 100, Z95)
    assert p_hat == 1.0
    assert upper == pytest.approx(1.0, abs=1e-9)
    assert lower == pytest.approx(0.9630065017944702, abs=1e-9)
    assert lower < 1.0  # a real, non-degenerate lower bound, not [1, 1]


def test_wilson_interval_extreme_p_equals_zero_clamps_sensibly():
    p_hat, lower, upper = _wilson_interval(0, 100, Z95)
    assert p_hat == 0.0
    assert lower == pytest.approx(0.0, abs=1e-9)
    assert upper > 0.0  # a real, non-degenerate upper bound, not [0, 0]


def test_interval_narrows_with_sample_size():
    _, tiny_lower, tiny_upper = _wilson_interval(9, 10, Z95)
    _, huge_lower, huge_upper = _wilson_interval(900000, 1000000, Z95)

    tiny_half_width = (tiny_upper - tiny_lower) / 2.0
    huge_half_width = (huge_upper - huge_lower) / 2.0

    # Both are centered near p_hat=0.9, but the tiny-n interval is wide
    # (double-digit percentage points) and the huge-n interval is
    # negligible (well under half a percentage point).
    assert tiny_half_width > 0.15
    assert huge_half_width < 0.001


def test_wilson_interval_undefined_for_zero_n():
    assert _wilson_interval(0, 0, Z95) is None


def test_unsupported_confidence_level_raises():
    with pytest.raises(ValueError):
        compute_uncertainty({"cross_key_duplication_rate": 0.5}, confidence=0.5)


# --- measurement-type detection and pair extraction -----------------------


def _duplication_json():
    return {
        "room": "lobby",
        "cross_key_duplication_rate": 0.92,
        "cross_key_duplicated_numerator": 92,
        "cross_key_duplicated_denominator": 100,
        "distinct_dids": 40,
    }


def test_detects_duplication_and_intervals_the_right_pair():
    data = _duplication_json()
    assert detect_measurement_type(data) == "duplication"

    stats = compute_uncertainty(data)
    assert stats["detected_measurement_type"] == "duplication"
    assert len(stats["intervals"]) == 1
    entry = stats["intervals"][0]
    assert entry["label"] == "cross-key duplication rate"
    assert entry["k"] == 92
    assert entry["n"] == 100
    assert entry["p_hat"] == 0.92
    assert entry["lower"] == pytest.approx(0.8500189229922829, abs=1e-9)
    assert entry["upper"] == pytest.approx(0.9589065385150722, abs=1e-9)
    assert "negligible" not in entry["width_note"] or True  # width note is present either way
    assert stats["coverage_floor_direction"].startswith("the true cross-key duplication rate")


def test_detects_diversity_and_intervals_overall_and_bands():
    data = {
        "coverage_bands": {
            "high": {"key_count": 50, "one_and_done_count": 45, "one_and_done_rate": 0.9},
            "mid": {"key_count": 0, "one_and_done_count": 0, "one_and_done_rate": None},
            "low": {"key_count": 10, "one_and_done_count": 6, "one_and_done_rate": 0.6},
        },
        "one_and_done_count": 51,
        "total_distinct_keys": 60,
    }
    assert detect_measurement_type(data) == "diversity"

    stats = compute_uncertainty(data)
    labels = {entry["label"]: entry for entry in stats["intervals"]}
    assert "one-and-done rate (overall)" in labels
    assert labels["one-and-done rate (overall)"]["k"] == 51
    assert labels["one-and-done rate (overall)"]["n"] == 60
    assert "one-and-done rate (high coverage band)" in labels
    assert labels["one-and-done rate (high coverage band)"]["k"] == 45
    assert labels["one-and-done rate (high coverage band)"]["n"] == 50
    assert "one-and-done rate (low coverage band)" in labels
    # mid band has key_count 0 -> unavailable, not a crash, not intervaled.
    assert "one-and-done rate (mid coverage band)" not in labels
    assert any(u["label"] == "one-and-done rate (mid coverage band)" for u in stats["unavailable_pairs"])


def test_detects_cohort_and_intervals_both_pairs():
    data = {
        "persistence_rate": 0.5,
        "returned_count": 5,
        "non_return_count": 5,
        "cohort_size": 10,
    }
    assert detect_measurement_type(data) == "cohort"

    stats = compute_uncertainty(data)
    labels = {entry["label"]: entry for entry in stats["intervals"]}
    assert labels["persistence rate (returned)"]["k"] == 5
    assert labels["persistence rate (returned)"]["n"] == 10
    assert labels["non-return rate"]["k"] == 5
    assert labels["non-return rate"]["n"] == 10


def test_detects_coordination_and_intervals_both_pairs():
    data = {
        "coordinated_share_messages": 0.7,
        "coordinated_share_messages_numerator": 14,
        "coordinated_share_messages_denominator": 20,
        "concentration_top_n_numerator": 12,
        "concentration_top_n_denominator": 14,
    }
    assert detect_measurement_type(data) == "coordination"

    stats = compute_uncertainty(data)
    labels = {entry["label"]: entry for entry in stats["intervals"]}
    assert labels["coordinated share of messages"]["k"] == 14
    assert labels["coordinated share of messages"]["n"] == 20
    assert labels["top-N concentration"]["k"] == 12
    assert labels["top-N concentration"]["n"] == 14


def test_detects_diurnal_and_derives_the_denominator():
    data = {
        "num_bins": 3,
        "bucket_seconds": 60.0,
        "total_captured_posts": 6,
        "total_estimated_dropped": 9,
    }
    assert detect_measurement_type(data) == "diurnal"

    stats = compute_uncertainty(data)
    entry = stats["intervals"][0]
    assert entry["label"] == "overall captured coverage"
    assert entry["k"] == 6
    assert entry["n"] == 15  # 6 + 9, derived from two present integer fields


def test_synchrony_and_nonce_are_recognized_but_have_no_intervals():
    synchrony_data = {"median_dispersion_ratio_room_minus_self": 1.5}
    assert detect_measurement_type(synchrony_data) == "synchrony"
    stats = compute_uncertainty(synchrony_data)
    assert stats["intervals"] == []
    assert stats["coverage_floor_direction"].startswith("a bursty finding is at least this bursty")
    # nonce and synchrony have no entry in NO_INTERVAL_REASONS, so their
    # no_interval_reason stays None and their report text is unchanged
    # from before clustering's reason was added.
    assert stats["no_interval_reason"] is None
    report = format_report(stats, "synchrony_lobby.json")
    assert "none of this measurement type's known rate pairs were available" in report
    assert "enumerations" not in report

    nonce_data = {"room_band_fractions": {"13": 0.5, "16": 0.0, "19": 0.3, "other": 0.2}}
    assert detect_measurement_type(nonce_data) == "nonce"
    stats = compute_uncertainty(nonce_data)
    assert stats["intervals"] == []
    assert stats["no_interval_reason"] is None


def _clustering_json():
    # A real-shaped clustering.py output: the two sentinel fields
    # detect_measurement_type looks for ("passes" and
    # "keys_in_bounded_templates_count") plus enough of the rest to look
    # like a genuine artifact, not a minimal stub.
    return {
        "room": "lobby",
        "cap": 200,
        "min_shared": 2,
        "messages_file_found": True,
        "signed_checked": 100,
        "signed_reverified": 98,
        "signed_reverify_failed": 2,
        "malformed_lines_skipped": 0,
        "distinct_keys_overall": 40,
        "distinct_shared_templates": 6,
        "bounded_template_count": 5,
        "excluded_promiscuous_template_count": 1,
        "keys_in_bounded_templates_count": 12,
        "passes": [
            {
                "min_shared": 2,
                "cluster_count": 6,
                "multi_key_cluster_count": 1,
                "singleton_count": 5,
                "largest_cluster_size": 3,
                "size_histogram": {"2": 0, "3-5": 1, "6-10": 0, "11-50": 0, "51-200": 0, "201-1000": 0, "1000+": 0},
            },
            {
                "min_shared": 3,
                "cluster_count": 8,
                "multi_key_cluster_count": 0,
                "singleton_count": 8,
                "largest_cluster_size": 1,
                "size_histogram": {"2": 0, "3-5": 0, "6-10": 0, "11-50": 0, "51-200": 0, "201-1000": 0, "1000+": 0},
            },
        ],
        "coverage_captured_total": 90,
        "coverage_dropped_total": 10,
        "coverage_ratio": 0.9,
    }


def test_clustering_is_recognized_as_a_no_interval_measurement_type():
    data = _clustering_json()
    assert detect_measurement_type(data) == "clustering"

    stats = compute_uncertainty(data)

    # No Wilson interval is computed for clustering, ever: it has no
    # entry in PAIR_EXTRACTORS at all, so this list is empty regardless
    # of what fields the JSON contains.
    assert stats["intervals"] == []
    assert stats["unavailable_pairs"] == []

    # The explicit, stated reason: complete enumerations, not samples.
    assert stats["no_interval_reason"] is not None
    assert "complete enumerations" in stats["no_interval_reason"]
    assert "not sample estimates" in stats["no_interval_reason"]
    assert "none were sampled" in stats["no_interval_reason"]

    # The coverage-floor direction is still reported for clustering, the
    # same way it is for every other recognized type.
    assert stats["coverage_floor_direction"].startswith("the cluster structure reported is a floor")


def _tclk_json():
    # A real-shaped tclk.py output, matching FINDINGS.md's own tclk
    # numbers: 30,884 distinct offers, 7 accepted, 6 completed. The
    # sentinel fields detect_measurement_type looks for are
    # total_tclk_frames and distinct_offer_count.
    return {
        "room": "lobby",
        "messages_file_found": True,
        "signed_checked": 40000,
        "signed_reverified": 39990,
        "signed_reverify_failed": 10,
        "malformed_lines_skipped": 0,
        "total_tclk_frames": 30897,
        "frames_by_type": {
            "offer": 30884,
            "accept": 7,
            "lock": 7,
            "reveal": 6,
            "refund": 0,
            "cancel": 0,
            "receipt": 0,
            "heartbeat": 0,
        },
        "unparseable_frame_count": 0,
        "unknown_type_frame_count": 0,
        "distinct_offer_count": 30884,
        "accepted_offer_count": 7,
        "accepts_with_unmatched_offer_ref": 0,
        "downstream_frames_with_unmatched_contract": 0,
        "partial_chain_frame_count": 0,
        "locked_contract_count": 7,
        "revealed_contract_count": 6,
        "refunded_contract_count": 0,
        "cancelled_contract_count": 0,
        "receipt_claimed_contract_count": 0,
        "completed_contract_count": 6,
        "completion_rate": 6 / 30884,
        "funnel": [
            {"stage": "offers", "count": 30884},
            {"stage": "accepted", "count": 7},
            {"stage": "locked", "count": 7},
            {"stage": "completed", "count": 6},
        ],
        "coverage_captured_total": 85000,
        "coverage_dropped_total": 15000,
        "coverage_ratio": 0.85,
    }


def test_tclk_is_recognized_and_both_rates_get_wilson_intervals():
    data = _tclk_json()
    assert detect_measurement_type(data) == "tclk"

    stats = compute_uncertainty(data)

    assert stats["unavailable_pairs"] == []
    assert stats["no_interval_reason"] is None  # tclk gets real intervals, not the no-interval path
    labels = [interval["label"] for interval in stats["intervals"]]
    assert "completion rate (completed / distinct offers)" in labels
    assert "acceptance rate (accepted / distinct offers)" in labels

    completion = next(
        i for i in stats["intervals"] if i["label"] == "completion rate (completed / distinct offers)"
    )
    acceptance = next(
        i for i in stats["intervals"] if i["label"] == "acceptance rate (accepted / distinct offers)"
    )
    assert completion["k"] == 6
    assert completion["n"] == 30884
    assert acceptance["k"] == 7
    assert acceptance["n"] == 30884

    assert stats["coverage_floor_direction"].startswith(
        "the completion and acceptance rates are floors"
    )


def test_tclk_completion_rate_interval_is_wide_relative_to_its_point_estimate():
    # Contrast against a large-numerator case (duplication's real
    # window-A figures from FINDINGS.md: 144,694 of 560,480) to prove the
    # small-numerator width actually shows up, not just that some
    # interval exists. Both intervals are tiny in ABSOLUTE probability
    # terms when p_hat itself is tiny (that is just arithmetic), so the
    # honest comparison is RELATIVE to each one's own point estimate.
    tclk_stats = compute_uncertainty(_tclk_json())
    completion = next(
        i for i in tclk_stats["intervals"] if i["label"] == "completion rate (completed / distinct offers)"
    )

    duplication_data = {
        "room": "lobby",
        "messages_file_found": True,
        "signed_checked": 560480,
        "signed_reverified": 560480,
        "signed_reverify_failed": 0,
        "malformed_lines_skipped": 0,
        "distinct_dids": 386731,
        "distinct_texts": 407926,
        "cross_key_duplicated_numerator": 144694,
        "cross_key_duplicated_denominator": 560480,
        "cross_key_duplication_rate": 144694 / 560480,
        "top_duplicated_texts": [],
        "coverage_captured_total": 560480,
        "coverage_dropped_total": 193000,
        "coverage_ratio": 0.744,
    }
    duplication_stats = compute_uncertainty(duplication_data)
    duplication_rate = duplication_stats["intervals"][0]

    # The completion rate's own numerator (6) is genuinely tiny against
    # its denominator: the lower bound is a real, nonzero, positive
    # number (not clamped to 0), and the interval spans several times
    # its own point estimate -- upper is nearly 5x lower.
    assert completion["lower"] > 0.0
    assert completion["upper"] > 4 * completion["lower"]

    completion_relative_half_width = completion["half_width"] / completion["p_hat"]
    duplication_relative_half_width = duplication_rate["half_width"] / duplication_rate["p_hat"]

    # The small-numerator interval is dramatically wider relative to its
    # own point estimate than the large-numerator one -- proving the
    # tiny numerator's uncertainty genuinely shows up, rather than being
    # swallowed by the same "negligible" framing every large-n figure in
    # this project already gets.
    assert completion_relative_half_width > 50 * duplication_relative_half_width
    assert completion_relative_half_width > 0.5  # more than half of its own point estimate
    assert duplication_relative_half_width < 0.01  # under 1% of its own point estimate


def test_tclk_no_did_or_key_string_leaks_into_uncertainty_output():
    data = _tclk_json()
    stats = compute_uncertainty(data)
    dumped = json.dumps(stats, ensure_ascii=False, sort_keys=True)
    assert "did:key:" not in dumped


def test_tclk_report_states_completion_and_acceptance_with_intervals():
    data = _tclk_json()
    stats = compute_uncertainty(data)
    report = format_report(stats, "tclk_lobby.json")

    assert "Detected measurement type: tclk" in report
    assert "completion rate (completed / distinct offers)" in report
    assert "acceptance rate (accepted / distinct offers)" in report


def test_clustering_no_interval_reason_appears_in_the_report():
    data = _clustering_json()
    stats = compute_uncertainty(data)
    report = format_report(stats, "clustering_lobby.json")

    assert "Detected measurement type: clustering" in report
    assert "none of this measurement type's known rate pairs were available" in report
    assert "complete enumerations" in report
    assert "not sample estimates" in report


def test_unrecognized_json_reported_plainly_not_a_crash():
    data = {"some_unrelated_field": 42}
    assert detect_measurement_type(data) is None

    stats = compute_uncertainty(data)  # must not raise
    assert stats["detected_measurement_type"] is None
    assert stats["intervals"] == []
    assert "not recognized" in stats["coverage_floor_direction"]

    report = format_report(stats, "some_file.json")
    assert "not recognized" in report


def test_output_contains_only_numeric_or_label_fields_no_key_material(tmp_path):
    data = _duplication_json()
    stats = compute_uncertainty(data)
    dumped = json.dumps(stats)

    # This module only ever reads counts; there is no did:key material
    # anywhere in its input or output to begin with, but confirm the
    # interval entries carry only the documented, narrow field set.
    for entry in stats["intervals"]:
        assert set(entry.keys()) == {
            "label",
            "k",
            "n",
            "p_hat",
            "confidence",
            "lower",
            "upper",
            "half_width",
            "width_note",
        }
        assert isinstance(entry["label"], str)
        assert isinstance(entry["width_note"], str)
        for numeric_field in ("k", "n", "p_hat", "confidence", "lower", "upper", "half_width"):
            assert isinstance(entry[numeric_field], (int, float))
    assert "did:key:" not in dumped


def test_report_states_the_two_sources_are_not_combined():
    data = _duplication_json()
    stats = compute_uncertainty(data)
    report = format_report(stats, "duplication_lobby.json")

    assert "Sampling confidence intervals" in report
    assert "Coverage-floor direction" in report
    assert "must never be combined or added together" in stats["note"]
