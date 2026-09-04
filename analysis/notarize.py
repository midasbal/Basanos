"""Findings notarization: makes Basanos's published findings tamper-evident.

This is not a measurement of the commons. It is a tool over Basanos's own
output, in the same spirit as `analysis/selfaudit.py` turning the
project's own standard back on itself: FINDINGS.md and the analysis
artifacts behind it are only worth publishing if a reader can later
confirm they have not been quietly edited. This module makes that
checkable, without ever touching a private key.

THE SHAPE OF THIS, AND WHY IT IS SHAPED THIS WAY: the project's founding
rule is that a private key never touches any tooling or the analysis
pipeline (see collector/verify.py's own docstring: it only ever reads
already-collected records, never signs or posts). Notarizing findings
still needs a signature to be worth anything, so the signing step itself
happens entirely OUTSIDE this tool, by the user, with their own
Technocore did:key, using their own `technocore_id.py sign` command. This
module's job is narrower and on each side of that step:

1. Compute a canonical hash over a fixed, explicit set of findings files
   (`compute_findings_hash`, Part 1 below). The user signs THIS HASH as
   the `text` of a normal Technocore signed message (room|nonce|text,
   exactly the shape every other signed record in this project has), by
   running `technocore_id.py sign` themselves. This tool prints the hash
   and instructions; it never signs anything itself.

2. Verify a completed notarization record after the fact
   (`verify_notarization`, Part 2 below): re-verify its signature with
   the exact same `collector.verify.verify_record` every other module in
   this project uses, and separately recompute the findings hash fresh
   from the current files and compare it to the record's `text`. Both
   have to hold for a notarization to be good. If the findings changed
   since signing, the hash comparison fails and says so plainly -- that
   failure IS the tamper-evidence working, not a bug.

KEY SAFETY, THE MOST IMPORTANT PROPERTY OF THIS FILE: this module never
imports, reads, opens, or references any private key material, any PEM
file, any passphrase, or `technocore_id.py`'s own signing code path. It
has no function that produces a signature. The only cryptographic
operation anywhere in this file is verification, via
`collector.verify.verify_record`, which itself only ever imports
`Ed25519PublicKey` (never `Ed25519PrivateKey`) for exactly this reason.
Signing is the user's own act, on their own machine, with their own key,
outside this tool entirely.

Deliberately out of scope: this module does not shell out to
`technocore_id.py`, does not construct a signing request, and does not
know or care where the user's private key lives. Every number and hash
below is derived only from already-public findings files and an
already-signed record; nothing here could produce a signature even by
accident.

Usage:
    python -m analysis.notarize hash --findings FINDINGS.md --artifact <path> [--artifact <path> ...]
    python -m analysis.notarize verify --record <notarization.json> --findings FINDINGS.md --artifact <path> [...]
"""

import argparse
import hashlib
import json
import os
import posixpath

from collector.verify import MalformedRecord, UnsupportedKeyType, is_signed, verify_record

DEFAULT_FINDINGS_PATH = "FINDINGS.md"


def _canonical_path(path):
    """Normalize a file path into the exact string form used both as the
    manifest entry and as input to the root hash: forward slashes only
    (so the same path string comes out the same on any OS), redundant
    "./" segments and duplicate slashes collapsed via posixpath.normpath.
    Never resolved to an absolute path -- the path as the caller gave it,
    just normalized, is what gets hashed, so two callers who point at the
    same file the same relative way reproduce the same manifest entry.
    """
    return posixpath.normpath(path.replace(os.sep, "/"))


def _sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def _file_canonical_bytes(path):
    """The exact bytes that get hashed for one file.

    A .json file is loaded and re-serialized with
    json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    before hashing, so two files that are semantically identical JSON --
    same keys, same values, different key order or incidental whitespace
    -- hash identically. Any other file (FINDINGS.md) is hashed as its
    raw bytes, unchanged: it is prose, not data, and its exact bytes are
    the thing being notarized.
    """
    if path.endswith(".json"):
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
        canonical = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return canonical.encode("utf-8")
    with open(path, "rb") as f:
        return f.read()


def compute_findings_hash(paths):
    """Compute the canonical root hash over `paths` (an explicit,
    caller-given list of file paths: FINDINGS.md and zero or more
    analysis JSON artifacts), plus the manifest that backs it.

    Deterministic construction, exact format, so anyone can reproduce the
    same root hash from the same files without this code:

    1. Each path is normalized to its canonical path string (see
       _canonical_path) and its canonical bytes are hashed with SHA-256
       (see _file_canonical_bytes), giving one (canonical_path,
       hex_sha256) pair per file.
    2. Those pairs are sorted lexicographically by canonical_path.
    3. A single buffer is built by concatenating, for each pair in that
       sorted order, the string "<canonical_path>\\n<hex_sha256>\\n"
       (newline-separated path then hash, newline-terminated, per file,
       one file directly after another, no other separator).
    4. The root hash is the hex SHA-256 of that buffer, UTF-8 encoded.

    Returns (root_hash_hex, manifest), where manifest is a list of
    {"path": canonical_path, "sha256": hex_sha256} dicts in the same
    sorted order, so a verifier can see exactly what was covered and
    re-check any single file independently of the root hash.
    """
    pairs = []
    for path in paths:
        canonical_path = _canonical_path(path)
        file_hash = _sha256_hex(_file_canonical_bytes(path))
        pairs.append((canonical_path, file_hash))
    pairs.sort(key=lambda entry: entry[0])

    buffer = "".join(f"{canonical_path}\n{file_hash}\n" for canonical_path, file_hash in pairs)
    root_hash = _sha256_hex(buffer.encode("utf-8"))
    manifest = [{"path": canonical_path, "sha256": file_hash} for canonical_path, file_hash in pairs]
    return root_hash, manifest


