"""Nonce fingerprint (analysis/nonce.py) against a known, synthetic mix:
a room whose baseline traffic is mostly 13-digit (ms-epoch) nonces, with
one shared template ("toolkit text") whose keys use 19-digit (ns-epoch)
nonces exclusively, one shared template ("matching text") whose own
nonce-length mix closely tracks the room's overall mix, a filler message
with an odd (21-digit) nonce length landing in "other", one message with
a missing (None) nonce (unusable, excluded from every band stat, not a
crash), an unsigned nick, and one deliberately broken signature.

Uses tests/fixtures/make_fixtures.py's deterministic throwaway-key
approach (FIXTURE_KEY_1/2/3), the same pattern tests/test_coordination.py
and tests/test_synchrony.py use -- no real did:key identity involved, and
make_fixtures.py itself is not modified.
"""

import json
import os

import pytest

from make_fixtures import FIXTURE_DID_1, FIXTURE_DID_2, FIXTURE_KEY_1, FIXTURE_KEY_2, _did_key, _fixture_key, _sign

from analysis.nonce import compute_nonce_stats, format_report

ROOM = "lobby"

FIXTURE_KEY_3 = _fixture_key("three")
FIXTURE_DID_3 = _did_key(FIXTURE_KEY_3.public_key().public_bytes_raw())

ALL_DIDS = [FIXTURE_DID_1, FIXTURE_DID_2, FIXTURE_DID_3]

KEYS = {
    1: (FIXTURE_KEY_1, FIXTURE_DID_1),
    2: (FIXTURE_KEY_2, FIXTURE_DID_2),
    3: (FIXTURE_KEY_3, FIXTURE_DID_3),
}

# Nonce constants named by their digit length, the only thing this module
# ever looks at. Reusing one literal per length is fine: multiple messages
# sharing the same nonce string is harmless here (a message's identity for
# signing purposes is room|nonce|text together, and text differs, or the
# signer differs, in every case below).
N13 = "1" * 13
N19 = "1" * 19
N_OTHER = "1" * 21  # 21 digits: outside 13/16/19, lands in "other"

TOOLKIT_TEXT = "toolkit text"
MATCHING_TEXT = "matching text"


def _signed(key_index, seq, text, nonce):
    key, did = KEYS[key_index]
    return {
        "room": ROOM,
        "seq": seq,
        "ts": f"2000-01-01T00:00:{seq:02d}.000000Z",
        "from": did,
        "text": text,
        "nonce": str(nonce),
        "sig": _sign(key, ROOM, str(nonce), text),
        "captured_at": f"2000-01-01T00:00:{seq:02d}.000000Z",
        "source": "test",
    }


def _signed_with_none_nonce(key_index, seq, text):
    # build_signing_payload interpolates nonce with an f-string, so a
    # record with a real None nonce is signed over the literal "None" --
    # this constructs a genuinely valid signature for exactly that case,
    # not a shortcut, matching what verify_record will independently
    # recompute.
    key, did = KEYS[key_index]
    return {
        "room": ROOM,
        "seq": seq,
        "ts": f"2000-01-01T00:00:{seq:02d}.000000Z",
        "from": did,
        "text": text,
        "nonce": None,
        "sig": _sign(key, ROOM, str(None), text),
        "captured_at": f"2000-01-01T00:00:{seq:02d}.000000Z",
        "source": "test",
    }


def _unsigned(seq, nick, text):
    return {
        "room": ROOM,
        "seq": seq,
        "ts": f"2000-01-01T00:00:{seq:02d}.000000Z",
        "from": nick,
        "text": text,
        "nonce": None,
        "sig": None,
        "captured_at": f"2000-01-01T00:00:{seq:02d}.000000Z",
        "source": "test",
    }


def _write_messages(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True))
            f.write("\n")


