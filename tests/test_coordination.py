"""Coordination concentration (analysis/coordination.py) against a known,
synthetic structure: a core set of keys that all sign several shared
templates (in decreasing overlap: T1 signed by 5 keys, T2 by 4, T3 by 3,
T4 by 2 -- deliberately outside a top-3 cut), some keys signing exactly
one shared template, a key with only unique text, one unsigned nick, and
one deliberately broken signature.

Uses tests/fixtures/make_fixtures.py's deterministic throwaway-key
approach (`_fixture_key`/`_did_key`/`_sign`) for extra keys beyond the two
already defined there (FIXTURE_KEY_1/2) -- same reproducible,
zero-value-seed construction, no real did:key identity involved, and
make_fixtures.py itself is not modified.
"""

import json
import os
from itertools import combinations

from make_fixtures import (
    FIXTURE_DID_1,
    FIXTURE_DID_2,
    FIXTURE_KEY_1,
    FIXTURE_KEY_2,
    _did_key,
    _fixture_key,
    _sign,
)

from analysis.coordination import compute_coordination_stats, format_report

ROOM = "lobby"

# Two more throwaway fixture keys, same deterministic construction as
# make_fixtures.py's FIXTURE_KEY_1/2, just with different labels.
FIXTURE_KEY_3 = _fixture_key("three")
FIXTURE_KEY_4 = _fixture_key("four")
FIXTURE_DID_3 = _did_key(FIXTURE_KEY_3.public_key().public_bytes_raw())
FIXTURE_DID_4 = _did_key(FIXTURE_KEY_4.public_key().public_bytes_raw())

# A fifth and sixth key, one that only ever partially joins the core bloc
# and one that never signs a shared template at all.
FIXTURE_KEY_5 = _fixture_key("five")
FIXTURE_KEY_6 = _fixture_key("six")
FIXTURE_DID_5 = _did_key(FIXTURE_KEY_5.public_key().public_bytes_raw())
FIXTURE_DID_6 = _did_key(FIXTURE_KEY_6.public_key().public_bytes_raw())

ALL_DIDS = [FIXTURE_DID_1, FIXTURE_DID_2, FIXTURE_DID_3, FIXTURE_DID_4, FIXTURE_DID_5, FIXTURE_DID_6]


def _signed(key, did, seq, text, nonce):
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


# Templates:
#   T1 "core template one"   -- signed by K1,K2,K3,K4,K5  (5 distinct keys)
#   T2 "core template two"   -- signed by K1,K2,K3,K4      (4 distinct keys)
#   T3 "core template three" -- signed by K1,K2,K3          (3 distinct keys)
#   T4 "core template four"  -- signed by K4,K5             (2 distinct keys, outside top-3)
# Unique (non-shared) texts: K1, K5, and K6 (x2) each sign a text nobody else uses.
# Plus: one unsigned nick, one message with a deliberately broken signature.

KEYS_DIDS = {
    1: (FIXTURE_KEY_1, FIXTURE_DID_1),
    2: (FIXTURE_KEY_2, FIXTURE_DID_2),
    3: (FIXTURE_KEY_3, FIXTURE_DID_3),
    4: (FIXTURE_KEY_4, FIXTURE_DID_4),
    5: (FIXTURE_KEY_5, FIXTURE_DID_5),
    6: (FIXTURE_KEY_6, FIXTURE_DID_6),
}

T1, T2, T3, T4 = "core template one", "core template two", "core template three", "core template four"


def _build_records():
    seq = 0
    records = []

    def emit(key_index, text, nonce):
        nonlocal seq
        seq += 1
        key, did = KEYS_DIDS[key_index]
        records.append(_signed(key, did, seq, text, nonce))

    for k in (1, 2, 3, 4, 5):
        emit(k, T1, 1000 + k)
    for k in (1, 2, 3, 4):
        emit(k, T2, 2000 + k)
    for k in (1, 2, 3):
        emit(k, T3, 3000 + k)
    for k in (4, 5):
        emit(k, T4, 4000 + k)

    emit(1, "unique text one", 5001)
    emit(5, "unique text five", 5005)
    emit(6, "unique text six a", 5006)
    emit(6, "unique text six b", 5007)

    seq += 1
    records.append(_unsigned(seq, "fixture-nick-anon", "unsigned nicks are excluded"))

    seq += 1
    key, did = KEYS_DIDS[2]
    broken = _signed(key, did, seq, "broken template text", 6002)
    broken["sig"] = ("A" if broken["sig"][0] != "A" else "B") + broken["sig"][1:]
    records.append(broken)

    return records


