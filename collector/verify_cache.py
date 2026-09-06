"""Incremental verification cache: a pure optimization over
`collector/verify.py`'s `verify_record`, safe only because of one fact
about the data model, exploited and nothing more.

THE SAFETY MODEL, THE WHOLE POINT: `collector/storage.py`'s `append_jsonl`
opens every JSONL file in mode "a" -- a room's `messages.jsonl` is
strictly append-only, records are never rewritten, and every record has a
unique, stable integer `seq` within its room. So a record's re-verification
result is PERMANENT: if seq X verified once, against a specific exact
signed byte-string, it verifies against that exact byte-string forever.
This cache exploits only that fact. It never changes which records a
measurement module sees, counts, or how many it re-verifies; it only
skips the redundant Ed25519 check on a record whose signed bytes it has
already checked once before, in a prior run.

THIS IS SAFE PRECISELY BECAUSE MODULES STILL STREAM THE WHOLE FILE: this
cache does not let a module skip reading any record, or skip any
first-seen-ts / min-max-span / seq-map bookkeeping a module builds over
the whole file. Only the cryptographic check itself is cached; every
record is still streamed, still looked at, still counted. A bounded or
windowed read would be a different, much riskier change (it could change
which records a module ever sees); this cache changes nothing about that.

HOW A CACHE HIT IS PROVEN SAFE: a cache entry is (seq, sha256 of the exact
signed byte-string `verify_record` would sign over for that record --
`build_signing_payload(room, nonce, text)`, byte for byte). A hit requires
BOTH the seq to be present AND the stored hash to equal the CURRENT
record's own signed-byte hash. Append-only means a given seq can never
carry different bytes across runs, so a hash mismatch should be
impossible in practice -- but it is checked anyway, and a mismatch is
treated as a full cache miss, never trusted. This means a cache hit is
provably identical to calling `verify_record` fresh: the only way to reach
the hit branch is for the exact bytes that verified successfully before to
be present now, and Ed25519 verification is a pure function of those
bytes. A wrong or stale cache entry can, at worst, cost one redundant
re-verification; it can never produce a wrong verdict.

FALLBACK, THE OTHER HALF OF THE SAFETY MODEL: an absent, empty, unreadable,
or malformed cache file behaves exactly like calling `verify_record` on
every record -- every lookup misses, every record is genuinely
re-verified, output is byte-identical to not having a cache at all. This
module never raises on a bad cache file; a malformed line, a line that
isn't a JSON object, or a seq/hash of the wrong type is simply skipped
when loading, the same "never crash on a malformed record" discipline
every sibling module already applies to messages.jsonl and coverage.jsonl.
The cache is a pure optimization: its absence or corruption only ever
makes a run slower, never wrong.

CONCURRENCY: the collector may be appending to `messages.jsonl` while a
measurement runs; this cache is written only by the measurement itself,
never by the collector, so there is no writer conflict. A measurement
already tolerates a concurrently-growing file (a partial final line is
simply not yet valid JSON and is skipped, picked up whole next run); the
cache only ever gains entries for seqs a measurement fully read and
genuinely verified during its own pass, so a partially-written cache (a
run that stops partway through) is always safe -- any seq missing from it
is simply re-verified next time, exactly as if the cache did not exist.

USAGE (see any of `analysis/duplication.py`, `analysis/coordination.py`,
etc. for the actual integration): a measurement module loads a room's
cache once at the start of its run (`load_verify_cache`), calls
`cached_verify(record, cache, new_entries)` in place of `verify_record`
for every candidate record, and appends whatever `new_entries` collected
back to disk once at the end of its run (`append_verify_cache_entries`).
Every module keeps an explicit `use_verify_cache` parameter (default
True) that, when False, skips both the load and the final append and
passes an empty, never-populated cache through the whole run -- which
makes every lookup a miss, so every record is genuinely re-verified,
identical to a cold cache. This is the disable path the correctness tests
use to prove the cache changes nothing.
"""

import hashlib
import json
import os

from collector.storage import append_jsonl
from collector.verify import build_signing_payload, verify_record


def cache_path(data_dir, room):
    return os.path.join(data_dir, "rooms", room, "verify_cache.jsonl")


def load_verify_cache(data_dir, room):
    """Load `<data_dir>/rooms/<room>/verify_cache.jsonl` into an in-memory
    dict: seq (int) -> sha256 hex hash (str) of the signed bytes that seq
    verified against.

    Returns an empty dict, never raises, if the file is absent, empty, or
    unreadable. A line that isn't valid JSON, isn't a JSON object, or
    whose `seq`/`hash` are not the expected types, is skipped -- it costs
    that one cache entry, not the whole cache, and never a crash. An
    empty dict here means every subsequent `cached_verify` call for this
    room misses, which is exactly the safe fallback: full verification,
    identical to not having a cache at all.
    """
    path = cache_path(data_dir, room)
    cache = {}
    if not os.path.exists(path):
        return cache
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                seq = entry.get("seq")
                digest = entry.get("hash")
                if isinstance(seq, int) and not isinstance(seq, bool) and isinstance(digest, str) and digest:
                    cache[seq] = digest
    except OSError:
        return {}
    return cache


def cached_verify(record, cache, new_entries):
    """Verify `record`'s signature, using `cache` (a dict as returned by
    `load_verify_cache`) to skip the real check when this exact seq has
    already verified against these exact signed bytes before.

    `new_entries` is a list the caller collects newly-confirmed (seq,
    hash) pairs into, to persist once at the end of a run via
    `append_verify_cache_entries`. This function never writes to disk
    itself.

    Returns True/False, or raises exactly what `verify_record` raises
    (`UnsupportedKeyType`, `MalformedRecord`, `KeyError`, `TypeError`),
    on every cache miss -- the miss path is a direct, unmodified call to
    `verify_record`, so a miss is byte-identical to today's behavior. A
    hit only ever returns True, and only when the current record's own
    signed-byte hash matches what this seq verified against before --
    see the module docstring for why that makes a hit provably identical
    to re-verifying, never a shortcut to a wrong answer.
    """
    try:
        payload = build_signing_payload(record["room"], record["nonce"], record["text"])
    except KeyError:
        # Can't be cached at all without room/nonce/text: fall through to
        # verify_record directly, which will raise or fail on the same
        # missing field, exactly as it would without this cache.
        return verify_record(record)

    digest = hashlib.sha256(payload).hexdigest()
    seq = record.get("seq")
    seq_is_cacheable = isinstance(seq, int) and not isinstance(seq, bool)

    if seq_is_cacheable and cache.get(seq) == digest:
        return True

    result = verify_record(record)
    if result and seq_is_cacheable:
        new_entries.append({"seq": seq, "hash": digest})
    return result


def append_verify_cache_entries(data_dir, room, new_entries):
    """Persist `new_entries` (as collected by `cached_verify` calls during
    one run) to `<data_dir>/rooms/<room>/verify_cache.jsonl`, append-only,
    exactly like every other JSONL file in this project. A no-op if there
    is nothing new to write. Entries are deduplicated by seq before
    writing (keeping the last one collected) so a pathological duplicate
    seq within one run's own messages.jsonl never writes more than one
    line per seq.
    """
    if not new_entries:
        return
    deduped = {}
    for entry in new_entries:
        deduped[entry["seq"]] = entry["hash"]
    ordered_entries = [{"seq": seq, "hash": digest} for seq, digest in sorted(deduped.items())]
    append_jsonl(cache_path(data_dir, room), ordered_entries)