def _build_records():
    records = [
        _signed(1, 1, "filler one", N13),
        _signed(2, 2, "filler two", N13),
        _signed(3, 3, "filler three", N13),
        _signed(2, 4, "filler four odd length", N_OTHER),
        _signed_with_none_nonce(3, 5, "filler five unusable"),
        _signed(1, 6, TOOLKIT_TEXT, N19),
        _signed(2, 7, TOOLKIT_TEXT, N19),
        _signed(1, 8, MATCHING_TEXT, N13),
        _signed(3, 9, MATCHING_TEXT, N13),
        _signed(1, 10, MATCHING_TEXT, N19),
        _unsigned(12, "fixture-nick-anon", "unsigned nicks are excluded"),
    ]
    broken = _signed(2, 11, "broken text", N13)
    bad_char = "A" if broken["sig"][0] != "A" else "B"
    broken["sig"] = bad_char + broken["sig"][1:]
    records.append(broken)
    return records


def _setup(tmp_path):
    data_dir = tmp_path / "data"
    messages_path = data_dir / "rooms" / ROOM / "messages.jsonl"
    _write_messages(str(messages_path), _build_records())

    coverage_state = {ROOM: {"captured_total": 90, "dropped_total": 10}}
    os.makedirs(str(data_dir), exist_ok=True)
    with open(data_dir / "coverage_state.json", "w", encoding="utf-8") as f:
        json.dump(coverage_state, f)

    return str(data_dir)


def _entry_for(stats, text):
    return next(e for e in stats["templates"] if e["text"] == text)


