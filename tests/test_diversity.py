"""Per-key content diversity (analysis/diversity.py) against two known,
synthetic fixtures.

FIXTURE 1 (no coverage.jsonl): pins the overall distinct-text distribution
and one-and-done rate by hand, and specifically includes a key that posts
the SAME text twice (2 messages, 1 distinct text) to prove one-and-done
requires BOTH exactly one message AND exactly one distinct text, not just
one of the two.
- K1: 1 message, 1 distinct text -> one-and-done.
- K2: 2 messages, same text twice -> 1 distinct text but NOT one-and-done
  (message count is 2).
- K3: 3 messages, 3 distinct texts -> not one-and-done.
Also proves missing coverage.jsonl is handled without crashing: the
overall rate is still computed, stratification is reported unavailable.

FIXTURE 2 (the load-bearing one): a coverage.jsonl with two real hours of
coverage (hour A high, hour B low) plus a restart (negative delta,
skipped), and keys whose first-seen ts places them in hour A, hour B, an
hour with no coverage data at all, or with no parseable ts at all -- so
every exclusion path and both real bands are exercised with a hand-pinned
rate in each.

Uses tests/fixtures/make_fixtures.py's deterministic throwaway-key
approach (FIXTURE_KEY_1/2/3 plus extra labels for more keys), the same
pattern tests/test_coordination.py and tests/test_diurnal.py use -- no
real did:key identity involved, and make_fixtures.py itself is not
modified.
"""

import json
import os
from datetime import datetime, timezone

import pytest

from make_fixtures import FIXTURE_DID_1, FIXTURE_DID_2, FIXTURE_KEY_1, FIXTURE_KEY_2, _did_key, _fixture_key, _sign

from analysis.diversity import compute_diversity_stats, format_report

ROOM = "lobby"

FIXTURE_KEY_3 = _fixture_key("three")
FIXTURE_DID_3 = _did_key(FIXTURE_KEY_3.public_key().public_bytes_raw())
FIXTURE_KEY_4 = _fixture_key("four")
FIXTURE_DID_4 = _did_key(FIXTURE_KEY_4.public_key().public_bytes_raw())
FIXTURE_KEY_5 = _fixture_key("five")
FIXTURE_DID_5 = _did_key(FIXTURE_KEY_5.public_key().public_bytes_raw())
FIXTURE_KEY_6 = _fixture_key("six")
FIXTURE_DID_6 = _did_key(FIXTURE_KEY_6.public_key().public_bytes_raw())
FIXTURE_KEY_7 = _fixture_key("seven")
FIXTURE_DID_7 = _did_key(FIXTURE_KEY_7.public_key().public_bytes_raw())

ALL_DIDS = [
    FIXTURE_DID_1,
    FIXTURE_DID_2,
    FIXTURE_DID_3,
    FIXTURE_DID_4,
    FIXTURE_DID_5,
    FIXTURE_DID_6,
    FIXTURE_DID_7,
]

KEYS = {
    1: (FIXTURE_KEY_1, FIXTURE_DID_1),
    2: (FIXTURE_KEY_2, FIXTURE_DID_2),
    3: (FIXTURE_KEY_3, FIXTURE_DID_3),
    4: (FIXTURE_KEY_4, FIXTURE_DID_4),
    5: (FIXTURE_KEY_5, FIXTURE_DID_5),
    6: (FIXTURE_KEY_6, FIXTURE_DID_6),
    7: (FIXTURE_KEY_7, FIXTURE_DID_7),
}


def _iso(ts_seconds):
    return datetime.fromtimestamp(ts_seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _signed(key_index, seq, text, nonce, ts_seconds):
    key, did = KEYS[key_index]
    ts = None if ts_seconds is None else _iso(ts_seconds)
    return {
        "room": ROOM,
        "seq": seq,
        "ts": ts,
        "from": did,
        "text": text,
        "nonce": str(nonce),
        "sig": _sign(key, ROOM, str(nonce), text),
        "captured_at": ts,
        "source": "test",
    }


def _write_messages(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True))
            f.write("\n")


def _write_coverage(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True))
            f.write("\n")


# --- Fixture 1: overall distribution and one-and-done, no coverage.jsonl ---


def _build_fixture_one_records():
    return [
        _signed(1, 1, "k1 only text", 9001, 1_700_000_000),
        _signed(2, 2, "k2 repeated text", 9002, 1_700_000_010),
        _signed(2, 3, "k2 repeated text", 9003, 1_700_000_020),  # same text again
        _signed(3, 4, "k3 text a", 9004, 1_700_000_030),
        _signed(3, 5, "k3 text b", 9005, 1_700_000_040),
        _signed(3, 6, "k3 text c", 9006, 1_700_000_050),
    ]


def _setup_fixture_one(tmp_path):
    data_dir = tmp_path / "data"
    _write_messages(str(data_dir / "rooms" / ROOM / "messages.jsonl"), _build_fixture_one_records())
    return str(data_dir)


