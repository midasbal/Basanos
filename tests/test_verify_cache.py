"""Proof suite for the shared incremental-verification cache
(collector/verify_cache.py). This touches the verification path every
published Basanos number depends on, so the point of this file is not
just coverage -- it is a direct proof that the cache can never change a
result, only skip redundant work.

Uses tests/fixtures/make_fixtures.py's deterministic throwaway-key
approach (FIXTURE_KEY_1/2, FIXTURE_DID_1/2, _sign), the same pattern every
other test file in this project uses -- no real did:key identity
involved, and make_fixtures.py itself is not modified.

analysis/duplication.py is used as the one representative sibling module
for the integration-level tests (empty-cache transparency, cache-hit
correctness, newly-appended-record pickup): it has the simplest
compute-function shape of the ten modules that were wired to the cache,
so its output is the clearest side-by-side comparison. The cache-safety
properties themselves (hash-mismatch handling, malformed-cache fallback,
cache-file shape) are proven directly against collector/verify_cache.py's
own functions, since those are true of the cache regardless of which
sibling module calls it.
"""

import hashlib
import json
import os

import pytest

from make_fixtures import FIXTURE_DID_1, FIXTURE_DID_2, FIXTURE_KEY_1, FIXTURE_KEY_2, _sign

from analysis.duplication import compute_duplication_stats
from collector.verify import UnsupportedKeyType, build_signing_payload, verify_record
from collector.verify_cache import (
    append_verify_cache_entries,
    cache_path,
    cached_verify,
    load_verify_cache,
)

ROOM = "lobby"


def _signed(seq, text, key=FIXTURE_KEY_1, did=FIXTURE_DID_1, nonce=None):
    nonce = nonce if nonce is not None else 9000000 + seq
    ts = f"2000-01-01T00:00:{seq:02d}.000000Z"
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


def _messages_path(data_dir):
    return os.path.join(data_dir, "rooms", ROOM, "messages.jsonl")


def _build_records():
    return [
        _signed(1, "hello one", FIXTURE_KEY_1, FIXTURE_DID_1),
        _signed(2, "hello one", FIXTURE_KEY_2, FIXTURE_DID_2),  # cross-key duplicate
        _signed(3, "solo message", FIXTURE_KEY_1, FIXTURE_DID_1),
        _signed(4, "another one", FIXTURE_KEY_2, FIXTURE_DID_2),
    ]


def _dump(stats):
    return json.dumps(stats, ensure_ascii=False, sort_keys=True)


# --- transparency: empty/absent cache must be byte-identical to full verify ---


def test_absent_cache_is_byte_identical_to_cache_disabled(tmp_path):
    dir_a = str(tmp_path / "a")
    dir_b = str(tmp_path / "b")
    _write_messages(_messages_path(dir_a), _build_records())
    _write_messages(_messages_path(dir_b), _build_records())

    # dir_a: cache enabled, but no verify_cache.jsonl exists yet -- an
    # absent cache, forcing every lookup to miss.
    stats_absent_cache = compute_duplication_stats(dir_a, room=ROOM)
    # dir_b: cache path bypassed entirely.
    stats_disabled = compute_duplication_stats(dir_b, room=ROOM, use_verify_cache=False)

    assert _dump(stats_absent_cache) == _dump(stats_disabled)


def test_empty_cache_dict_is_byte_identical_to_full_verify_at_the_function_level(tmp_path):
    # Directly at cached_verify's own level: an empty cache dict for every
    # record in a batch must produce exactly what verify_record itself
    # would, for both successes and failures.
    good = _signed(1, "hello", FIXTURE_KEY_1, FIXTURE_DID_1)
    tampered = _signed(2, "hello", FIXTURE_KEY_1, FIXTURE_DID_1)
    bad_char = "A" if tampered["sig"][0] != "A" else "B"
    tampered["sig"] = bad_char + tampered["sig"][1:]

    for record in (good, tampered):
        cache = {}
        new_entries = []
        cached_result = cached_verify(record, cache, new_entries)
        direct_result = verify_record(record)
        assert cached_result == direct_result


# --- cache correctness: hit must match miss, byte for byte ---


def test_cache_hit_run_matches_cache_miss_run_byte_identical(tmp_path):
    data_dir = str(tmp_path / "data")
    _write_messages(_messages_path(data_dir), _build_records())

    stats_first_run = compute_duplication_stats(data_dir, room=ROOM)  # cold cache, all misses
    cache_file = cache_path(data_dir, ROOM)
    assert os.path.exists(cache_file)

    stats_second_run = compute_duplication_stats(data_dir, room=ROOM)  # warm cache, all hits
    assert _dump(stats_first_run) == _dump(stats_second_run)


