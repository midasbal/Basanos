"""Cross-key duplication rate (analysis/duplication.py) against a known,
synthetic mix: a text signed by two distinct fixture keys (cross-key
duplicate), unique texts, a same-key repeat (heartbeat-shaped, NOT a
cross-key duplicate), an unsigned nick (excluded from the population), and
one message with a deliberately broken signature (excluded, tallied as a
re-verify failure).

Signed by the reproducible throwaway fixture keys from
tests/fixtures/make_fixtures.py -- no real did:key identity involved.
"""

import json
import os

from make_fixtures import FIXTURE_DID_1, FIXTURE_DID_2, FIXTURE_KEY_1, FIXTURE_KEY_2, _sign

from analysis.duplication import compute_duplication_stats, format_report

ROOM = "lobby"


def _signed(key, did, seq, text, nonce):
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
        _signed(FIXTURE_KEY_1, FIXTURE_DID_1, 1, "duplicate text", 1001),
        _signed(FIXTURE_KEY_2, FIXTURE_DID_2, 2, "duplicate text", 1002),  # cross-key dup
        _signed(FIXTURE_KEY_1, FIXTURE_DID_1, 3, "unique to one", 1003),
        _signed(FIXTURE_KEY_2, FIXTURE_DID_2, 4, "unique to two", 1004),
        _signed(FIXTURE_KEY_1, FIXTURE_DID_1, 5, "heartbeat text", 1005),
        _signed(FIXTURE_KEY_1, FIXTURE_DID_1, 6, "heartbeat text", 1006),  # same DID repeat
        _unsigned(7, "fixture-nick-anon", "unsigned nicks are excluded"),
    ]
    broken = _signed(FIXTURE_KEY_1, FIXTURE_DID_1, 8, "broken sig text", 1008)
    bad_char = "A" if broken["sig"][0] != "A" else "B"
    broken["sig"] = bad_char + broken["sig"][1:]
    records.append(broken)
    return records


def _setup(tmp_path):
    data_dir = tmp_path / "data"
    messages_path = data_dir / "rooms" / ROOM / "messages.jsonl"
    _write_messages(str(messages_path), _build_records())

    coverage_state = {ROOM: {"captured_total": 100, "dropped_total": 25}}
    os.makedirs(str(data_dir), exist_ok=True)
    with open(data_dir / "coverage_state.json", "w", encoding="utf-8") as f:
        json.dump(coverage_state, f)

    return str(data_dir)


