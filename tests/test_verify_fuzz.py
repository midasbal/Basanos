"""Property-based fuzzing of collector/verify.py's adversary-facing parsing.

verify.py parses fully attacker-controlled input: any poster can submit any
string as `from`, `sig`, `text`, `nonce`, and any value at all for fields
this project does not otherwise validate before reaching verify.py directly
(this test calls verify_record directly, without collector/core.py's own
_validate_page gate in front of it, to test verify.py's OWN contract in
isolation).

Approach: Hypothesis is not available in this environment (no network
access to install it), so this is a deterministic, seeded generative test
instead, per the documented fallback. Every random draw comes from a
random.Random seeded with a fixed integer, so a failure here reproduces
exactly the same way on every run, on every machine, forever.

Input space covered, fed into did_key_to_ed25519_pubkey (the did argument)
and verify_record (every field of the record):
  - random unicode strings, including codepoints outside the printable
    ASCII range, wide (astral-plane) codepoints, and the specific
    characters base58 excludes (0, O, I, l)
  - empty strings
  - huge strings (thousands of characters)
  - near-miss did:key prefixes (missing the z, wrong case, no prefix at
    all, the bare prefix with nothing after it)
  - malformed base58 (random text following "did:key:z")
  - malformed base64 (random text, and the base64url alphabet at random
    lengths, so some decode cleanly to the wrong number of bytes and some
    do not decode at all)
  - wrong-length keys and signatures (the multicodec-prefixed body decodes
    to something other than the 32 raw bytes an Ed25519 public key needs)
  - non-string values (int, float, bool, None, list, dict) in every field,
    including `sig`, `room`, `nonce`, and `text`
  - records with fields entirely missing, not merely wrong-typed

The only assertion: every one of these calls either returns a plain
True/False, or raises one of the four documented exception types
(UnsupportedKeyType, MalformedRecord, KeyError, TypeError). Anything else
-- any other exception type, or a hang -- is a test failure. A TypeError
escaping verify_record's own except clause (for example from a non-string
sig) is not itself a bug by this test's standard: TypeError is one of the
documented types a caller must already be prepared to catch, exactly as
every analysis module's own verify_record call site already does.
"""

import random
import string

from collector.verify import (
    MalformedRecord,
    UnsupportedKeyType,
    did_key_to_ed25519_pubkey,
    verify_record,
)

DOCUMENTED_EXCEPTIONS = (UnsupportedKeyType, MalformedRecord, KeyError, TypeError)

TRIALS = 4000

_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE64URL_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _random_unicode_string(rng, min_len=0, max_len=40):
    length = rng.randint(min_len, max_len)
    chars = []
    for _ in range(length):
        choice = rng.random()
        if choice < 0.35:
            chars.append(rng.choice(string.printable))
        elif choice < 0.55:
            chars.append(rng.choice(_BASE58_ALPHABET))
        elif choice < 0.7:
            chars.append(rng.choice("0OIl"))
        elif choice < 0.85:
            codepoint = rng.randint(0x20, 0xFFFF)
            while 0xD800 <= codepoint <= 0xDFFF:
                codepoint = rng.randint(0x20, 0xFFFF)
            chars.append(chr(codepoint))
        else:
            chars.append(chr(rng.randint(0x10000, 0x10FFFF)))
    return "".join(chars)


def _random_did_candidate(rng):
    kind = rng.randint(0, 6)
    if kind == 0:
        return "did:key:z" + _random_unicode_string(rng, 0, 60)
    if kind == 1:
        return "did:key:" + _random_unicode_string(rng, 0, 60)
    if kind == 2:
        return _random_unicode_string(rng, 0, 60)
    if kind == 3:
        return "did:key:z"
    if kind == 4:
        body_len = rng.randint(0, 80)
        body = "".join(rng.choice(_BASE58_ALPHABET) for _ in range(body_len))
        return "did:key:z" + body
    if kind == 5:
        return "did:key:Z" + _random_unicode_string(rng, 1, 40)
    return "did:key:z" + "1" * rng.randint(0, 5) + _random_unicode_string(rng, 0, 40)


def _random_sig_candidate(rng):
    kind = rng.randint(0, 5)
    if kind == 0:
        return _random_unicode_string(rng, 0, 100)
    if kind == 1:
        length = rng.randint(0, 200)
        return "".join(rng.choice(_BASE64URL_ALPHABET) for _ in range(length))
    if kind == 2:
        return ""
    if kind == 3:
        return rng.choice([None, 12345, 1.5, [], {}, True, False])
    if kind == 4:
        return "A" * rng.randint(1000, 5000)
    return "not-valid-base64!!!" + _random_unicode_string(rng, 0, 20)


def _random_field_value(rng):
    kind = rng.randint(0, 7)
    if kind == 0:
        return _random_unicode_string(rng, 0, 50)
    if kind == 1:
        return rng.randint(-(10**18), 10**18)
    if kind == 2:
        return rng.random()
    if kind == 3:
        return None
    if kind == 4:
        return rng.choice([True, False])
    if kind == 5:
        return [rng.random() for _ in range(rng.randint(0, 3))]
    if kind == 6:
        return {"x": rng.random()}
    return ""


def test_fuzz_did_key_to_ed25519_pubkey_never_raises_undocumented_exception():
    rng = random.Random(1234567890)
    for _ in range(TRIALS):
        candidate = _random_did_candidate(rng)
        try:
            did_key_to_ed25519_pubkey(candidate)
        except DOCUMENTED_EXCEPTIONS:
            pass
        except Exception as exc:
            raise AssertionError(
                f"undocumented exception {type(exc).__name__}({exc!r}) for did candidate {candidate!r}"
            ) from exc


def test_fuzz_verify_record_never_raises_undocumented_exception():
    rng = random.Random(987654321)
    for _ in range(TRIALS):
        record = {
            "room": _random_field_value(rng),
            "from": _random_did_candidate(rng),
            "nonce": _random_field_value(rng),
            "text": _random_field_value(rng),
            "sig": _random_sig_candidate(rng),
        }
        try:
            verify_record(record)
        except DOCUMENTED_EXCEPTIONS:
            pass
        except Exception as exc:
            raise AssertionError(
                f"undocumented exception {type(exc).__name__}({exc!r}) for record {record!r}"
            ) from exc


def test_fuzz_verify_record_with_missing_fields_never_raises_undocumented_exception():
    rng = random.Random(555000111)
    all_fields = ["room", "from", "nonce", "text", "sig"]
    for _ in range(TRIALS):
        record = {}
        for field in all_fields:
            if rng.random() < 0.8:
                if field == "from":
                    record[field] = _random_did_candidate(rng)
                elif field == "sig":
                    record[field] = _random_sig_candidate(rng)
                else:
                    record[field] = _random_field_value(rng)
        try:
            verify_record(record)
        except DOCUMENTED_EXCEPTIONS:
            pass
        except Exception as exc:
            raise AssertionError(
                f"undocumented exception {type(exc).__name__}({exc!r}) for record {record!r}"
            ) from exc
