"""Cross-check that the in-browser verifier (docs/verify.html, backed by
docs/verify_logic.js) reaches the exact same verified/not-verified verdict
as collector/verify.py on the same records.

Approach (a) from the task: docs/verify_logic.js is a small, standalone JS
module with no browser-only dependency except crypto.subtle, which Node
also implements (confirmed: Node's WebCrypto supports Ed25519 raw
import/verify with the identical API surface a browser exposes). This
file is required directly under Node, fed the exact same fixture records
tests/fixtures/make_fixtures.py produces (known-good and known-bad), and
its verdicts are compared against collector/verify.py's on each one.

This also guards the other way the browser page could silently drift from
the tested logic: docs/verify.html does not load verify_logic.js via
<script src> (that would be an external request, and the page must make
none), it embeds that file's code verbatim inside its own inline
<script>. A test here asserts that embedded block is byte for byte
identical to docs/verify_logic.js, so the two can never diverge silently.

Requires a working `node` on PATH; skipped (not failed) if node is not
available, since Node is the delivery mechanism for this cross-check, not
a hard dependency of the Python project itself.
"""

import json
import os
import re
import subprocess

import pytest

from make_fixtures import FIXTURE_DID_1, FIXTURE_DID_2, FIXTURE_KEY_1, FIXTURE_KEY_2, _sign

from collector.verify import MalformedRecord, UnsupportedKeyType, is_signed, verify_record

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_TESTS_DIR)
VERIFY_LOGIC_PATH = os.path.join(_REPO_ROOT, "docs", "verify_logic.js")
VERIFY_HTML_PATH = os.path.join(_REPO_ROOT, "docs", "verify.html")

ROOM = "lobby"


