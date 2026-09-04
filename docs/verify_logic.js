/*
Basanos in-browser signature verifier: pure verification logic.

This file mirrors collector/verify.py exactly, function for function, so
the in-browser check and the Python re-verification path make the same
decision on the same record. It is copied verbatim into the inline
<script> block of docs/verify.html (see the BEGIN/END markers in that
file) rather than loaded as a separate file, so the page has zero
external requests and works from file:// with no server. This file also
runs standalone under Node for the automated cross-check against
collector/verify.py (see tests/test_verify_html.py).

No em dashes anywhere in this file, matching the rest of the project.

Correspondence with collector/verify.py, read that file first:

  _B58_ALPHABET               -> B58_ALPHABET
  _ED25519_MULTICODEC_PREFIX  -> ED25519_MULTICODEC_PREFIX
  b58decode(s)                -> b58decode(s)
  did_key_to_ed25519_pubkey    -> didKeyToEd25519PublicKeyBytes
  build_signing_payload        -> buildSigningPayload
  verify_record                -> verifyRecord (async, WebCrypto has no
                                   synchronous verify)
  is_signed                    -> isSigned

Only the two decoding steps WebCrypto does not provide (base58btc
multibase decoding for the did:key, and base64url decoding for the
signature) are hand-written here. The actual Ed25519 check itself is
crypto.subtle.verify, the browser's own native implementation, never a
bundled library.
*/

