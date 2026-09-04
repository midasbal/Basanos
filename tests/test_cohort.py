"""Cohort persistence (analysis/cohort.py) against a known, synthetic
window layout.

Windows (epoch seconds, BASE = 1_700_000_000):
  window 1 (cohort): [BASE,     BASE+100)
  gap:               [BASE+100, BASE+200)
  window 2 (return): [BASE+200, BASE+300)

Keys, by first-ever ts and later activity:
  K1: first-ever ts = BASE+50  (in window 1), also posts at BASE+250 (in
      window 2) -> in the cohort, and returns.
  K2: first-ever ts = BASE+60  (in window 1), never posts again -> in the
      cohort, does not return.
  K3: first-ever ts = BASE-50  (BEFORE window 1), also posts during
      window 1 (BASE+10) and window 2 (BASE+250) -> the anti-confound
      case: despite posting in both windows, K3 is NOT a new key in
      window 1 and MUST be excluded from the cohort.
  K4: first-ever ts = BASE+150 (in the gap) -> excluded from the cohort.
  K5: first-ever ts = BASE+220 (in window 2 only) -> excluded from the
      cohort (window 2 activity with no window-1 presence is not a
      cohort member at all).

Cohort = {K1, K2} (size 2). Returned = {K1} (K1 posts in window 2, K2
does not). persistence_rate = 1/2 = 0.5, non_return_rate = 0.5.

Uses tests/fixtures/make_fixtures.py's deterministic throwaway-key
approach (FIXTURE_KEY_1/2/3 plus extra labels), the same pattern
tests/test_diversity.py and tests/test_diurnal.py use -- no real did:key
identity involved, and make_fixtures.py itself is not modified.
"""

import json
import os
from datetime import datetime, timezone

import pytest

from make_fixtures import FIXTURE_DID_1, FIXTURE_DID_2, FIXTURE_KEY_1, FIXTURE_KEY_2, _did_key, _fixture_key, _sign

from analysis.cohort import compute_cohort_stats, format_report

ROOM = "lobby"

FIXTURE_KEY_3 = _fixture_key("three")
FIXTURE_DID_3 = _did_key(FIXTURE_KEY_3.public_key().public_bytes_raw())
FIXTURE_KEY_4 = _fixture_key("four")
FIXTURE_DID_4 = _did_key(FIXTURE_KEY_4.public_key().public_bytes_raw())
FIXTURE_KEY_5 = _fixture_key("five")
FIXTURE_DID_5 = _did_key(FIXTURE_KEY_5.public_key().public_bytes_raw())

ALL_DIDS = [FIXTURE_DID_1, FIXTURE_DID_2, FIXTURE_DID_3, FIXTURE_DID_4, FIXTURE_DID_5]

KEYS = {
    1: (FIXTURE_KEY_1, FIXTURE_DID_1),
    2: (FIXTURE_KEY_2, FIXTURE_DID_2),
    3: (FIXTURE_KEY_3, FIXTURE_DID_3),
    4: (FIXTURE_KEY_4, FIXTURE_DID_4),
    5: (FIXTURE_KEY_5, FIXTURE_DID_5),
}

BASE = 1_700_000_000

W1_START = float(BASE)
W1_END = float(BASE + 100)
W2_START = float(BASE + 200)
W2_END = float(BASE + 300)


