"""Findings notarization (analysis/notarize.py) against known, synthetic
findings files: determinism and canonicalization of the root hash, and
verify_notarization's two independent checks (signature validity, hash
match), including both ways a notarization can go bad.

Uses tests/fixtures/make_fixtures.py's deterministic throwaway-key
approach (FIXTURE_KEY_1, FIXTURE_DID_1, _sign) to build a notarization
record the same way every other signed-record test in this project does
-- no real did:key identity involved, and make_fixtures.py itself is not
modified. This tool never signs anything itself; these tests only ever
call the existing test-fixture signing helper, exactly as every other
test file in this project already does to build fixture records.
"""

import ast
import inspect
import json
import os

import pytest

from make_fixtures import FIXTURE_DID_1, FIXTURE_KEY_1, _sign

import analysis.notarize as notarize_module
from analysis.notarize import compute_findings_hash, verify_notarization

ROOM = "basanos-notarization"


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def _setup_findings(tmp_path, findings_text="Measurement 1: at least 25.8 percent.\n", artifact_obj=None):
    if artifact_obj is None:
        artifact_obj = {"room": "lobby", "cross_key_duplication_rate": 0.258, "distinct_dids": 3}
    findings_path = tmp_path / "FINDINGS.md"
    artifact_path = tmp_path / "duplication_lobby.json"
    _write(str(findings_path), findings_text)
    _write_json(str(artifact_path), artifact_obj)
    return [str(findings_path), str(artifact_path)]


def _sign_notarization(text, nonce="1"):
    sig = _sign(FIXTURE_KEY_1, ROOM, str(nonce), text)
    return {"room": ROOM, "from": FIXTURE_DID_1, "nonce": str(nonce), "text": text, "sig": sig}


def test_same_files_produce_the_same_hash_across_runs(tmp_path):
    paths = _setup_findings(tmp_path)

    hash_a, manifest_a = compute_findings_hash(paths)
    hash_b, manifest_b = compute_findings_hash(paths)

    assert hash_a == hash_b
    assert manifest_a == manifest_b


def test_reordered_json_keys_produce_the_same_hash(tmp_path):
    # Proves canonicalization: the JSON artifact is re-serialized with
    # sort_keys=True before hashing, so key order in the stored file
    # never affects the result. The root hash also covers the file's
    # canonical PATH string, so this keeps the exact same paths fixed and
    # only changes the on-disk key order of the artifact's JSON between
    # the two computations, rather than comparing two different
    # directories (which would legitimately produce different hashes).
    paths = _setup_findings(tmp_path, artifact_obj={"b": 2, "a": 1, "c": 3})
    hash_a, _ = compute_findings_hash(paths)

    _write_json(paths[1], {"a": 1, "b": 2, "c": 3})
    hash_b, _ = compute_findings_hash(paths)

    assert hash_a == hash_b


def test_changing_one_byte_of_findings_changes_the_hash(tmp_path):
    paths_a = _setup_findings(tmp_path / "a", findings_text="Measurement 1: at least 25.8 percent.\n")
    paths_b = _setup_findings(tmp_path / "b", findings_text="Measurement 1: at least 25.9 percent.\n")

    hash_a, _ = compute_findings_hash(paths_a)
    hash_b, _ = compute_findings_hash(paths_b)

    assert hash_a != hash_b


def test_changing_one_json_value_changes_the_hash(tmp_path):
    paths_a = _setup_findings(tmp_path / "a", artifact_obj={"cross_key_duplication_rate": 0.258})
    paths_b = _setup_findings(tmp_path / "b", artifact_obj={"cross_key_duplication_rate": 0.259})

    hash_a, _ = compute_findings_hash(paths_a)
    hash_b, _ = compute_findings_hash(paths_b)

    assert hash_a != hash_b


def test_manifest_lists_every_covered_file_with_its_own_hash(tmp_path):
    paths = _setup_findings(tmp_path)
    root_hash, manifest = compute_findings_hash(paths)

    assert len(manifest) == 2
    covered_paths = {entry["path"] for entry in manifest}
    for path in paths:
        canonical = path.replace(os.sep, "/")
        assert canonical in covered_paths or os.path.normpath(canonical) in covered_paths

    for entry in manifest:
        assert isinstance(entry["sha256"], str)
        assert len(entry["sha256"]) == 64  # hex sha256
        int(entry["sha256"], 16)  # must be valid hex

    # The manifest is sorted by canonical path.
    assert [entry["path"] for entry in manifest] == sorted(entry["path"] for entry in manifest)


def test_verify_notarization_good_case(tmp_path):
    paths = _setup_findings(tmp_path)
    root_hash, _ = compute_findings_hash(paths)
    record = _sign_notarization(root_hash)

    result = verify_notarization(record, paths)

    assert result["signature_valid"] is True
    assert result["hash_matches"] is True
    assert result["overall_verdict"] == "GOOD"
    assert result["recomputed_hash"] == root_hash
    assert result["record_text"] == root_hash


