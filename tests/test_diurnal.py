"""Activity curve (analysis/diurnal.py) against a known, synthetic window:
a small set of re-verified signed messages placed in known 60s bins, and a
hand-written coverage.jsonl with cumulative snapshots (including a
restart, i.e. a cumulative counter reset) so estimated-dropped-per-bin is
known exactly.

Layout (bucket_seconds=60, offsets in seconds from BASE_TS):
- bin 0 [0, 60):    messages at offsets 0, 10, 20   -> 3 captured posts
- bin 1 [60, 120):  message at offset 65             -> 1 captured post
- bin 2 [120, 180): messages at offsets 130, 140     -> 2 captured posts

Coverage snapshots for "lobby" (cumulative captured_total/dropped_total):
  A: offset  5  (bin 0) -> captured_total=100, dropped_total=10  (first snapshot, no prior interval)
  B: offset 65  (bin 1) -> captured_total=110, dropped_total=15  (A -> B: +10 captured, +5 dropped)
  C: offset 130 (bin 2) -> captured_total=90,  dropped_total=8   (B -> C: -20 captured, RESTART, skipped)
  D: offset 150 (bin 2) -> captured_total=95,  dropped_total=12  (C -> D: +5 captured, +4 dropped)

Uses tests/fixtures/make_fixtures.py's deterministic throwaway-key
approach (FIXTURE_KEY_1/2/3), the same pattern tests/test_synchrony.py
uses -- no real did:key identity involved, and make_fixtures.py itself is
not modified.
"""

import json
import os
from datetime import datetime, timedelta, timezone

from make_fixtures import FIXTURE_DID_1, FIXTURE_DID_2, FIXTURE_KEY_1, FIXTURE_KEY_2, _did_key, _fixture_key, _sign

from analysis.diurnal import compute_diurnal_stats, format_report

ROOM = "lobby"

FIXTURE_KEY_3 = _fixture_key("three")
FIXTURE_DID_3 = _did_key(FIXTURE_KEY_3.public_key().public_bytes_raw())

ALL_DIDS = [FIXTURE_DID_1, FIXTURE_DID_2, FIXTURE_DID_3]

BASE_TS = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

KEYS = [
    (FIXTURE_KEY_1, FIXTURE_DID_1),
    (FIXTURE_KEY_2, FIXTURE_DID_2),
    (FIXTURE_KEY_3, FIXTURE_DID_3),
]


def _ts_at(offset_seconds):
    return (BASE_TS + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _signed(key, did, seq, text, nonce, offset_seconds):
    return {
        "room": ROOM,
        "seq": seq,
        "ts": _ts_at(offset_seconds),
        "from": did,
        "text": text,
        "nonce": str(nonce),
        "sig": _sign(key, ROOM, str(nonce), text),
        "captured_at": _ts_at(offset_seconds),
        "source": "test",
    }


def _unsigned(seq, nick, text, offset_seconds):
    return {
        "room": ROOM,
        "seq": seq,
        "ts": _ts_at(offset_seconds),
        "from": nick,
        "text": text,
        "nonce": None,
        "sig": None,
        "captured_at": _ts_at(offset_seconds),
        "source": "test",
    }


def _write_messages(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True))
            f.write("\n")


# Offsets (seconds from BASE_TS) for each bin's messages, at 60s buckets.
BIN0_OFFSETS = [0, 10, 20]
BIN1_OFFSETS = [65]
BIN2_OFFSETS = [130, 140]


def _build_message_records():
    records = []
    seq = 0

    def emit(offsets, text_prefix):
        nonlocal seq
        for i, offset in enumerate(offsets):
            seq += 1
            key, did = KEYS[i % len(KEYS)]
            records.append(
                _signed(key, did, seq, f"{text_prefix} {seq}", nonce=9000 + seq, offset_seconds=offset)
            )

    emit(BIN0_OFFSETS, "bin0 post")
    emit(BIN1_OFFSETS, "bin1 post")
    emit(BIN2_OFFSETS, "bin2 post")

    seq += 1
    records.append(_unsigned(seq, "fixture-nick-anon", "unsigned nicks are excluded", offset_seconds=0))

    seq += 1
    key, did = KEYS[0]
    broken = _signed(key, did, seq, "broken message text", nonce=9999, offset_seconds=0)
    broken["sig"] = ("A" if broken["sig"][0] != "A" else "B") + broken["sig"][1:]
    records.append(broken)

    return records


def _build_coverage_records():
    # (offset_seconds, captured_total, dropped_total)
    snapshots = [
        (5, 100, 10),   # A
        (65, 110, 15),  # B: A -> B is +10 captured, +5 dropped (bin 1)
        (130, 90, 8),   # C: B -> C is a restart (-20 captured), skipped entirely
        (150, 95, 12),  # D: C -> D is +5 captured, +4 dropped (bin 2)
    ]
    records = []
    for offset, captured_total, dropped_total in snapshots:
        records.append(
            {
                "room": ROOM,
                "captured_total": captured_total,
                "dropped_total": dropped_total,
                "coverage": None,
                "cursor": None,
                "captured_at": _ts_at(offset),
                "source": "test",
            }
        )
    return records


def _write_coverage(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True))
            f.write("\n")


def _setup(tmp_path, with_coverage=True):
    data_dir = tmp_path / "data"
    messages_path = data_dir / "rooms" / ROOM / "messages.jsonl"
    _write_messages(str(messages_path), _build_message_records())

    if with_coverage:
        coverage_path = data_dir / "coverage.jsonl"
        _write_coverage(str(coverage_path), _build_coverage_records())

    return str(data_dir)


