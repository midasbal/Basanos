"""Self-audit (analysis/selfaudit.py) against a known, synthetic pair of
files: messages.jsonl with known seqs and signers, and a hand-built
rooms_snapshots.jsonl with published nick_diversity/window/last_seq for
"lobby", using window=5 (not the real 200) so every window is small enough
to hand-verify.

Snapshots (all target room "lobby" unless noted):
  0. a bare JSON array line -- malformed (not a dict), skipped.
  1. last_seq=5,  window=5, published nick_diversity=0.6, zero_response_share=0.02
     -> window seqs [1, 5], all 5 present (K1,K2,K3,K1,K2 -> 3 distinct)
     -> recomputed = 3/5 = 0.6 -> EXACT MATCH (divergence 0.0).
  2. last_seq=10, window=5, published nick_diversity=0.8, zero_response_share=0.05
     -> window seqs [6, 10], all 5 present (K1,K2,K3,K4,K5 -> 5 distinct)
     -> recomputed = 5/5 = 1.0 -> a REAL DIVERGENCE of 0.2 against the
        (deliberately wrong) published 0.8, proving the module detects a
        genuine mismatch, not just confirms matches.
  3. last_seq=20, window=5, published nick_diversity=0.9, zero_response_share=0.10
     -> window seqs [16, 20], only seq 16-19 are present, seq 20 is MISSING
     -> window_coverage = 4/5 = 0.8 < 1.0 -> INCOMPLETE, skipped, never
        compared (the false-divergence guard).
  4. a snapshot for a different room ("meta") -- no lobby entry, ignored.
  5. a lobby entry missing "window" entirely -- malformed, skipped.

Uses tests/fixtures/make_fixtures.py's deterministic throwaway-key
approach (FIXTURE_KEY_1/2/3 plus extra labels), the same pattern
tests/test_diversity.py and tests/test_clustering.py use -- no real
did:key identity involved, and make_fixtures.py itself is not modified.
"""

import json
import os

import pytest

from make_fixtures import FIXTURE_DID_1, FIXTURE_DID_2, FIXTURE_KEY_1, FIXTURE_KEY_2, _did_key, _fixture_key, _sign

from analysis.selfaudit import compute_selfaudit_stats, format_report

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


def _signed(key_index, seq, text, nonce):
    key, did = KEYS[key_index]
    return {
        "room": ROOM,
        "seq": seq,
        "ts": f"2000-01-01T00:{seq:02d}:00.000000Z",
        "from": did,
        "text": text,
        "nonce": str(nonce),
        "sig": _sign(key, ROOM, str(nonce), text),
        "captured_at": f"2000-01-01T00:{seq:02d}:00.000000Z",
        "source": "test",
    }


def _unsigned(seq, nick, text):
    return {
        "room": ROOM,
        "seq": seq,
        "ts": f"2000-01-01T00:{seq:02d}:00.000000Z",
        "from": nick,
        "text": text,
        "nonce": None,
        "sig": None,
        "captured_at": f"2000-01-01T00:{seq:02d}:00.000000Z",
        "source": "test",
    }


def _write_messages(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True))
            f.write("\n")


def _write_snapshots(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False, sort_keys=True))
            f.write("\n")


def _build_message_records():
    records = [
        # snapshot 1's window, seqs 1-5: K1,K2,K3,K1,K2 -> 3 distinct
        _signed(1, 1, "a", 9001),
        _signed(2, 2, "b", 9002),
        _signed(3, 3, "c", 9003),
        _signed(1, 4, "d", 9004),
        _signed(2, 5, "e", 9005),
        # snapshot 2's window, seqs 6-10: K1,K2,K3,K4,K5 -> 5 distinct
        _signed(1, 6, "f", 9006),
        _signed(2, 7, "g", 9007),
        _signed(3, 8, "h", 9008),
        _signed(4, 9, "i", 9009),
        _signed(5, 10, "j", 9010),
        # snapshot 3's window, seqs 16-20: only 16-19 present, 20 missing
        _signed(1, 16, "k", 9016),
        _signed(2, 17, "l", 9017),
        _signed(3, 18, "m", 9018),
        _signed(4, 19, "n", 9019),
        _unsigned(101, "fixture-nick-anon", "unsigned nicks are excluded"),
    ]
    broken = _signed(2, 100, "broken text", 9999)
    bad_char = "A" if broken["sig"][0] != "A" else "B"
    broken["sig"] = bad_char + broken["sig"][1:]
    records.append(broken)
    return records