def test_cached_verify_hit_branch_matches_a_direct_verify_record_call():
    record = _signed(1, "hello", FIXTURE_KEY_1, FIXTURE_DID_1)
    payload = build_signing_payload(record["room"], record["nonce"], record["text"])
    correct_hash = hashlib.sha256(payload).hexdigest()

    cache = {1: correct_hash}  # a genuine prior verification of these exact bytes
    new_entries = []
    hit_result = cached_verify(record, cache, new_entries)

    assert hit_result is True
    assert hit_result == verify_record(record)
    assert new_entries == []  # a hit never re-adds an entry, nothing new to write


# --- hash-mismatch safety: a wrong or stale cache entry can never win ---


def test_hash_mismatch_forces_real_reverification_of_a_genuine_record():
    record = _signed(1, "genuine text", FIXTURE_KEY_1, FIXTURE_DID_1)
    cache = {1: "0" * 64}  # deliberately wrong hash for this seq
    new_entries = []

    result = cached_verify(record, cache, new_entries)

    assert result is True  # still correctly verified for real, the bad entry was ignored
    payload = build_signing_payload(record["room"], record["nonce"], record["text"])
    correct_hash = hashlib.sha256(payload).hexdigest()
    assert new_entries == [{"seq": 1, "hash": correct_hash}]  # corrected on the way out


def test_stale_cache_entry_never_produces_a_false_positive_for_tampered_bytes():
    genuine = _signed(1, "genuine text", FIXTURE_KEY_1, FIXTURE_DID_1)
    genuine_hash = hashlib.sha256(
        build_signing_payload(genuine["room"], genuine["nonce"], genuine["text"])
    ).hexdigest()

    tampered = dict(genuine)
    tampered["text"] = "not the genuine text"  # sig no longer matches this text

    cache = {1: genuine_hash}  # the cache still remembers the OLD bytes' hash
    new_entries = []
    result = cached_verify(tampered, cache, new_entries)

    assert result is False  # the mismatch forced a real re-verify, which correctly fails
    assert new_entries == []  # a failed verify_record is never cached


def test_cache_miss_raises_the_same_exception_types_verify_record_would():
    bad_record = {
        "room": ROOM,
        "seq": 99,
        "from": "did:key:zINVALIDBASE58!!!",
        "text": "x",
        "nonce": "1",
        "sig": "AAAA",
    }
    cache = {}
    new_entries = []
    with pytest.raises(UnsupportedKeyType):
        cached_verify(bad_record, cache, new_entries)
    assert new_entries == []


def test_missing_signing_field_falls_back_to_verify_record_and_raises_the_same_keyerror():
    incomplete_record = {"from": FIXTURE_DID_1, "sig": "x", "seq": 1}  # no room/nonce/text
    cache = {}
    new_entries = []
    with pytest.raises(KeyError):
        cached_verify(incomplete_record, cache, new_entries)


def test_record_without_a_usable_seq_is_verified_but_never_cached():
    record = _signed(1, "hello", FIXTURE_KEY_1, FIXTURE_DID_1)
    del record["seq"]
    cache = {}
    new_entries = []
    result = cached_verify(record, cache, new_entries)
    assert result is True
    assert new_entries == []  # nothing cacheable without an integer seq


# --- corrupt / malformed cache file: falls back to full verification ---


def test_corrupt_cache_file_falls_back_to_full_verification_byte_identical(tmp_path):
    dir_a = str(tmp_path / "a")
    dir_b = str(tmp_path / "b")
    _write_messages(_messages_path(dir_a), _build_records())
    _write_messages(_messages_path(dir_b), _build_records())

    garbage_path = cache_path(dir_a, ROOM)
    os.makedirs(os.path.dirname(garbage_path), exist_ok=True)
    with open(garbage_path, "w", encoding="utf-8") as f:
        f.write("not valid json at all\n")
        f.write('{"seq": "not-an-int", "hash": 12345}\n')  # wrong field types
        f.write('["not", "an", "object"]\n')
        f.write("\n")  # a blank line

    stats_with_garbage_cache = compute_duplication_stats(dir_a, room=ROOM)
    stats_full_verify = compute_duplication_stats(dir_b, room=ROOM, use_verify_cache=False)

    assert _dump(stats_with_garbage_cache) == _dump(stats_full_verify)


def test_load_verify_cache_on_garbage_file_returns_empty_dict(tmp_path):
    data_dir = str(tmp_path / "data")
    path = cache_path(data_dir, ROOM)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not even json\n")
        f.write("null\n")
        f.write("42\n")

    cache = load_verify_cache(data_dir, ROOM)
    assert cache == {}