def test_verify_notarization_tampered_findings_fails_hash_match(tmp_path):
    paths = _setup_findings(tmp_path)
    root_hash, _ = compute_findings_hash(paths)
    record = _sign_notarization(root_hash)  # signed over the ORIGINAL findings

    # Tamper the findings after signing: the signature over the record
    # itself is untouched and still valid, but the CURRENT findings no
    # longer match what was notarized.
    findings_path = paths[0]
    with open(findings_path, "a", encoding="utf-8") as f:
        f.write("An extra sentence added after notarization.\n")

    result = verify_notarization(record, paths)

    assert result["signature_valid"] is True  # the record itself was never touched
    assert result["hash_matches"] is False  # but the findings it covers changed
    assert result["overall_verdict"] == "BAD"
    assert result["recomputed_hash"] != root_hash


def test_verify_notarization_broken_signature_fails(tmp_path):
    paths = _setup_findings(tmp_path)
    root_hash, _ = compute_findings_hash(paths)
    record = _sign_notarization(root_hash)
    bad_char = "A" if record["sig"][0] != "A" else "B"
    record["sig"] = bad_char + record["sig"][1:]

    result = verify_notarization(record, paths)

    assert result["signature_valid"] is False
    assert result["hash_matches"] is True  # the text field itself is still the correct hash
    assert result["overall_verdict"] == "BAD"


def _collect_identifiers(tree):
    """Every actual code IDENTIFIER in the module: variable and function
    names, function parameter names, keyword argument names, and
    attribute names (e.g. the "system" in os.system). Deliberately does
    NOT include string literal contents, so a docstring or comment that
    mentions "passphrase" or "PEM" in prose (explaining what this module
    does NOT do) can never be mistaken for the module actually handling
    one -- only a real variable, parameter, or call would use these as
    identifiers.
    """
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
    return identifiers


def test_key_safety_no_forbidden_identifiers_in_module_code():
    # Adversarial: scan the actual code identifiers (never docstring or
    # comment prose, see _collect_identifiers) for anything that would
    # suggest a private key, PEM, passphrase, or a subprocess/getpass
    # call is actually used anywhere in this module's code.
    source = inspect.getsource(notarize_module)
    tree = ast.parse(source)
    identifiers = [name.lower() for name in _collect_identifiers(tree)]

    forbidden_substrings = ("passphrase", "getpass", "subprocess", "privatekey", "private_key", "pem")
    for identifier in identifiers:
        for token in forbidden_substrings:
            assert token not in identifier, (
                f"forbidden token {token!r} found in code identifier {identifier!r} in analysis/notarize.py"
            )


def test_key_safety_no_private_key_class_imported():
    # Precise, AST-based check (not a text scan) that no import anywhere
    # in the module brings in a private-key-capable class. The module's
    # own docstring mentions "Ed25519PrivateKey" by name, in prose, to
    # say it is never imported -- that mention must not itself be
    # mistaken for an import, which is exactly why this walks the actual
    # parsed import statements rather than grepping the text.
    source = inspect.getsource(notarize_module)
    tree = ast.parse(source)
    imported_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_names.extend(alias.name for alias in node.names)

    for name in imported_names:
        assert "private" not in name.lower(), f"unexpected private-key-shaped import: {name}"


def test_key_safety_no_signing_function_defined_in_this_module():
    # There must be no function DEFINED in this module (as opposed to
    # imported from elsewhere, like is_signed/verify_record from
    # collector.verify, which check and verify, never produce a
    # signature) whose job is to produce a signature.
    own_functions = [
        name
        for name, obj in inspect.getmembers(notarize_module, inspect.isfunction)
        if obj.__module__ == notarize_module.__name__
    ]
    for name in own_functions:
        assert "sign" not in name.lower(), f"unexpected signing-shaped function defined in this module: {name}"


def test_no_em_dash_in_module_source():
    source = inspect.getsource(notarize_module)
    assert "—" not in source


def test_cli_hash_and_verify_round_trip(tmp_path, capsys):
    paths = _setup_findings(tmp_path)
    findings_path, artifact_path = paths

    exit_code = notarize_module.main(["hash", "--findings", findings_path, "--artifact", artifact_path])
    assert exit_code == 0
    hash_output = capsys.readouterr().out
    assert "Root hash (sha256):" in hash_output
    assert "does not sign anything" in hash_output

    root_hash, _ = compute_findings_hash(paths)
    record = _sign_notarization(root_hash)
    record_path = tmp_path / "notarization.json"
    _write_json(str(record_path), record)

    exit_code = notarize_module.main(
        [
            "verify",
            "--record",
            str(record_path),
            "--findings",
            findings_path,
            "--artifact",
            artifact_path,
        ]
    )
    assert exit_code == 0
    verify_output = capsys.readouterr().out
    assert "Overall verdict: GOOD" in verify_output


def test_missing_record_field_does_not_crash(tmp_path):
    paths = _setup_findings(tmp_path)
    result = verify_notarization({}, paths)

    assert result["signature_valid"] is False
    assert result["hash_matches"] is False
    assert result["overall_verdict"] == "BAD"