var Basanos = (function () {
  "use strict";

  // Same alphabet, same order, as collector/verify.py's _B58_ALPHABET.
  var B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";

  // Same two bytes as collector/verify.py's _ED25519_MULTICODEC_PREFIX,
  // bytes((0xED, 0x01)).
  var ED25519_MULTICODEC_PREFIX = [0xed, 0x01];

  function VerifyError(kind, message) {
    this.kind = kind; // "unsupported_key" or "malformed_record", matching
    // collector/verify.py's UnsupportedKeyType and MalformedRecord
    this.message = message;
  }

  // Mirrors collector/verify.py's b58decode(s) exactly: same alphabet,
  // same big-integer accumulation (num = num * 58 + index(char)), same
  // leading-'1'-means-leading-zero-byte rule. Uses BigInt because the
  // accumulated integer is far larger than a JS Number can hold exactly
  // (a 32-byte-plus multicodec-prefixed value), the same reason Python's
  // arbitrary-precision int is used there.
  function b58decode(s) {
    for (var i = 0; i < s.length; i++) {
      if (s.charCodeAt(i) > 127) {
        throw new VerifyError(
          "unsupported_key",
          "non-ASCII characters in multibase string: " + JSON.stringify(s)
        );
      }
    }
    var num = 0n;
    for (var j = 0; j < s.length; j++) {
      var idx = B58_ALPHABET.indexOf(s[j]);
      if (idx === -1) {
        throw new VerifyError("unsupported_key", "not valid base58: " + JSON.stringify(s));
      }
      num = num * 58n + BigInt(idx);
    }
    var hex = num.toString(16);
    if (hex.length % 2 === 1) {
      hex = "0" + hex;
    }
    var combined = num === 0n ? new Uint8Array(0) : hexToBytes(hex);
    // n_pad = len(s) - len(s.lstrip("1")) in collector/verify.py: the
    // count of leading '1' characters.
    var nPad = 0;
    for (var k = 0; k < s.length; k++) {
      if (s[k] === "1") {
        nPad++;
      } else {
        break;
      }
    }
    var result = new Uint8Array(nPad + combined.length);
    result.set(combined, nPad);
    return result;
  }

  function hexToBytes(hex) {
    var bytes = new Uint8Array(hex.length / 2);
    for (var i = 0; i < bytes.length; i++) {
      bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
    }
    return bytes;
  }

  function bytesToHex(bytes) {
    var out = "";
    for (var i = 0; i < bytes.length; i++) {
      out += bytes[i].toString(16).padStart(2, "0");
    }
    return out;
  }

  // Mirrors collector/verify.py's did_key_to_ed25519_pubkey exactly:
  // require the "did:key:z" prefix, base58-decode the rest of the
  // multibase string with the leading 'z' dropped, check the two-byte
  // multicodec prefix, and return the raw public key bytes (should be 32
  // for Ed25519, the same length Ed25519PublicKey.from_public_bytes
  // would reject otherwise).
  function didKeyToEd25519PublicKeyBytes(did) {
    if (typeof did !== "string" || did.indexOf("did:key:z") !== 0) {
      throw new VerifyError("unsupported_key", "not a did:key: " + JSON.stringify(did));
    }
    var multibase = did.slice("did:key:".length);
    var raw = b58decode(multibase.slice(1)); // drop the 'z' base58btc multibase prefix
    if (raw.length < 2 || raw[0] !== ED25519_MULTICODEC_PREFIX[0] || raw[1] !== ED25519_MULTICODEC_PREFIX[1]) {
      var prefixHex = bytesToHex(raw.slice(0, 2));
      throw new VerifyError(
        "unsupported_key",
        "unsupported multicodec prefix in " + JSON.stringify(did) + ": " + prefixHex
      );
    }
    var pubkeyBytes = raw.slice(2);
    if (pubkeyBytes.length !== 32) {
      throw new VerifyError(
        "unsupported_key",
        "malformed Ed25519 public key bytes in " + JSON.stringify(did) + ": expected 32 bytes, got " + pubkeyBytes.length
      );
    }
    return pubkeyBytes;
  }

  // Mirrors collector/verify.py's build_signing_payload exactly: the
  // same three-field, pipe-separated string, encoded as UTF-8.
  function buildSigningPayload(room, nonce, text) {
    var s = String(room) + "|" + String(nonce) + "|" + String(text);
    return { bytes: new TextEncoder().encode(s), signedString: s };
  }

  // Mirrors collector/verify.py's inline sig decode exactly:
  // base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)). The padding
  // formula (4 - len % 4) % 4 produces the same 0/3/2/1 pad counts as
  // Python's -len(sig) % 4 for len % 4 of 0/1/2/3 respectively.
  function base64UrlDecode(sig) {
    if (typeof sig !== "string") {
      throw new VerifyError("malformed_record", "sig is not valid base64url: " + JSON.stringify(sig));
    }
    var padLen = (4 - (sig.length % 4)) % 4;
    var padded = sig + "=".repeat(padLen);
    var standard = padded.replace(/-/g, "+").replace(/_/g, "/");
    var binary;
    try {
      binary = atob(standard);
    } catch (e) {
      throw new VerifyError("malformed_record", "sig is not valid base64url: " + JSON.stringify(sig));
    }
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) {
      bytes[i] = binary.charCodeAt(i);
    }
    return bytes;
  }

  // Mirrors collector/verify.py's is_signed exactly.
  function isSigned(record) {
    return typeof record.from === "string" && record.from.indexOf("did:key:") === 0;
  }

  // Probes whether this runtime's WebCrypto actually supports Ed25519
  // (not just that crypto.subtle exists), since some browsers expose
  // SubtleCrypto without every algorithm. Never falls back to anything
  // else if this fails, per the page's own stated policy.
  async function detectEd25519Support() {
    try {
      if (!(globalThis.crypto && globalThis.crypto.subtle && globalThis.crypto.subtle.generateKey)) {
        return false;
      }
      await globalThis.crypto.subtle.generateKey({ name: "Ed25519" }, false, ["sign", "verify"]);
      return true;
    } catch (e) {
      return false;
    }
  }

  // Mirrors collector/verify.py's verify_record, with the same collapsing
  // behavior every analysis module in this project already uses at its
  // own verify_record call site: UnsupportedKeyType and MalformedRecord
  // both mean "not verifiable", reported here as NOT_VERIFIED with a
  // stated reason, never a crash and never silently treated as verified.
  async function verifyRecord(record) {
    if (record === null || typeof record !== "object") {
      return { verdict: "NOT_VERIFIED", reason: "not a JSON object" };
    }
    var did = record.from;
    var pubkeyBytes;
    try {
      pubkeyBytes = didKeyToEd25519PublicKeyBytes(did);
    } catch (e) {
      if (e instanceof VerifyError) {
        return { verdict: "NOT_VERIFIED", reason: e.message };
      }
      throw e;
    }
    var publicKeyHex = bytesToHex(pubkeyBytes);

    var built = buildSigningPayload(record.room, record.nonce, record.text);

    var sigBytes;
    try {
      sigBytes = base64UrlDecode(record.sig);
    } catch (e) {
      if (e instanceof VerifyError) {
        return {
          verdict: "NOT_VERIFIED",
          reason: e.message,
          signedString: built.signedString,
          publicKeyHex: publicKeyHex,
        };
      }
      throw e;
    }

    var cryptoKey;
    try {
      cryptoKey = await globalThis.crypto.subtle.importKey("raw", pubkeyBytes, { name: "Ed25519" }, false, ["verify"]);
    } catch (e) {
      return {
        verdict: "NOT_VERIFIED",
        reason: "malformed Ed25519 public key bytes: " + e.message,
        signedString: built.signedString,
        publicKeyHex: publicKeyHex,
      };
    }

    var ok;
    try {
      ok = await globalThis.crypto.subtle.verify("Ed25519", cryptoKey, sigBytes, built.bytes);
    } catch (e) {
      return {
        verdict: "NOT_VERIFIED",
        reason: "verification error: " + e.message,
        signedString: built.signedString,
        publicKeyHex: publicKeyHex,
      };
    }

    return {
      verdict: ok ? "VERIFIED" : "NOT_VERIFIED",
      reason: ok ? null : "signature does not match",
      signedString: built.signedString,
      publicKeyHex: publicKeyHex,
    };
  }

  return {
    B58_ALPHABET: B58_ALPHABET,
    ED25519_MULTICODEC_PREFIX: ED25519_MULTICODEC_PREFIX,
    VerifyError: VerifyError,
    b58decode: b58decode,
    didKeyToEd25519PublicKeyBytes: didKeyToEd25519PublicKeyBytes,
    buildSigningPayload: buildSigningPayload,
    base64UrlDecode: base64UrlDecode,
    isSigned: isSigned,
    detectEd25519Support: detectEd25519Support,
    verifyRecord: verifyRecord,
    bytesToHex: bytesToHex,
  };
})();

if (typeof module !== "undefined" && module.exports) {
  module.exports = Basanos;
}