def test_reverify_counts_and_unusable_tally(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_nonce_stats(data_dir, room=ROOM, top_n=2)

    # 11 signed messages (4 filler + 1 unusable-nonce filler + 2 toolkit +
    # 3 matching + 1 broken); the unsigned nick is excluded before it is
    # ever counted as "checked".
    assert stats["signed_checked"] == 11
    assert stats["signed_reverified"] == 10
    assert stats["signed_reverify_failed"] == 1
    assert stats["unusable_nonces"] == 1


def test_room_wide_band_distribution_hand_calculated(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_nonce_stats(data_dir, room=ROOM, top_n=2)

    # Usable nonces (10 re-verified minus 1 unusable = 9), by length:
    # 13-digit: filler one/two/three (3) + matching's two 13-digit posts (2) = 5
    # 19-digit: toolkit's two posts (2) + matching's one 19-digit post (1) = 3
    # 21-digit (other): filler four (1)
    # total = 5 + 3 + 1 = 9
    assert stats["room_band_counts"] == {"13": 5, "16": 0, "19": 3, "other": 1}
    fractions = stats["room_band_fractions"]
    assert fractions["13"] == 5 / 9
    assert fractions["16"] == 0
    assert fractions["19"] == 3 / 9
    assert fractions["other"] == 1 / 9

    # The tail: the one odd (21-digit) nonce, not folded silently into a
    # main band and not a crash.
    assert stats["room_other_length_breakdown"] == {"21": 1}


def test_toolkit_template_diverges_strongly_from_the_room(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_nonce_stats(data_dir, room=ROOM, top_n=2)
    toolkit = _entry_for(stats, TOOLKIT_TEXT)

    # toolkit's own bands: both its posts are 19-digit -> {19: 1.0}.
    assert toolkit["band_counts"] == {"13": 0, "16": 0, "19": 2, "other": 0}
    assert toolkit["band_fractions"] == {"13": 0.0, "16": 0.0, "19": 1.0, "other": 0.0}

    # divergence vs whole room: 0.5 * (|0-5/9| + |0-0| + |1-3/9| + |0-1/9|)
    #                         = 0.5 * (5/9 + 0 + 6/9 + 1/9) = 0.5 * 12/9 = 2/3.
    assert toolkit["divergence_vs_room"] == pytest.approx(2 / 3)

    # room-minus-self: remove toolkit's own 2 posts (both 19-digit) from the
    # room's raw length counts (13:5, 19:3, 21:1) -> (13:5, 19:1, 21:1),
    # total 7. Band fractions: 13=5/7, 19=1/7, other=1/7.
    # divergence: 0.5 * (|0-5/7| + |0-0| + |1-1/7| + |0-1/7|)
    #           = 0.5 * (5/7 + 0 + 6/7 + 1/7) = 0.5 * 12/7 = 6/7.
    assert toolkit["divergence_vs_room_minus_self"] == pytest.approx(6 / 7)


def test_matching_template_reads_near_zero(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_nonce_stats(data_dir, room=ROOM, top_n=2)
    matching = _entry_for(stats, MATCHING_TEXT)

    # matching's own bands: 2 of 3 posts are 13-digit, 1 is 19-digit.
    assert matching["band_counts"] == {"13": 2, "16": 0, "19": 1, "other": 0}
    assert matching["band_fractions"]["13"] == 2 / 3
    assert matching["band_fractions"]["19"] == 1 / 3

    # divergence vs whole room: 0.5 * (|2/3-5/9| + |0-0| + |1/3-3/9| + |0-1/9|)
    #                         = 0.5 * (1/9 + 0 + 0 + 1/9) = 0.5 * 2/9 = 1/9.
    assert matching["divergence_vs_room"] == pytest.approx(1 / 9)

    # Near zero relative to toolkit's strongly diverging 2/3 and 6/7.
    assert matching["divergence_vs_room"] < 0.2
    assert matching["divergence_vs_room"] < toolkit_divergence_for_comparison(stats)


def toolkit_divergence_for_comparison(stats):
    return _entry_for(stats, TOOLKIT_TEXT)["divergence_vs_room_minus_self"]


def test_minus_self_degenerate_case_reports_none(tmp_path):
    # A separate, minimal room where a single shared template accounts for
    # every usable nonce: room-minus-self has nothing left to compare
    # against, so divergence_vs_room_minus_self must be None, not a
    # misleading 0 or a crash.
    records = [
        _signed(1, 1, "only text", N13),
        _signed(2, 2, "only text", N13),
    ]
    data_dir = tmp_path / "data"
    _write_messages(str(data_dir / "rooms" / ROOM / "messages.jsonl"), records)

    stats = compute_nonce_stats(str(data_dir), room=ROOM, top_n=2)
    only = _entry_for(stats, "only text")

    assert stats["room_band_counts"] == {"13": 2, "16": 0, "19": 0, "other": 0}
    assert only["band_counts"] == {"13": 2, "16": 0, "19": 0, "other": 0}
    # own distribution equals the room's exactly -> TVD is exactly 0.
    assert only["divergence_vs_room"] == 0.0
    assert only["divergence_vs_room_minus_self"] is None


def test_no_did_string_anywhere_in_json_output(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_nonce_stats(data_dir, room=ROOM, top_n=2)
    dumped = json.dumps(stats)

    for entry in stats["templates"]:
        assert set(entry.keys()) == {
            "text",
            "distinct_keys",
            "usable_nonce_count",
            "band_counts",
            "band_fractions",
            "divergence_vs_room",
            "divergence_vs_room_minus_self",
        }
    for did in ALL_DIDS:
        assert did not in dumped


def test_coverage_surfaced(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_nonce_stats(data_dir, room=ROOM, top_n=2)

    assert stats["coverage_captured_total"] == 90
    assert stats["coverage_dropped_total"] == 10
    assert stats["coverage_ratio"] == 90 / 100


def test_report_contains_required_language_and_no_dids(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_nonce_stats(data_dir, room=ROOM, top_n=2)
    report = format_report(stats)

    assert "FLOOR" in report
    assert "heartbeat-style posting" in report
    assert "not a verdict about any poster" in report
    assert "room-minus-self" in report
    assert "ms-epoch" in report and "ns-epoch" in report
    assert "checked" in report and "re-verified" in report and "failed" in report
    for did in ALL_DIDS:
        assert did not in report


def test_missing_messages_file_does_not_crash(tmp_path):
    data_dir = tmp_path / "empty_data"
    os.makedirs(str(data_dir), exist_ok=True)
    stats = compute_nonce_stats(str(data_dir), room="lobby")
    assert stats["messages_file_found"] is False
    assert stats["signed_checked"] == 0
    report = format_report(stats)
    assert "No messages.jsonl found" in report


def test_invalid_room_is_rejected(tmp_path):
    data_dir = tmp_path / "data"
    os.makedirs(str(data_dir), exist_ok=True)
    with pytest.raises(ValueError):
        compute_nonce_stats(str(data_dir), room="../../escaped")