def test_load_verify_cache_on_absent_file_returns_empty_dict(tmp_path):
    data_dir = str(tmp_path / "data")
    cache = load_verify_cache(data_dir, ROOM)
    assert cache == {}


# --- newly-appended record: verified, cached, then hit next run ---


def test_newly_appended_record_is_verified_and_cached_then_hit_next_run(tmp_path):
    data_dir = str(tmp_path / "data")
    records = _build_records()
    _write_messages(_messages_path(data_dir), records[:2])

    compute_duplication_stats(data_dir, room=ROOM)
    cache_after_first_run = load_verify_cache(data_dir, ROOM)
    assert set(cache_after_first_run.keys()) == {1, 2}

    # simulate the collector appending a new message while the seq-1 and
    # seq-2 entries already sit in the cache from the prior run
    with open(_messages_path(data_dir), "a", encoding="utf-8") as f:
        f.write(json.dumps(records[2], ensure_ascii=False, sort_keys=True))
        f.write("\n")

    compute_duplication_stats(data_dir, room=ROOM)
    cache_after_second_run = load_verify_cache(data_dir, ROOM)

    assert set(cache_after_second_run.keys()) == {1, 2, 3}
    # the pre-existing entries are unchanged, not merely re-derived to the
    # same value by coincidence
    assert cache_after_second_run[1] == cache_after_first_run[1]
    assert cache_after_second_run[2] == cache_after_first_run[2]


# --- cache file shape: seq + hash only, nothing else ---


def test_cache_file_contains_only_seq_and_hash_no_sig_no_text_no_did(tmp_path):
    data_dir = str(tmp_path / "data")
    _write_messages(_messages_path(data_dir), _build_records())

    compute_duplication_stats(data_dir, room=ROOM)

    path = cache_path(data_dir, ROOM)
    with open(path, encoding="utf-8") as f:
        raw_content = f.read()
        lines = [json.loads(line) for line in raw_content.splitlines() if line.strip()]

    assert len(lines) == 4
    for entry in lines:
        assert set(entry.keys()) == {"seq", "hash"}
        assert isinstance(entry["seq"], int)
        assert isinstance(entry["hash"], str)
        assert len(entry["hash"]) == 64
        int(entry["hash"], 16)  # must be valid hex

    assert "did:key:" not in raw_content
    assert FIXTURE_DID_1 not in raw_content
    assert FIXTURE_DID_2 not in raw_content
    for text_value in ("hello one", "solo message", "another one"):
        assert text_value not in raw_content
    assert "sig" not in raw_content


def test_append_verify_cache_entries_deduplicates_by_seq(tmp_path):
    data_dir = str(tmp_path / "data")
    append_verify_cache_entries(
        data_dir,
        ROOM,
        [{"seq": 1, "hash": "a" * 64}, {"seq": 1, "hash": "b" * 64}, {"seq": 2, "hash": "c" * 64}],
    )
    cache = load_verify_cache(data_dir, ROOM)
    assert cache == {1: "b" * 64, 2: "c" * 64}


def test_append_verify_cache_entries_is_a_noop_for_empty_list(tmp_path):
    data_dir = str(tmp_path / "data")
    append_verify_cache_entries(data_dir, ROOM, [])
    assert not os.path.exists(cache_path(data_dir, ROOM))


# --- key safety and style, matching every other module's own discipline ---


def test_no_key_material_referenced_anywhere_in_verify_cache_module():
    import ast
    import inspect

    import collector.verify_cache as verify_cache_module

    source = inspect.getsource(verify_cache_module)
    tree = ast.parse(source)

    identifiers = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.append(node.id)
        elif isinstance(node, ast.arg):
            identifiers.append(node.arg)
        elif isinstance(node, ast.keyword) and node.arg is not None:
            identifiers.append(node.arg)
        elif isinstance(node, ast.Attribute):
            identifiers.append(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            identifiers.append(node.name)

    forbidden_substrings = ("passphrase", "getpass", "subprocess", "privatekey", "private_key", "pem")
    for identifier in identifiers:
        lowered = identifier.lower()
        for token in forbidden_substrings:
            assert token not in lowered, f"forbidden token {token!r} found in identifier {identifier!r}"

    imported_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_names.extend(alias.name for alias in node.names)
    for name in imported_names:
        assert "private" not in name.lower(), f"unexpected private-key-shaped import: {name}"


def test_no_em_dash_in_verify_cache_module_source():
    import inspect

    import collector.verify_cache as verify_cache_module

    source = inspect.getsource(verify_cache_module)
    assert "—" not in source