def test_reverify_counts_fixture_one(tmp_path):
    data_dir = _setup_fixture_one(tmp_path)
    stats = compute_diversity_stats(data_dir, room=ROOM)

    assert stats["signed_checked"] == 6
    assert stats["signed_reverified"] == 6
    assert stats["signed_reverify_failed"] == 0


def test_distinct_text_buckets_hand_calculated(tmp_path):
    data_dir = _setup_fixture_one(tmp_path)
    stats = compute_diversity_stats(data_dir, room=ROOM)

    # K1: 1 distinct text -> bucket "1". K2: 1 distinct text (same text
    # twice) -> bucket "1". K3: 3 distinct texts -> bucket "2-5".
    assert stats["total_distinct_keys"] == 3
    assert stats["distinct_text_buckets"] == {"1": 2, "2-5": 1, "6-10": 0, "11-50": 0, "51+": 0}
    assert stats["max_distinct_texts"] == 3
    assert stats["distinct_text_level_counts"] == {"1": 2, "3": 1}


def test_one_and_done_requires_both_conditions(tmp_path):
    data_dir = _setup_fixture_one(tmp_path)
    stats = compute_diversity_stats(data_dir, room=ROOM)

    # Only K1 is one-and-done. K2 has exactly one distinct text but posted
    # 2 messages, so it fails the "exactly one message" half of the
    # definition and must NOT count as one-and-done. K3 fails both halves.
    assert stats["one_and_done_count"] == 1
    assert stats["one_and_done_rate"] == 1 / 3


def test_missing_coverage_file_does_not_crash_overall_rate_still_computed(tmp_path):
    data_dir = _setup_fixture_one(tmp_path)
    stats = compute_diversity_stats(data_dir, room=ROOM)

    assert stats["coverage_file_found"] is False
    # Overall rate still computed even with no coverage.jsonl at all.
    assert stats["one_and_done_rate"] == 1 / 3
    # No band data to report.
    assert all(stats["coverage_bands"][b]["one_and_done_rate"] is None for b in ("high", "mid", "low"))
    assert all(stats["coverage_bands"][b]["key_count"] == 0 for b in ("high", "mid", "low"))

    report = format_report(stats)
    assert "No coverage.jsonl found" in report


def test_room_text_diversity_supporting_stats(tmp_path):
    data_dir = _setup_fixture_one(tmp_path)
    stats = compute_diversity_stats(data_dir, room=ROOM)

    # 5 distinct texts room-wide (k1 only text, k2 repeated text, k3 text
    # a/b/c) over 6 total messages: k2's repeated text has count 2, every
    # other text has count 1.
    assert stats["distinct_texts_room_wide"] == 5
    assert stats["most_common_text_share"] == 2 / 6


def test_no_did_string_anywhere_in_json_output(tmp_path):
    data_dir = _setup_fixture_one(tmp_path)
    stats = compute_diversity_stats(data_dir, room=ROOM)
    dumped = json.dumps(stats)

    for did in ALL_DIDS:
        assert did not in dumped


def test_report_contains_required_language_and_no_dids(tmp_path):
    data_dir = _setup_fixture_one(tmp_path)
    stats = compute_diversity_stats(data_dir, room=ROOM)
    report = format_report(stats)

    assert "FLOOR" in report
    assert "at least" in report.lower()
    assert "heartbeat-style posting" in report
    assert "not a verdict about any poster" in report
    assert "checked" in report and "re-verified" in report and "failed" in report
    for did in ALL_DIDS:
        assert did not in report


def test_missing_messages_file_does_not_crash(tmp_path):
    data_dir = tmp_path / "empty_data"
    os.makedirs(str(data_dir), exist_ok=True)
    stats = compute_diversity_stats(str(data_dir), room="lobby")
    assert stats["messages_file_found"] is False
    assert stats["signed_checked"] == 0
    report = format_report(stats)
    assert "No messages.jsonl found" in report


def test_invalid_room_is_rejected(tmp_path):
    data_dir = tmp_path / "data"
    os.makedirs(str(data_dir), exist_ok=True)
    with pytest.raises(ValueError):
        compute_diversity_stats(str(data_dir), room="../../escaped")


