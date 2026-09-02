"""A fixture-signed record re-verifies; a tampered copy fails.

Signed by the reproducible throwaway fixture key from
tests/fixtures/make_fixtures.py -- no real did:key identity involved.
"""

import pytest

from helpers import load_fixture

from collector.verify import MalformedRecord, UnsupportedKeyType, is_signed, verify_record


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
    with pytest.raises(UnsupportedKeyType):
        verify_record(dict(record, nonce=0, text="", sig=""))


def test_malformed_did_key_raises_unsupported_key_type_not_a_raw_value_error():
    # Regression: a did:key with non-base58 characters after the prefix
    # used to raise a raw ValueError ("substring not found") from
    # b58decode's alphabet lookup, not the module's own exception.
    record = {
        "room": "lobby",
        "from": "did:key:z!!!not-base58!!!",
        "text": "x",
        "nonce": 1,
        "sig": "",
    }
    with pytest.raises(UnsupportedKeyType):
        verify_record(record)


def test_malformed_did_key_wrong_length_raises_unsupported_key_type():
    # A syntactically valid base58 string that decodes to the right
    # multicodec prefix but the wrong key length used to raise a raw
    # ValueError from cryptography's from_public_bytes.
    from collector.verify import _B58_ALPHABET, _ED25519_MULTICODEC_PREFIX

    def b58encode(data):
        num = int.from_bytes(data, "big")
        encoded = ""
        while num > 0:
            num, rem = divmod(num, 58)
            encoded = chr(_B58_ALPHABET[rem]) + encoded
        return encoded or "1"

    too_short = b58encode(_ED25519_MULTICODEC_PREFIX + b"\x01\x02\x03")  # not 32 bytes
    record = {
        "room": "lobby",
        "from": f"did:key:z{too_short}",
        "text": "x",
        "nonce": 1,
        "sig": "",
    }
    with pytest.raises(UnsupportedKeyType):
        verify_record(record)


def test_malformed_sig_raises_malformed_record_not_a_raw_binascii_error():
    # Regression: a non-base64url sig used to raise a raw binascii.Error
    # from base64.urlsafe_b64decode, not a module-level exception.
    record = _fixture_record()
    tampered = dict(record, sig="not valid base64 !!! @@@")
    with pytest.raises(MalformedRecord):
        verify_record(tampered)


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