def _lobby_snapshot(last_seq, window, nick_diversity, zero_response_share):
    return {
        "captured_at": "2000-01-01T00:00:00.000000Z",
        "source": "test",
        "payload": {
            "rooms": [
                {
                    "room": ROOM,
                    "last_seq": last_seq,
                    "window": window,
                    "nick_diversity": nick_diversity,
                    "zero_response_share": zero_response_share,
                }
            ]
        },
    }


def _build_snapshot_lines():
    return [
        [1, 2, 3],  # malformed: not a dict
        _lobby_snapshot(5, 5, 0.6, 0.02),  # exact match
        _lobby_snapshot(10, 5, 0.8, 0.05),  # real divergence (recomputed will be 1.0)
        _lobby_snapshot(20, 5, 0.9, 0.10),  # incomplete window, must be skipped
        {  # a snapshot for a different room, no lobby entry at all
            "captured_at": "2000-01-01T00:00:00.000000Z",
            "source": "test",
            "payload": {"rooms": [{"room": "meta", "last_seq": 5, "window": 5, "nick_diversity": 1.0}]},
        },
        {  # lobby entry missing "window" entirely -- malformed
            "captured_at": "2000-01-01T00:00:00.000000Z",
            "source": "test",
            "payload": {"rooms": [{"room": ROOM, "last_seq": 5, "nick_diversity": 0.5}]},
        },
    ]


def _setup(tmp_path, with_snapshots=True):
    data_dir = tmp_path / "data"
    _write_messages(str(data_dir / "rooms" / ROOM / "messages.jsonl"), _build_message_records())
    if with_snapshots:
        _write_snapshots(str(data_dir / "rooms_snapshots.jsonl"), _build_snapshot_lines())
    return str(data_dir)