def test_reverify_counts_and_exclusions(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_duplication_stats(data_dir, room=ROOM)

    # 7 signed records total (unsigned nick excluded up front); 1 fails re-verify.
    assert stats["signed_checked"] == 7
    assert stats["signed_reverified"] == 6
    assert stats["signed_reverify_failed"] == 1


def test_duplication_rate_matches_hand_calculation(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_duplication_stats(data_dir, room=ROOM)

    # Cross-key duplicates: only "duplicate text" (signed by 2 distinct DIDs).
    # "heartbeat text" is signed twice but by the SAME DID -- not cross-key.
    assert stats["cross_key_duplicated_numerator"] == 2
    assert stats["cross_key_duplicated_denominator"] == 6
    assert stats["cross_key_duplication_rate"] == 2 / 6


def test_distinct_dids_and_texts(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_duplication_stats(data_dir, room=ROOM)

    assert stats["distinct_dids"] == 2
    # duplicate text / unique to one / unique to two / heartbeat text
    assert stats["distinct_texts"] == 4


def test_top_duplicated_texts_no_did_and_correct(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_duplication_stats(data_dir, room=ROOM)

    top = stats["top_duplicated_texts"]
    assert top == [{"text": "duplicate text", "distinct_keys": 2}]
    for entry in top:
        assert set(entry.keys()) == {"text", "distinct_keys"}
        assert FIXTURE_DID_1 not in json.dumps(entry)
        assert FIXTURE_DID_2 not in json.dumps(entry)


def test_coverage_surfaced(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_duplication_stats(data_dir, room=ROOM)

    assert stats["coverage_captured_total"] == 100
    assert stats["coverage_dropped_total"] == 25
    assert stats["coverage_ratio"] == 100 / 125


def test_report_contains_required_language_and_no_dids(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_duplication_stats(data_dir, room=ROOM)
    report = format_report(stats)

    assert "at least" in report.lower()
    assert "33.3%" in report
    assert "FLOOR" in report
    assert "heartbeat-style posting" in report
    assert "not a verdict about any poster" in report
    assert "checked" in report and "re-verified" in report and "failed" in report
    assert FIXTURE_DID_1 not in report
    assert FIXTURE_DID_2 not in report


def test_missing_messages_file_does_not_crash(tmp_path):
    data_dir = tmp_path / "empty_data"
    os.makedirs(str(data_dir), exist_ok=True)
    stats = compute_duplication_stats(str(data_dir), room="lobby")
    assert stats["messages_file_found"] is False
    assert stats["signed_checked"] == 0
    report = format_report(stats)
    assert "No messages.jsonl found" in report


def test_non_string_sig_is_a_failed_reverify_not_a_crash(tmp_path):
    # A bare number where sig should be a string. No valid signature is
    # possible or needed here: verify.py's base64 decoding raises a raw
    # TypeError on this before any cryptographic check happens.
    record = {
        "room": ROOM,
        "seq": 1,
        "ts": "2000-01-01T00:00:01.000000Z",
        "from": FIXTURE_DID_1,
        "text": "hello",
        "nonce": "1",
        "sig": 12345,
        "captured_at": "2000-01-01T00:00:01.000000Z",
        "source": "test",
    }
    data_dir = tmp_path / "data"
    _write_messages(str(data_dir / "rooms" / ROOM / "messages.jsonl"), [record])

    stats = compute_duplication_stats(str(data_dir), room=ROOM)  # must not raise

    assert stats["signed_checked"] == 1
    assert stats["signed_reverified"] == 0
    assert stats["signed_reverify_failed"] == 1


def test_non_string_text_is_a_failed_reverify_not_a_crash(tmp_path):
    # A genuinely valid signature (the signer signed the exact bytes
    # verify.py will re-derive) over a text field that is a JSON array,
    # not a string. Proves the guard fires even when re-verification
    # itself would otherwise succeed, before text is ever used as a dict
    # key (which would raise "unhashable type").
    text_value = ["not", "a", "string"]
    nonce = "1"
    sig = _sign(FIXTURE_KEY_1, ROOM, nonce, str(text_value))
    record = {
        "room": ROOM,
        "seq": 1,
        "ts": "2000-01-01T00:00:01.000000Z",
        "from": FIXTURE_DID_1,
        "text": text_value,
        "nonce": nonce,
        "sig": sig,
        "captured_at": "2000-01-01T00:00:01.000000Z",
        "source": "test",
    }
    data_dir = tmp_path / "data"
    _write_messages(str(data_dir / "rooms" / ROOM / "messages.jsonl"), [record])

    stats = compute_duplication_stats(str(data_dir), room=ROOM)  # must not raise

    assert stats["signed_checked"] == 1
    assert stats["signed_reverified"] == 0
    assert stats["signed_reverify_failed"] == 1
    assert stats["distinct_texts"] == 0


def test_non_string_dict_text_is_a_failed_reverify_not_a_crash(tmp_path):
    # Same as above but a JSON object instead of an array -- also
    # unhashable, and confirms the guard is not array-specific.
    text_value = {"a": 1}
    nonce = "1"
    sig = _sign(FIXTURE_KEY_1, ROOM, nonce, str(text_value))
    record = {
        "room": ROOM,
        "seq": 1,
        "ts": "2000-01-01T00:00:01.000000Z",
        "from": FIXTURE_DID_1,
        "text": text_value,
        "nonce": nonce,
        "sig": sig,
        "captured_at": "2000-01-01T00:00:01.000000Z",
        "source": "test",
    }
    data_dir = tmp_path / "data"
    _write_messages(str(data_dir / "rooms" / ROOM / "messages.jsonl"), [record])

    stats = compute_duplication_stats(str(data_dir), room=ROOM)  # must not raise

    assert stats["signed_checked"] == 1
    assert stats["signed_reverified"] == 0
    assert stats["signed_reverify_failed"] == 1


def test_valid_record_result_unchanged_by_the_new_guards(tmp_path):
    # Regression guard: a normal, valid, all-string-field record produces
    # exactly the result it did before this fix.
    data_dir = _setup(tmp_path)
    stats = compute_duplication_stats(data_dir, room=ROOM)

    assert stats["signed_checked"] == 7
    assert stats["signed_reverified"] == 6
    assert stats["signed_reverify_failed"] == 1
    assert stats["cross_key_duplicated_numerator"] == 2
    assert stats["cross_key_duplicated_denominator"] == 6
    assert stats["cross_key_duplication_rate"] == 2 / 6