# --- Fixture 2: coverage stratification, the load-bearing one ---
#
# Two absolute UTC hours, chosen far apart so their windows never overlap:
#   hour A index = 500000 -> starts at 500000 * 3600 = 1_800_000_000
#   hour B index = 500001 -> starts at 500001 * 3600 = 1_800_003_600
#   hour C index = 600000 -> starts at 600000 * 3600 = 2_160_000_000 (no coverage data at all)
#
# Coverage snapshots for "lobby" (cumulative captured_total/dropped_total):
#   snap0: captured_at = hour A start - 100        -> captured_total=0,  dropped_total=0
#   snap1: captured_at = hour A start + 1800        -> captured_total=95, dropped_total=5
#     (snap0 -> snap1: +95 captured, +5 dropped, in hour A -> coverage 95/100 = 0.95 -> "high")
#   snap2: captured_at = hour A start + 3000        -> captured_total=50, dropped_total=3
#     (snap1 -> snap2: captured_total DROPS 95 -> 50: RESTART, skipped entirely)
#   snap3: captured_at = hour B start + 1800        -> captured_total=55, dropped_total=8
#     (snap2 -> snap3: +5 captured, +5 dropped, in hour B -> coverage 5/10 = 0.5 -> "low")
#
# Keys, by first-seen ts:
#   KA1 (hour A, offset +100): 1 message  -> one-and-done
#   KA2 (hour A, offset +200): 2 distinct texts -> not one-and-done
#     -> hour A: 2 keys, 1 one-and-done -> rate 1/2 = 0.5
#   KB1 (hour B, offset +100): 1 message -> one-and-done
#   KB2 (hour B, offset +200): same text twice -> not one-and-done
#   KB3 (hour B, offset +300): 2 distinct texts -> not one-and-done
#     -> hour B: 3 keys, 1 one-and-done -> rate 1/3
#   KC1: 1 message, ts=None (unparsable) -> one-and-done overall, but
#     excluded from every band (no parseable first-seen ts at all).
#   KD1 (hour C, offset +100): 1 message -> one-and-done overall, but
#     excluded from every band (hour C has no coverage data at all).

HOUR_A_START = 500000 * 3600
HOUR_B_START = 500001 * 3600
HOUR_C_START = 600000 * 3600


def _build_fixture_two_records():
    records = [
        _signed(1, 1, "ka1 text", 9101, HOUR_A_START + 100),
        _signed(2, 2, "ka2 text one", 9102, HOUR_A_START + 200),
        _signed(2, 3, "ka2 text two", 9103, HOUR_A_START + 210),
        _signed(3, 4, "kb1 text", 9104, HOUR_B_START + 100),
        _signed(4, 5, "kb2 repeated text", 9105, HOUR_B_START + 200),
        _signed(4, 6, "kb2 repeated text", 9106, HOUR_B_START + 210),
        _signed(5, 7, "kb3 text one", 9107, HOUR_B_START + 300),
        _signed(5, 8, "kb3 text two", 9108, HOUR_B_START + 310),
        _signed(6, 9, "kc1 text", 9109, None),
        _signed(7, 10, "kd1 text", 9110, HOUR_C_START + 100),
    ]
    return records


def _build_fixture_two_coverage_records():
    snapshots = [
        (HOUR_A_START - 100, 0, 0),
        (HOUR_A_START + 1800, 95, 5),
        (HOUR_A_START + 3000, 50, 3),  # restart: captured_total drops
        (HOUR_B_START + 1800, 55, 8),
    ]
    records = []
    for captured_at_seconds, captured_total, dropped_total in snapshots:
        records.append(
            {
                "room": ROOM,
                "captured_total": captured_total,
                "dropped_total": dropped_total,
                "coverage": None,
                "cursor": None,
                "captured_at": _iso(captured_at_seconds),
                "source": "test",
            }
        )
    return records


def _setup_fixture_two(tmp_path):
    data_dir = tmp_path / "data"
    _write_messages(str(data_dir / "rooms" / ROOM / "messages.jsonl"), _build_fixture_two_records())
    _write_coverage(str(data_dir / "coverage.jsonl"), _build_fixture_two_coverage_records())
    return str(data_dir)


def test_coverage_stratification_bands_hand_calculated(tmp_path):
    data_dir = _setup_fixture_two(tmp_path)
    stats = compute_diversity_stats(data_dir, room=ROOM)

    assert stats["coverage_file_found"] is True
    assert stats["restart_intervals_skipped"] == 1

    high = stats["coverage_bands"]["high"]
    assert high["key_count"] == 2
    assert high["one_and_done_count"] == 1
    assert high["one_and_done_rate"] == 0.5

    low = stats["coverage_bands"]["low"]
    assert low["key_count"] == 3
    assert low["one_and_done_count"] == 1
    assert low["one_and_done_rate"] == 1 / 3

    mid = stats["coverage_bands"]["mid"]
    assert mid["key_count"] == 0
    assert mid["one_and_done_rate"] is None


def test_coverage_stratification_exclusions(tmp_path):
    data_dir = _setup_fixture_two(tmp_path)
    stats = compute_diversity_stats(data_dir, room=ROOM)

    # KC1 has no parseable ts at all -> excluded from every band.
    assert stats["keys_excluded_no_parseable_ts"] == 1
    # KD1's first-seen hour (hour C) has no coverage data at all -> excluded.
    assert stats["keys_excluded_no_coverage_data"] == 1

    # Both excluded keys are still counted in the OVERALL one-and-done
    # rate (which needs no ts at all), alongside KA1 and KB1.
    assert stats["total_distinct_keys"] == 7
    assert stats["one_and_done_count"] == 4  # KA1, KB1, KC1, KD1
    assert stats["one_and_done_rate"] == 4 / 7


def test_coverage_stratification_report_language(tmp_path):
    data_dir = _setup_fixture_two(tmp_path)
    stats = compute_diversity_stats(data_dir, room=ROOM)
    report = format_report(stats)

    assert "best-captured" in report
    assert "must not be read as contradicting" in report
    for did in ALL_DIDS:
        assert did not in report
