"""Optional data-quality check: re-verify a stored message's signature.

Signed writers post as `did:key:<multibase-ed25519-pubkey>`. The signed
payload is `<room>|<nonce>|<text>` (utf-8), the signature is Ed25519 over
that payload, base64url-encoded without padding. This module only reads
already-collected records; it never signs or posts anything.
"""

import base64

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_ED25519_MULTICODEC_PREFIX = bytes((0xED, 0x01))


class UnsupportedKeyType(Exception):
    """The did:key isn't an Ed25519 key we know how to verify."""


def b58decode(s):
    num = 0
    for c in s.encode("ascii"):
        num = num * 58 + _B58_ALPHABET.index(c)
    combined = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    n_pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * n_pad + combined


def did_key_to_ed25519_pubkey(did):
    if not did.startswith("did:key:z"):
        raise UnsupportedKeyType(f"not a did:key: {did!r}")
    multibase = did[len("did:key:"):]
    raw = b58decode(multibase[1:])  # drop the 'z' (base58btc multibase prefix)
    if raw[:2] != _ED25519_MULTICODEC_PREFIX:
        raise UnsupportedKeyType(f"unsupported multicodec prefix in {did!r}: {raw[:2].hex()}")
    return Ed25519PublicKey.from_public_bytes(raw[2:])


def build_signing_payload(room, nonce, text):
    return f"{room}|{nonce}|{text}".encode("utf-8")


def verify_record(record):
    """Re-verify one stored message record.

    `record` needs room, from, nonce, text, sig. Returns True/False.
    Raises UnsupportedKeyType if `from` isn't a did:key we can check
    (e.g. a plain unsigned nick) -- callers should treat that as
    "not verifiable", not "invalid".
    """
    did = record["from"]
    pub = did_key_to_ed25519_pubkey(did)
    payload = build_signing_payload(record["room"], record["nonce"], record["text"])
    sig_bytes = base64.urlsafe_b64decode(record["sig"] + "=" * (-len(record["sig"]) % 4))
    try:
        pub.verify(sig_bytes, payload)
        return True
    except InvalidSignature:
        return False


def is_signed(record):
    return isinstance(record.get("from"), str) and record["from"].startswith("did:key:")