def _setup(tmp_path):
    data_dir = tmp_path / "data"
    messages_path = data_dir / "rooms" / ROOM / "messages.jsonl"
    _write_messages(str(messages_path), _build_records())

    coverage_state = {ROOM: {"captured_total": 200, "dropped_total": 50}}
    os.makedirs(str(data_dir), exist_ok=True)
    with open(data_dir / "coverage_state.json", "w", encoding="utf-8") as f:
        json.dump(coverage_state, f)

    return str(data_dir)


def test_reverify_counts_and_exclusions(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_coordination_stats(data_dir, room=ROOM, top_n=3)

    # 5+4+3+2 shared-template messages + 4 unique messages + 1 broken = 19 signed.
    # Unsigned nick excluded up front (not counted as "checked" at all).
    assert stats["signed_checked"] == 19
    assert stats["signed_reverified"] == 18
    assert stats["signed_reverify_failed"] == 1


def test_coordinated_share_matches_hand_calculation(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_coordination_stats(data_dir, room=ROOM, top_n=3)

    # Shared-template messages: T1(5)+T2(4)+T3(3)+T4(2) = 14, of 18 re-verified.
    assert stats["coordinated_share_messages_numerator"] == 14
    assert stats["coordinated_share_messages_denominator"] == 18
    assert stats["coordinated_share_messages"] == 14 / 18


def test_coordinated_share_by_did_thresholds(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_coordination_stats(data_dir, room=ROOM, top_n=3)

    # Distinct shared templates signed per key: K1=3(T1,T2,T3) K2=3 K3=3
    # K4=3(T1,T2,T4) K5=2(T1,T4) K6=0.
    assert stats["distinct_dids"] == 6
    assert stats["coordinated_share_dids"][">=1"]["count"] == 5  # all but K6
    assert stats["coordinated_share_dids"][">=2"]["count"] == 5  # all but K6
    assert stats["coordinated_share_dids"][">=3"]["count"] == 4  # K1,K2,K3,K4
    assert stats["coordinated_share_dids"][">=1"]["fraction"] == 5 / 6
    assert stats["coordinated_share_dids"][">=3"]["fraction"] == 4 / 6


def test_concentration_top_n(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_coordination_stats(data_dir, room=ROOM, top_n=3)

    # top-3 by distinct-key count are T1(5),T2(4),T3(3) -- unambiguous, no ties.
    assert [e["text"] for e in stats["top_n_templates"]] == [T1, T2, T3]
    assert [e["distinct_keys"] for e in stats["top_n_templates"]] == [5, 4, 3]

    # top-3 message total = 5+4+3=12, of all shared-template messages 5+4+3+2=14.
    assert stats["concentration_top_n_numerator"] == 12
    assert stats["concentration_top_n_denominator"] == 14
    assert stats["concentration_top_n_fraction"] == 12 / 14


def test_membership_curve_and_intersection(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_coordination_stats(data_dir, room=ROOM, top_n=3)

    # Membership over top-3 (T1,T2,T3): K1=3 K2=3 K3=3 K4=2(T1,T2 only) K5=1(T1 only) K6=0.
    # Thresholds are capped at N=3, so only M in {1,2,3} are reported.
    assert stats["membership_curve"] == {"1": 5, "2": 4, "3": 3}

    # Intersection of T1 & T2 & T3's key-sets: {K1,K2,K3}.
    assert stats["intersection_all_top_n_size"] == 3


def test_pairwise_jaccard_top5(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_coordination_stats(data_dir, room=ROOM, top_n=3)

    # Only 4 shared templates exist total, so "top 5" uses all 4: T1..T4.
    keysets = {
        T1: {FIXTURE_DID_1, FIXTURE_DID_2, FIXTURE_DID_3, FIXTURE_DID_4, FIXTURE_DID_5},
        T2: {FIXTURE_DID_1, FIXTURE_DID_2, FIXTURE_DID_3, FIXTURE_DID_4},
        T3: {FIXTURE_DID_1, FIXTURE_DID_2, FIXTURE_DID_3},
        T4: {FIXTURE_DID_4, FIXTURE_DID_5},
    }
    expected = {}
    for a, b in combinations([T1, T2, T3, T4], 2):
        inter = keysets[a] & keysets[b]
        union = keysets[a] | keysets[b]
        expected[frozenset((a, b))] = len(inter) / len(union)

    assert len(stats["pairwise_jaccard_top5"]) == 6
    for entry in stats["pairwise_jaccard_top5"]:
        key = frozenset((entry["text_a"], entry["text_b"]))
        assert entry["jaccard"] == expected[key]


def test_top_n_and_pairwise_contain_no_did(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_coordination_stats(data_dir, room=ROOM, top_n=3)
    dumped = json.dumps(stats)

    for entry in stats["top_n_templates"]:
        assert set(entry.keys()) == {"text", "distinct_keys"}
    for entry in stats["pairwise_jaccard_top5"]:
        assert set(entry.keys()) == {"text_a", "text_b", "jaccard"}
    for did in ALL_DIDS:
        assert did not in dumped


def test_coverage_surfaced(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_coordination_stats(data_dir, room=ROOM, top_n=3)

    assert stats["coverage_captured_total"] == 200
    assert stats["coverage_dropped_total"] == 50
    assert stats["coverage_ratio"] == 200 / 250


def test_report_contains_required_language_and_no_dids(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_coordination_stats(data_dir, room=ROOM, top_n=3)
    report = format_report(stats)

    assert "at least" in report.lower()
    assert "coordinated bloc" in report
    assert "behavioral linkage" in report
    assert "byte-identical text shared across keys" in report
    assert "not a definitive operator census" in report
    assert "FLOOR" in report
    assert "heartbeat-style posting" in report
    assert "not a verdict about any poster" in report
    assert "checked" in report and "re-verified" in report and "failed" in report
    for did in ALL_DIDS:
        assert did not in report


def test_missing_messages_file_does_not_crash(tmp_path):
    data_dir = tmp_path / "empty_data"
    os.makedirs(str(data_dir), exist_ok=True)
    stats = compute_coordination_stats(str(data_dir), room="lobby")
    assert stats["messages_file_found"] is False
    assert stats["signed_checked"] == 0
    report = format_report(stats)
    assert "No messages.jsonl found" in report


def test_top_n_ranks_by_distinct_keys_not_message_count(tmp_path):
    # "rank template A": 3 distinct keys (K1,K2,K3), 3 messages, one each.
    # "rank template B": 2 distinct keys (K1,K2), 5 messages -- K1 signs it
    # four times. A has more distinct keys (3 > 2) but fewer messages
    # (3 < 5) than B, so a message-count ranking would return [B, A] and
    # fail the assert below; only distinct-key ranking gives [A, B].
    text_a = "rank template A"
    text_b = "rank template B"

    records = [
        _signed(FIXTURE_KEY_1, FIXTURE_DID_1, 1, text_a, 9001),
        _signed(FIXTURE_KEY_2, FIXTURE_DID_2, 2, text_a, 9002),
        _signed(FIXTURE_KEY_3, FIXTURE_DID_3, 3, text_a, 9003),
        _signed(FIXTURE_KEY_1, FIXTURE_DID_1, 4, text_b, 9004),
        _signed(FIXTURE_KEY_1, FIXTURE_DID_1, 5, text_b, 9005),
        _signed(FIXTURE_KEY_1, FIXTURE_DID_1, 6, text_b, 9006),
        _signed(FIXTURE_KEY_1, FIXTURE_DID_1, 7, text_b, 9007),
        _signed(FIXTURE_KEY_2, FIXTURE_DID_2, 8, text_b, 9008),
    ]

    data_dir = tmp_path / "data"
    messages_path = data_dir / "rooms" / ROOM / "messages.jsonl"
    _write_messages(str(messages_path), records)

    stats = compute_coordination_stats(str(data_dir), room=ROOM, top_n=2)

    assert [e["text"] for e in stats["top_n_templates"]] == [text_a, text_b]
    assert [e["distinct_keys"] for e in stats["top_n_templates"]] == [3, 2]