def _node_available():
    try:
        subprocess.run(["node", "--version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


requires_node = pytest.mark.skipif(not _node_available(), reason="node is not available on PATH")


def _signed(key, did, text, nonce):
    return {
        "room": ROOM,
        "from": did,
        "text": text,
        "nonce": str(nonce),
        "sig": _sign(key, ROOM, str(nonce), text),
    }


def _python_verdict(record):
    """The same collapsing every analysis module in this project already
    applies at its own verify_record call site: not signed, or
    UnsupportedKeyType/MalformedRecord/KeyError/TypeError, all collapse to
    a plain False (not verified), never an uncaught exception.
    """
    if not is_signed(record):
        return False
    try:
        return verify_record(record)
    except (UnsupportedKeyType, MalformedRecord, KeyError, TypeError):
        return False


def _build_fixture_records():
    good = _signed(FIXTURE_KEY_1, FIXTURE_DID_1, "hello from key one", 1700000000123)

    tampered_sig = dict(good)
    bad_char = "A" if tampered_sig["sig"][0] != "A" else "B"
    tampered_sig["sig"] = bad_char + tampered_sig["sig"][1:]

    wrong_text = dict(good)
    wrong_text["text"] = "a different message entirely"

    wrong_key_same_sig = dict(good)
    wrong_key_same_sig["from"] = FIXTURE_DID_2  # a real did:key, wrong one for this sig

    malformed_did = dict(good)
    malformed_did["from"] = "did:key:zINVALIDBASE58!!!"

    malformed_did_prefix = dict(good)
    malformed_did_prefix["from"] = "did:notkey:z6MkfK5NC9JAWQWu5YTaxcmwYTXnbvQ88nY5pLmGuXJhorjB"

    malformed_sig = dict(good)
    malformed_sig["sig"] = "not-valid-base64!!!"

    unsigned_nick = {"room": ROOM, "from": "some-nick", "text": "hi", "nonce": "1", "sig": None}

    second_key_good = _signed(FIXTURE_KEY_2, FIXTURE_DID_2, "hello from key two", 1700000000456)

    return {
        "good": good,
        "second_key_good": second_key_good,
        "tampered_sig": tampered_sig,
        "wrong_text": wrong_text,
        "wrong_key_same_sig": wrong_key_same_sig,
        "malformed_did": malformed_did,
        "malformed_did_prefix": malformed_did_prefix,
        "malformed_sig": malformed_sig,
        "unsigned_nick": unsigned_nick,
    }


def _run_js_verdicts(records):
    """Feed `records` (a dict of name -> record) to docs/verify_logic.js
    under Node and return {name: "VERIFIED"|"NOT_VERIFIED"}.
    """
    driver = """
const Basanos = require(process.argv[1]);
const records = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));
(async () => {
  const out = {};
  for (const name of Object.keys(records)) {
    const result = await Basanos.verifyRecord(records[name]);
    out[name] = result.verdict;
  }
  process.stdout.write(JSON.stringify(out));
})();
"""
    fd_path = os.path.join(os.environ.get("TMPDIR", "/tmp"), "basanos_verify_html_fixtures.json")
    with open(fd_path, "w", encoding="utf-8") as f:
        json.dump(records, f)
    try:
        result = subprocess.run(
            ["node", "-e", driver, "--", VERIFY_LOGIC_PATH, fd_path],
            capture_output=True,
            text=True,
            check=True,
        )
    finally:
        os.remove(fd_path)
    return json.loads(result.stdout)


@requires_node
def test_js_and_python_agree_on_every_fixture_record():
    records = _build_fixture_records()
    expected = {name: ("VERIFIED" if _python_verdict(rec) else "NOT_VERIFIED") for name, rec in records.items()}
    actual = _run_js_verdicts(records)

    assert actual == expected

    # State explicitly what this proves, not just that the dicts match:
    # a genuinely valid signature must never show as NOT_VERIFIED, and a
    # genuinely bad one must never show as VERIFIED.
    assert expected["good"] == "VERIFIED"
    assert actual["good"] == "VERIFIED"
    assert expected["second_key_good"] == "VERIFIED"
    assert actual["second_key_good"] == "VERIFIED"
    for name in ("tampered_sig", "wrong_text", "wrong_key_same_sig", "malformed_did", "malformed_did_prefix", "malformed_sig", "unsigned_nick"):
        assert expected[name] == "NOT_VERIFIED"
        assert actual[name] == "NOT_VERIFIED"


@requires_node
def test_js_public_key_decoding_matches_python_exactly():
    """Beyond the pass/fail verdict, confirm the decoded public key bytes
    (hex) match what Python's did_key_to_ed25519_pubkey produces, for the
    two fixture keys -- the part of this page most likely to silently
    diverge from verify.py if the base58/multicodec logic were ever
    subtly wrong in a way that still happened to pass or fail by luck.
    """
    from collector.verify import did_key_to_ed25519_pubkey

    driver = """
const Basanos = require(process.argv[1]);
const dids = JSON.parse(process.argv[2]);
const out = {};
for (const did of dids) {
  out[did] = Basanos.bytesToHex(Basanos.didKeyToEd25519PublicKeyBytes(did));
}
process.stdout.write(JSON.stringify(out));
"""
    dids = [FIXTURE_DID_1, FIXTURE_DID_2]
    result = subprocess.run(
        ["node", "-e", driver, "--", VERIFY_LOGIC_PATH, json.dumps(dids)],
        capture_output=True,
        text=True,
        check=True,
    )
    js_hex_by_did = json.loads(result.stdout)

    for did in dids:
        python_hex = did_key_to_ed25519_pubkey(did).public_bytes_raw().hex()
        assert js_hex_by_did[did] == python_hex


def _extract_code_block(text, start_marker, end_marker):
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


def test_html_embeds_verify_logic_verbatim():
    """docs/verify.html must never load verify_logic.js via <script src>
    (an external request the page is not allowed to make); it embeds the
    same code inline instead. This asserts that embedded copy is byte for
    byte identical to docs/verify_logic.js's own code, so the two files
    can never silently drift apart.
    """
    with open(VERIFY_LOGIC_PATH, encoding="utf-8") as f:
        logic_source = f.read()
    with open(VERIFY_HTML_PATH, encoding="utf-8") as f:
        html_source = f.read()

    code_marker_start = "var Basanos = (function () {"
    code_marker_end = "/* END VERIFY LOGIC */"

    logic_code = logic_source[logic_source.index(code_marker_start):].rstrip("\n")
    embedded_code = _extract_code_block(html_source, code_marker_start, code_marker_end).rstrip("\n")

    assert embedded_code == logic_code


def test_html_never_loads_verify_logic_via_script_src():
    with open(VERIFY_HTML_PATH, encoding="utf-8") as f:
        html_source = f.read()

    # A comment inside the page may mention "<script src>" in loose prose
    # (to explain what is deliberately NOT done), so this checks for an
    # actual tag with a real src attribute (src=), not just the substring:
    # every script on this page has to be inline, or the page would make
    # an external request.
    assert re.search(r"<script\s+[^>]*\bsrc\s*=", html_source) is None


def test_html_makes_no_external_requests():
    """No CDN, no fonts, no analytics, nothing loaded over the network.
    The only occurrences of "http" allowed in the page are inside plain
    prose text (there are none needed here) or a same-origin path; this
    asserts there is no external resource reference at all.
    """
    with open(VERIFY_HTML_PATH, encoding="utf-8") as f:
        html_source = f.read()

    for forbidden in ("http://", "https://", "//fonts.", "cdn.", "<link ", "googleapis", "gstatic"):
        assert forbidden not in html_source


def test_no_em_dash_in_browser_verifier_files():
    for path in (VERIFY_LOGIC_PATH, VERIFY_HTML_PATH):
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "—" not in content


def test_self_test_cases_embedded_in_html_match_expected_verdicts():
    """The page's own in-browser self-test button uses a fixed set of
    records with a stated expected verdict alongside each. Confirm here,
    from Python, that those exact records really do produce the stated
    verdict under collector/verify.py -- so the page's self-test cannot
    quietly assert the wrong thing about itself.
    """
    good_sig = "BAWL8tst51e-VSiEDZqUQiA_UdLVza06VjNmE2ris5ll1y6-YQXQg8spnzKqGUC5EogZTRiyuzedcpJCKBfkAQ"
    record = {
        "room": "lobby",
        "from": "did:key:z6MkfK5NC9JAWQWu5YTaxcmwYTXnbvQ88nY5pLmGuXJhorjB",
        "nonce": "1700000000123",
        "text": "basanos self test message one",
        "sig": good_sig,
    }
    assert _python_verdict(record) is True

    with open(VERIFY_HTML_PATH, encoding="utf-8") as f:
        html_source = f.read()
    assert good_sig in html_source