def _iso(ts_seconds):
    return datetime.fromtimestamp(ts_seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _signed(key_index, seq, text, nonce, ts_seconds):
    key, did = KEYS[key_index]
    ts = _iso(ts_seconds)
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


def _unsigned(seq, nick, text, ts_seconds):
    ts = _iso(ts_seconds)
    return {
        "room": ROOM,
        "seq": seq,
        "ts": ts,
        "from": nick,
        "text": text,
        "nonce": None,
        "sig": None,
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


def _build_message_records():
    records = [
        _signed(1, 1, "k1 first", 9001, BASE + 50),
        _signed(1, 2, "k1 return", 9002, BASE + 250),
        _signed(2, 3, "k2 only", 9003, BASE + 60),
        _signed(3, 4, "k3 pre", 9004, BASE - 50),
        _signed(3, 5, "k3 during w1", 9005, BASE + 10),
        _signed(3, 6, "k3 return", 9006, BASE + 250),
        _signed(4, 7, "k4 gap", 9007, BASE + 150),
        _signed(5, 8, "k5 w2 only", 9008, BASE + 220),
        _unsigned(9, "fixture-nick-anon", "unsigned nicks are excluded", BASE + 55),
    ]
    broken = _signed(2, 10, "broken text", 9999, BASE + 65)
    bad_char = "A" if broken["sig"][0] != "A" else "B"
    broken["sig"] = bad_char + broken["sig"][1:]
    records.append(broken)
    return records


def _build_coverage_records():
    # (captured_at_seconds, captured_total, dropped_total)
    snapshots = [
        (BASE + 10, 0, 0),
        (BASE + 50, 5, 1),      # a -> b: (5, 1) at ts=50, in window 1, excluded from w2
        (BASE + 190, 5, 1),     # b -> c: (0, 0) at ts=190, in the gap, excluded from w2
        (BASE + 250, 23, 3),    # c -> d: (18, 2) at ts=250, IN window 2
        (BASE + 270, 15, 2),    # d -> e: restart (captured_total drops), skipped
        (BASE + 290, 20, 4),    # e -> f: (5, 2) at ts=290, IN window 2
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


def _setup(tmp_path, with_coverage=True):
    data_dir = tmp_path / "data"
    _write_messages(str(data_dir / "rooms" / ROOM / "messages.jsonl"), _build_message_records())
    if with_coverage:
        _write_coverage(str(data_dir / "coverage.jsonl"), _build_coverage_records())
    return str(data_dir)


def _compute(data_dir):
    return compute_cohort_stats(
        data_dir, room=ROOM, w1_start=W1_START, w1_end=W1_END, w2_start=W2_START, w2_end=W2_END
    )


def test_reverify_counts(tmp_path):
    data_dir = _setup(tmp_path)
    stats = _compute(data_dir)

    # 8 fixture messages (K1 x2, K2 x1, K3 x3, K4 x1, K5 x1) + 1 broken =
    # 9 signed messages checked; the unsigned nick is excluded before it
    # is ever counted as "checked".
    assert stats["signed_checked"] == 9
    assert stats["signed_reverified"] == 8
    assert stats["signed_reverify_failed"] == 1


def test_cohort_and_persistence_hand_calculated(tmp_path):
    data_dir = _setup(tmp_path)
    stats = _compute(data_dir)

    # Cohort = keys first-ever seen in window 1 = {K1 (ts=50), K2 (ts=60)}.
    assert stats["cohort_size"] == 2
    # Returned in window 2 = {K1} (posts at ts=250); K2 never posts again.
    assert stats["returned_count"] == 1
    assert stats["non_return_count"] == 1
    assert stats["persistence_rate"] == 0.5
    assert stats["non_return_rate"] == 0.5


def test_pre_window_1_key_is_excluded_from_the_cohort(tmp_path):
    # The anti-confound test: K3 posts during window 1 (ts=10) AND window 2
    # (ts=250), which an "active in each half" measurement would count as
    # persistent. But K3's first-ever appearance is BEFORE window 1
    # (ts=-50), so it is not a new key in window 1 and must not be in the
    # cohort at all. If this test failed, cohort_size would be 3, not 2,
    # and persistence_rate would be wrong (activity-weighted).
    data_dir = _setup(tmp_path)
    stats = _compute(data_dir)

    assert stats["cohort_size"] == 2  # not 3 -- K3 excluded
    # K4 (first seen in the gap) and K5 (first seen in window 2 only) are
    # also excluded from the cohort, for the same underlying reason: their
    # first-ever appearance does not fall in window 1.


def test_window_2_coverage_hand_calculated(tmp_path):
    data_dir = _setup(tmp_path)
    stats = _compute(data_dir)

    assert stats["coverage_file_found"] is True
    # Only intervals whose (later-snapshot) captured_at falls in window 2
    # count: (18, 2) at ts=250 and (5, 2) at ts=290. The window-1 interval
    # (5, 1) at ts=50 and the zero-delta gap interval at ts=190 are
    # excluded from these totals.
    assert stats["w2_captured_total"] == 23
    assert stats["w2_dropped_total"] == 4
    assert stats["w2_coverage_ratio"] == 23 / 27
    # The d -> e interval (captured_total drops from 23 to 15) is a
    # restart and must be skipped, not counted as a drop.
    assert stats["restart_intervals_skipped"] == 1


def test_bad_window_bounds_rejected(tmp_path):
    data_dir = tmp_path / "data"
    os.makedirs(str(data_dir), exist_ok=True)

    # w1_start >= w1_end
    with pytest.raises(ValueError):
        compute_cohort_stats(str(data_dir), room=ROOM, w1_start=100.0, w1_end=100.0, w2_start=200.0, w2_end=300.0)

    # window 1 and window 2 overlap (w1_end > w2_start)
    with pytest.raises(ValueError):
        compute_cohort_stats(str(data_dir), room=ROOM, w1_start=0.0, w1_end=250.0, w2_start=200.0, w2_end=300.0)

    # w2_start >= w2_end
    with pytest.raises(ValueError):
        compute_cohort_stats(str(data_dir), room=ROOM, w1_start=0.0, w1_end=100.0, w2_start=200.0, w2_end=200.0)


def test_zero_width_gap_is_allowed(tmp_path):
    # w1_end == w2_start is a real, valid (zero-width) gap, not an error.
    data_dir = tmp_path / "data"
    os.makedirs(str(data_dir), exist_ok=True)
    stats = compute_cohort_stats(str(data_dir), room=ROOM, w1_start=0.0, w1_end=100.0, w2_start=100.0, w2_end=200.0)
    assert stats["gap_seconds"] == 0.0


def test_no_did_string_anywhere_in_json_output(tmp_path):
    data_dir = _setup(tmp_path)
    stats = _compute(data_dir)
    dumped = json.dumps(stats)

    for did in ALL_DIDS:
        assert did not in dumped


def test_report_contains_required_language_and_no_dids(tmp_path):
    data_dir = _setup(tmp_path)
    stats = _compute(data_dir)
    report = format_report(stats)

    assert "FLOOR" in report
    assert "heartbeat-style posting" in report
    assert "not a verdict about any poster" in report
    assert "bounded to window 2" in report
    assert "checked" in report and "re-verified" in report and "failed" in report
    for did in ALL_DIDS:
        assert did not in report


def test_missing_messages_file_does_not_crash(tmp_path):
    data_dir = tmp_path / "empty_data"
    os.makedirs(str(data_dir), exist_ok=True)
    stats = compute_cohort_stats(
        str(data_dir), room="lobby", w1_start=W1_START, w1_end=W1_END, w2_start=W2_START, w2_end=W2_END
    )
    assert stats["messages_file_found"] is False
    assert stats["signed_checked"] == 0
    report = format_report(stats)
    assert "No messages.jsonl found" in report


def test_missing_coverage_file_does_not_crash_persistence_still_computed(tmp_path):
    data_dir = _setup(tmp_path, with_coverage=False)
    stats = _compute(data_dir)

    assert stats["coverage_file_found"] is False
    # Persistence itself needs no coverage data at all.
    assert stats["cohort_size"] == 2
    assert stats["persistence_rate"] == 0.5
    assert stats["w2_coverage_ratio"] is None

    report = format_report(stats)
    assert "No coverage.jsonl found" in report


def test_invalid_room_is_rejected(tmp_path):
    data_dir = tmp_path / "data"
    os.makedirs(str(data_dir), exist_ok=True)
    with pytest.raises(ValueError):
        compute_cohort_stats(
            str(data_dir), room="../../escaped", w1_start=W1_START, w1_end=W1_END, w2_start=W2_START, w2_end=W2_END
        )