def verify_notarization(record, paths):
    """Verify a completed notarization record against the current
    findings files.

    `record` is a signed room|nonce|text JSON, exactly the shape
    technocore_id.py sign produces (from, room, nonce, text, sig) and
    exactly the shape every other signed message in this project has.
    `paths` is the same explicit file list `compute_findings_hash` would
    take for the findings being notarized.

    Two independent checks, both required for a good notarization:

    (a) signature_valid: the record's signature actually verifies, via
        the exact same collector.verify.verify_record every other
        module in this project uses (the same Ed25519/did:key check,
        never a different or looser one for this tool). Not signed, or
        UnsupportedKeyType/MalformedRecord/KeyError/TypeError, all
        collapse to False, never a crash, matching the pattern every
        analysis module already uses at its own verify_record call site.
    (b) hash_matches: the record's own `text` equals the CURRENT
        findings hash, recomputed fresh from `paths` right now, not
        trusted from whenever the record was signed. If the findings
        changed since signing, this is what catches it.

    overall_verdict is "GOOD" only if both hold. Returns a dict with
    both booleans, the verdict, the freshly recomputed hash, the
    record's own text, and the manifest, so a reader can see exactly
    what was compared.
    """
    try:
        signature_valid = bool(is_signed(record)) and bool(verify_record(record))
    except (UnsupportedKeyType, MalformedRecord, KeyError, TypeError):
        signature_valid = False

    recomputed_hash, manifest = compute_findings_hash(paths)
    record_text = record.get("text") if isinstance(record, dict) else None
    hash_matches = isinstance(record_text, str) and record_text == recomputed_hash

    return {
        "signature_valid": signature_valid,
        "hash_matches": hash_matches,
        "overall_verdict": "GOOD" if (signature_valid and hash_matches) else "BAD",
        "recomputed_hash": recomputed_hash,
        "record_text": record_text,
        "manifest": manifest,
    }


def format_hash_report(root_hash, manifest):
    lines = []
    lines.append("Findings canonical hash")
    lines.append("=======================")
    lines.append("")
    lines.append("Manifest (file covered, per-file sha256):")
    for entry in manifest:
        lines.append(f"  {entry['path']}: {entry['sha256']}")
    lines.append("")
    lines.append(f"Root hash (sha256): {root_hash}")
    lines.append("")
    lines.append(
        "This tool does not sign anything. Sign this exact root hash as the text of a "
        "normal Technocore signed message yourself, with your own did:key, using your own "
        "technocore_id.py sign command (for example: sign with text set to the root hash "
        "above). Save the resulting signed record and keep it beside these findings; anyone "
        "can later run `python -m analysis.notarize verify` to confirm the findings still "
        "match what was signed."
    )
    return "\n".join(lines)


def format_verify_report(result):
    lines = []
    lines.append("Notarization verification")
    lines.append("==========================")
    lines.append("")
    lines.append(f"signature valid: {result['signature_valid']}")
    lines.append(f"hash matches:    {result['hash_matches']}")
    lines.append("")
    lines.append(f"recomputed findings hash: {result['recomputed_hash']}")
    lines.append(f"record's signed text:     {result['record_text']}")
    lines.append("")
    lines.append("Manifest (file covered, per-file sha256):")
    for entry in result["manifest"]:
        lines.append(f"  {entry['path']}: {entry['sha256']}")
    lines.append("")
    lines.append(f"Overall verdict: {result['overall_verdict']}")
    if result["overall_verdict"] != "GOOD":
        if not result["signature_valid"]:
            lines.append("The record's signature does not verify.")
        if not result["hash_matches"]:
            lines.append(
                "The findings no longer match what this record notarizes: either the "
                "findings changed since signing, or this record was never signed over "
                "these exact files. This is the tamper-evidence working, not a bug."
            )
    return "\n".join(lines)


def _resolve_paths(args):
    if args.manifest:
        with open(args.manifest, encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    return [args.findings] + list(args.artifact)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Notarize or verify Basanos's published findings (read-only; never "
        "signs anything itself)."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    hash_parser = subparsers.add_parser(
        "hash", help="compute the canonical findings hash for the user to sign themselves"
    )
    hash_parser.add_argument(
        "--findings", default=DEFAULT_FINDINGS_PATH, help=f"path to FINDINGS.md (default: {DEFAULT_FINDINGS_PATH})"
    )
    hash_parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        help="path to an analysis JSON artifact to include; may be given more than once",
    )
    hash_parser.add_argument(
        "--manifest",
        default=None,
        help="path to a plain text file listing one file path per line; overrides --findings/--artifact",
    )

    verify_parser = subparsers.add_parser(
        "verify", help="verify a signed notarization record against the current findings"
    )
    verify_parser.add_argument("--record", required=True, help="path to the signed notarization record JSON")
    verify_parser.add_argument(
        "--findings", default=DEFAULT_FINDINGS_PATH, help=f"path to FINDINGS.md (default: {DEFAULT_FINDINGS_PATH})"
    )
    verify_parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        help="path to an analysis JSON artifact to include; may be given more than once",
    )
    verify_parser.add_argument(
        "--manifest",
        default=None,
        help="path to a plain text file listing one file path per line; overrides --findings/--artifact",
    )

    args = parser.parse_args(argv)
    paths = _resolve_paths(args)

    if args.command == "hash":
        root_hash, manifest = compute_findings_hash(paths)
        print(format_hash_report(root_hash, manifest))
        return 0

    with open(args.record, encoding="utf-8") as f:
        record = json.load(f)
    result = verify_notarization(record, paths)
    print(format_verify_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