def test_reverify_counts(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_selfaudit_stats(data_dir, room=ROOM)

    # 5 + 5 + 4 = 14 fixture messages + 1 broken = 15 signed checked; the
    # unsigned nick is excluded before it is ever counted as "checked".
    assert stats["signed_checked"] == 15
    assert stats["signed_reverified"] == 14
    assert stats["signed_reverify_failed"] == 1


def test_snapshot_counts_and_malformed_entries(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_selfaudit_stats(data_dir, room=ROOM)

    # 5 dict snapshots seen (the leading bare array does not count as
    # "seen" -- it fails the dict check before that counter increments).
    assert stats["total_snapshots_seen"] == 5
    # Of those, 4 have a lobby entry: the 3 valid ones plus the one
    # missing "window" (the room entry was found, just invalid).
    assert stats["snapshots_with_room_entry"] == 4
    # Malformed: the bare array (not a dict) plus the lobby entry missing
    # "window" = 2 total.
    assert stats["snapshot_malformed_entries_skipped"] == 2


def test_exact_match_snapshot_detected(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_selfaudit_stats(data_dir, room=ROOM)
    audit = stats["nick_diversity_audit"]

    # Snapshot 1: window seqs 1-5 = K1,K2,K3,K1,K2 -> 3 distinct / 5 = 0.6,
    # exactly matching the published 0.6.
    assert audit["exact_match_count"] >= 1
    assert audit["divergence_histogram"]["exact_match"] == 1


def test_real_divergence_detected(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_selfaudit_stats(data_dir, room=ROOM)
    audit = stats["nick_diversity_audit"]

    # Snapshot 2: window seqs 6-10 = K1,K2,K3,K4,K5, all distinct ->
    # recomputed 5/5 = 1.0, against a deliberately wrong published 0.8 ->
    # divergence exactly 0.2. This proves the module can find and report
    # a genuine mismatch, not just confirm agreement.
    assert audit["max_absolute_divergence"] == pytest.approx(0.2)
    assert audit["divergence_histogram"][">=0.05"] == 1
    # Two compared snapshots total (1 and 2); snapshot 3 is excluded (see
    # the next test), so the mean is (0.0 + 0.2) / 2.
    assert audit["compared_count"] == 2
    assert audit["mean_absolute_divergence"] == pytest.approx(0.1)


def test_incomplete_window_skipped_not_compared(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_selfaudit_stats(data_dir, room=ROOM)

    # Snapshot 3's window (seqs 16-20) is missing seq 20 entirely:
    # window_coverage = 4/5 = 0.8, below 1.0, so it must be skipped, not
    # compared -- comparing it would use the wrong denominator and could
    # report a divergence that is really just this capture gap.
    assert stats["snapshots_fully_reconstructable"] == 2
    assert stats["snapshots_skipped_incomplete_window"] == 1
    # Only 2 snapshots were ever compared, confirming snapshot 3 never
    # reached the divergence statistics at all.
    assert stats["nick_diversity_audit"]["compared_count"] == 2


def test_zero_response_share_reported_as_unauditable(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_selfaudit_stats(data_dir, room=ROOM)
    zra = stats["zero_response_share_audit"]

    assert zra["auditable"] is False
    assert "reply" in zra["reason"] or "response" in zra["reason"]
    assert "not a claim" in zra["reason"]
    # Context only, from the 3 valid snapshots (0.02, 0.05, 0.10) -- never
    # a recomputed figure, since there is nothing to recompute it from.
    assert zra["snapshot_count"] == 3
    assert zra["published_min"] == pytest.approx(0.02)
    assert zra["published_max"] == pytest.approx(0.10)
    assert zra["published_mean"] == pytest.approx((0.02 + 0.05 + 0.10) / 3)


def test_no_did_string_anywhere_in_json_output(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_selfaudit_stats(data_dir, room=ROOM)
    dumped = json.dumps(stats)

    for did in ALL_DIDS:
        assert did not in dumped
    assert "did:key:" not in dumped


def test_report_contains_required_language_and_no_dids(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_selfaudit_stats(data_dir, room=ROOM)
    report = format_report(stats)

    assert "FLOOR" in report
    assert "not a claim that the published number is wrong" in report
    assert "not an accusation" in report
    assert "checked" in report and "re-verified" in report and "failed" in report
    for did in ALL_DIDS:
        assert did not in report


def test_missing_messages_file_does_not_crash(tmp_path):
    data_dir = tmp_path / "empty_data"
    os.makedirs(str(data_dir), exist_ok=True)
    stats = compute_selfaudit_stats(str(data_dir), room="lobby")
    assert stats["messages_file_found"] is False
    assert stats["signed_checked"] == 0
    report = format_report(stats)
    assert "No messages.jsonl found" in report


def test_missing_snapshots_file_does_not_crash(tmp_path):
    data_dir = _setup(tmp_path, with_snapshots=False)
    stats = compute_selfaudit_stats(data_dir, room=ROOM)

    assert stats["snapshots_file_found"] is False
    assert stats["total_snapshots_seen"] == 0
    assert stats["nick_diversity_audit"]["compared_count"] == 0
    assert stats["zero_response_share_audit"]["auditable"] is False

    report = format_report(stats)
    assert "No rooms_snapshots.jsonl found" in report


def test_invalid_room_is_rejected(tmp_path):
    data_dir = tmp_path / "data"
    os.makedirs(str(data_dir), exist_ok=True)
    with pytest.raises(ValueError):
        compute_selfaudit_stats(str(data_dir), room="../../escaped")
