"""A fixture-signed record re-verifies; a tampered copy fails.

Signed by the reproducible throwaway fixture key from
tests/fixtures/make_fixtures.py -- no real did:key identity involved.
"""

from helpers import load_fixture

from collector.verify import UnsupportedKeyType, is_signed, verify_record


def _fixture_record():
    page = load_fixture("lobby_page.fixture.json")
    m = page["messages"][0]  # seq 100, signed by FIXTURE_KEY_1
    return {
        "room": page["room"],
        "seq": m["seq"],
        "ts": m["ts"],
        "from": m["from"],
        "text": m["text"],
        "nonce": m["nonce"],
        "sig": m["sig"],
    }


def test_fixture_signature_reverifies():
    record = _fixture_record()
    assert is_signed(record)
    assert verify_record(record) is True


def test_tampered_text_fails_verification():
    record = _fixture_record()
    tampered = dict(record, text=record["text"] + " (tampered)")
    assert verify_record(tampered) is False


def test_tampered_nonce_fails_verification():
    record = _fixture_record()
    tampered = dict(record, nonce=record["nonce"] + 1)
    assert verify_record(tampered) is False


def test_tampered_sig_fails_verification():
    record = _fixture_record()
    # flip the first base64url char
    bad_char = "A" if record["sig"][0] != "A" else "B"
    tampered = dict(record, sig=bad_char + record["sig"][1:])
    assert verify_record(tampered) is False


def test_unsigned_nick_is_not_a_did_key():
    page = load_fixture("unsigned_nick_page.fixture.json")
    unsigned = next(m for m in page["messages"] if "sig" not in m)
    record = {"room": page["room"], "from": unsigned["from"]}
    assert is_signed(record) is False
    try:
        verify_record(dict(record, nonce=0, text="", sig=""))
        assert False, "expected UnsupportedKeyType"
    except UnsupportedKeyType:
        pass


def test_all_signed_messages_in_fixture_pages_reverify():
    # Broader sweep: every signed message across the two multi-writer
    # fixture pages should independently re-verify against its own DID
    # (either FIXTURE_DID_1 or FIXTURE_DID_2).
    checked = 0
    for fixture_name in ("lobby_page.fixture.json", "meta_page.fixture.json"):
        page = load_fixture(fixture_name)
        for m in page["messages"]:
            if "sig" not in m:
                continue
            record = {
                "room": page["room"],
                "from": m["from"],
                "text": m["text"],
                "nonce": m["nonce"],
                "sig": m["sig"],
            }
            assert verify_record(record) is True, m
            checked += 1
    # lobby: seq 100,101,102,105,106 signed (5); meta: all 5 signed. Exact
    # and deterministic, since the fixtures are.
    assert checked == 10