def _bin(stats, index):
    return stats["bins"][index]


def test_reverify_counts_and_exclusions(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_diurnal_stats(data_dir, room=ROOM, bucket_seconds=60.0)

    # 3 + 1 + 2 = 6 shared-window signed messages + 1 broken = 7 signed
    # messages checked; the unsigned nick is excluded before it is ever
    # counted as "checked".
    assert stats["signed_checked"] == 7
    assert stats["signed_reverified"] == 6
    assert stats["signed_reverify_failed"] == 1


def test_captured_posts_per_bin_hand_calculated(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_diurnal_stats(data_dir, room=ROOM, bucket_seconds=60.0)

    # earliest = 0 (bin0's first offset), latest = 140 (bin2's last
    # message offset); bin_count = floor(140/60) + 1 = 2 + 1 = 3.
    assert stats["num_bins"] == 3
    assert _bin(stats, 0)["captured_posts"] == 3  # offsets 0, 10, 20
    assert _bin(stats, 1)["captured_posts"] == 1  # offset 65
    assert _bin(stats, 2)["captured_posts"] == 2  # offsets 130, 140


def test_estimated_dropped_per_bin_from_differenced_coverage(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_diurnal_stats(data_dir, room=ROOM, bucket_seconds=60.0)

    # A -> B (offset 65, bin 1): dropped_total 15 - 10 = 5.
    # B -> C is a restart (captured_total drops from 110 to 90): skipped,
    # contributes nothing to any bin, no negative dropped anywhere.
    # C -> D (offset 150, bin 2): dropped_total 12 - 8 = 4.
    assert _bin(stats, 0)["estimated_dropped"] == 0  # no interval falls in bin 0
    assert _bin(stats, 1)["estimated_dropped"] == 5
    assert _bin(stats, 2)["estimated_dropped"] == 4
    assert stats["restart_intervals_skipped"] == 1
    assert all(b["estimated_dropped"] >= 0 for b in stats["bins"])


def test_per_bin_coverage_ratio_hand_calculated(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_diurnal_stats(data_dir, room=ROOM, bucket_seconds=60.0)

    # Pinned bin: bin 1 has captured_posts=1, estimated_dropped=5, so
    # estimated_throughput=6 and coverage_ratio = 1 / 6 exactly.
    bin1 = _bin(stats, 1)
    assert bin1["estimated_throughput"] == 6
    assert bin1["coverage_ratio"] == 1 / 6

    # bin 0 has no estimated drops at all: coverage_ratio is exactly 1.0.
    assert _bin(stats, 0)["coverage_ratio"] == 1.0

    # bin 2: captured=2, dropped=4, throughput=6, coverage=2/6=1/3.
    assert _bin(stats, 2)["coverage_ratio"] == 1 / 3


def test_aggregate_totals_and_shape(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_diurnal_stats(data_dir, room=ROOM, bucket_seconds=60.0)

    assert stats["total_captured_posts"] == 6  # 3 + 1 + 2
    assert stats["total_estimated_dropped"] == 9  # 0 + 5 + 4
    assert stats["overall_coverage_ratio"] == 6 / 15

    # captured curve shape: max=3 (bin0), min=1 (bin1), ratio=3.0.
    assert stats["max_captured_in_a_bin"] == 3
    assert stats["min_captured_in_a_bin"] == 1
    assert stats["captured_shape_ratio"] == 3.0


def test_no_did_string_anywhere_in_json_output(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_diurnal_stats(data_dir, room=ROOM, bucket_seconds=60.0)
    dumped = json.dumps(stats)

    for b in stats["bins"]:
        assert set(b.keys()) == {
            "bin_index",
            "bin_start_ts",
            "captured_posts",
            "estimated_dropped",
            "estimated_throughput",
            "coverage_ratio",
            "coverage_captured_crosscheck",
        }
    for did in ALL_DIDS:
        assert did not in dumped


def test_report_contains_required_language_and_no_dids(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_diurnal_stats(data_dir, room=ROOM, bucket_seconds=60.0)
    report = format_report(stats)

    assert "FLOOR" in report
    assert "APPROXIMATE" in report
    assert "heartbeat-style posting" in report
    assert "not a verdict about any poster" in report
    assert "diurnal cycle" in report
    assert "checked" in report and "re-verified" in report and "failed" in report
    for did in ALL_DIDS:
        assert did not in report


def test_missing_messages_file_does_not_crash(tmp_path):
    data_dir = tmp_path / "empty_data"
    os.makedirs(str(data_dir), exist_ok=True)
    stats = compute_diurnal_stats(str(data_dir), room="lobby")
    assert stats["messages_file_found"] is False
    assert stats["signed_checked"] == 0
    assert stats["bins"] == []
    report = format_report(stats)
    assert "No messages.jsonl found" in report


def test_missing_coverage_file_does_not_crash(tmp_path):
    data_dir = _setup(tmp_path, with_coverage=False)
    stats = compute_diurnal_stats(data_dir, room=ROOM, bucket_seconds=60.0)

    assert stats["coverage_file_found"] is False
    # Captured curve still computed; every bin's estimated_dropped is 0
    # (no coverage.jsonl to source drops from), not a crash and not a
    # negative or missing value.
    assert stats["num_bins"] == 3
    assert all(b["estimated_dropped"] == 0 for b in stats["bins"])
    assert stats["restart_intervals_skipped"] == 0
    assert _bin(stats, 0)["captured_posts"] == 3

    report = format_report(stats)
    assert "No coverage.jsonl found" in report
