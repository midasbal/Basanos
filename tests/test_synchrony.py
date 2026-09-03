"""Timing synchrony (analysis/synchrony.py) against a known, synthetic
structure: two shared templates with the same post count and the same
distinct-key count, so the ONLY difference between them is the timing of
their posts.

- "bursty template": 6 posts, 5 of them within a single second (one 10s
  bucket) and 1 far out at the end of the same overall active span.
- "spread template": the same 6 posts, evenly spaced one per 10s bucket
  across the identical overall active span.

Both templates share the same active span (50s) and therefore the same
bucket_count (6) at the default 10s bucket width, which means they also
share the exact same seeded UNIFORM null-model expectation (seeded only
from post_count and bucket_count), and -- since they are the only two
shared templates in this fixture's room, and both happen to span the
room's own full window -- the same seeded ROOM null-model expectation too
(both see the identical room-wide activity curve as their weights). Their
room-MINUS-SELF nulls differ (each is weighted by the OTHER template's own
shape), which is expected and is exactly the point of that null.

The headline metric is the index of dispersion (population variance of
per-bucket post counts, over ALL buckets including empty ones, divided by
the mean) versus THREE null models: uniform (naive), room-weighted (this
template's posts against the lobby's own overall activity), and
room-minus-self (room-weighted with this template's own posts subtracted
out first -- the rigorous headline). This module additionally has two
fixtures further down that exercise the room-weighted nulls specifically:
a "crowd follower" template that merely tracks a skewed room (reads near 1
on room and room-minus-self even though it reads well above 1 on the
naive uniform null), and a "self-clustered outlier" template that is
genuinely more clustered than its surrounding room (reads well above 1 on
room-minus-self, the rigorous check).

Uses tests/fixtures/make_fixtures.py's deterministic throwaway-key
approach (FIXTURE_KEY_1/2/3), the same pattern tests/test_coordination.py
uses -- no real did:key identity involved, and make_fixtures.py itself is
not modified.
"""

import json
import os
from datetime import datetime, timedelta, timezone

from make_fixtures import FIXTURE_DID_1, FIXTURE_DID_2, FIXTURE_KEY_1, FIXTURE_KEY_2, _did_key, _fixture_key, _sign

from analysis.synchrony import compute_synchrony_stats, format_report

ROOM = "lobby"

FIXTURE_KEY_3 = _fixture_key("three")
FIXTURE_DID_3 = _did_key(FIXTURE_KEY_3.public_key().public_bytes_raw())

ALL_DIDS = [FIXTURE_DID_1, FIXTURE_DID_2, FIXTURE_DID_3]

BASE_TS = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _ts_at(offset_seconds):
    return (BASE_TS + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _signed(key, did, seq, text, nonce, offset_seconds):
    return {
        "room": ROOM,
        "seq": seq,
        "ts": _ts_at(offset_seconds),
        "from": did,
        "text": text,
        "nonce": str(nonce),
        "sig": _sign(key, ROOM, str(nonce), text),
        "captured_at": _ts_at(offset_seconds),
        "source": "test",
    }


def _unsigned(seq, nick, text, offset_seconds):
    return {
        "room": ROOM,
        "seq": seq,
        "ts": _ts_at(offset_seconds),
        "from": nick,
        "text": text,
        "nonce": None,
        "sig": None,
        "captured_at": _ts_at(offset_seconds),
        "source": "test",
    }


def _write_messages(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True))
            f.write("\n")


BURSTY_TEXT = "bursty template"
SPREAD_TEXT = "spread template"

KEYS = [
    (FIXTURE_KEY_1, FIXTURE_DID_1),
    (FIXTURE_KEY_2, FIXTURE_DID_2),
    (FIXTURE_KEY_3, FIXTURE_DID_3),
]

# Same active span (0s to 50s -> 50s) and therefore the same bucket_count
# (6, at the default 10s bucket width) for both templates -- only the
# distribution of posts within that span differs.
BURSTY_OFFSETS = [0, 1, 2, 3, 4, 50]  # 5 posts inside bucket 0, 1 far post in bucket 5
SPREAD_OFFSETS = [0, 10, 20, 30, 40, 50]  # exactly one post per bucket, 0..5


def _build_records():
    records = []
    seq = 0

    def emit(offsets, text):
        nonlocal seq
        for i, offset in enumerate(offsets):
            seq += 1
            key, did = KEYS[i % len(KEYS)]
            records.append(_signed(key, did, seq, text, nonce=9000 + seq, offset_seconds=offset))

    emit(BURSTY_OFFSETS, BURSTY_TEXT)
    emit(SPREAD_OFFSETS, SPREAD_TEXT)

    seq += 1
    records.append(_unsigned(seq, "fixture-nick-anon", "unsigned nicks are excluded", offset_seconds=0))

    seq += 1
    key, did = KEYS[0]
    broken = _signed(key, did, seq, "broken template text", nonce=9999, offset_seconds=0)
    broken["sig"] = ("A" if broken["sig"][0] != "A" else "B") + broken["sig"][1:]
    records.append(broken)

    return records


def _setup(tmp_path):
    data_dir = tmp_path / "data"
    messages_path = data_dir / "rooms" / ROOM / "messages.jsonl"
    _write_messages(str(messages_path), _build_records())

    coverage_state = {ROOM: {"captured_total": 40, "dropped_total": 10}}
    os.makedirs(str(data_dir), exist_ok=True)
    with open(data_dir / "coverage_state.json", "w", encoding="utf-8") as f:
        json.dump(coverage_state, f)

    return str(data_dir)


def _entry_for(stats, text):
    return next(e for e in stats["templates"] if e["text"] == text)


def test_reverify_counts_and_exclusions(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_synchrony_stats(data_dir, room=ROOM, top_n=2)

    # 6 bursty + 6 spread + 1 broken = 13 signed messages checked; the
    # unsigned nick is excluded before it is ever counted as "checked".
    assert stats["signed_checked"] == 13
    assert stats["signed_reverified"] == 12
    assert stats["signed_reverify_failed"] == 1


def test_bursty_observed_dispersion_hand_calculated(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_synchrony_stats(data_dir, room=ROOM, top_n=2, bucket_seconds=10.0)
    bursty = _entry_for(stats, BURSTY_TEXT)

    # active span = 50 - 0 = 50s; bucket_count = floor(50/10) + 1 = 6.
    # Offsets 0,1,2,3,4 all land in bucket 0 (5 posts); offset 50 lands in
    # bucket 5 (1 post); buckets 1,2,3,4 are empty (0 posts each).
    # Per-bucket counts over ALL 6 buckets: [5, 0, 0, 0, 0, 1].
    # mean = post_count / bucket_count = 6 / 6 = 1.
    # population variance = mean of squared deviations from the mean:
    #   (5-1)^2 + (0-1)^2*4 + (1-1)^2  all over 6 buckets
    #   = (16 + 1 + 1 + 1 + 1 + 0) / 6 = 20 / 6.
    # observed_dispersion = variance / mean = (20/6) / 1 = 20/6.
    assert bursty["post_count"] == 6
    assert bursty["active_span_seconds"] == 50.0
    assert bursty["bucket_count"] == 6
    assert bursty["occupied_bucket_count"] == 2
    assert bursty["observed_dispersion"] == 20 / 6

    # busiest_bucket_fraction: bucket 0 (5 posts) / 6 total posts = 5/6.
    assert bursty["busiest_bucket_fraction"] == 5 / 6


def test_spread_observed_dispersion_hand_calculated(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_synchrony_stats(data_dir, room=ROOM, top_n=2, bucket_seconds=10.0)
    spread = _entry_for(stats, SPREAD_TEXT)

    # Same active span and bucket_count as bursty (50s, 6 buckets), but one
    # post per bucket -> per-bucket counts over all 6 buckets: [1,1,1,1,1,1].
    # mean = 6/6 = 1. Every bucket equals the mean exactly, so every
    # squared deviation is 0: population variance = 0 / 6 = 0.
    # observed_dispersion = variance / mean = 0 / 1 = 0, exactly.
    assert spread["post_count"] == 6
    assert spread["active_span_seconds"] == 50.0
    assert spread["bucket_count"] == 6
    assert spread["occupied_bucket_count"] == 6
    assert spread["observed_dispersion"] == 0

    # busiest_bucket_fraction: any single bucket (1 post) / 6 total = 1/6.
    assert spread["busiest_bucket_fraction"] == 1 / 6


def test_bursty_dispersion_ratio_meaningfully_greater_than_spread(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_synchrony_stats(data_dir, room=ROOM, top_n=2, bucket_seconds=10.0)
    bursty = _entry_for(stats, BURSTY_TEXT)
    spread = _entry_for(stats, SPREAD_TEXT)

    # Both templates share the same post_count (6) and bucket_count (6),
    # so they share the exact same seeded UNIFORM null-model expectation.
    assert bursty["expected_dispersion_uniform"] == spread["expected_dispersion_uniform"]

    # bursty and spread are the only two shared templates in this room, and
    # both span the room's own full window, so the room curve slice used
    # for each is identical -- they share the same seeded ROOM null too.
    assert bursty["expected_dispersion_room"] == spread["expected_dispersion_room"]

    # Their room-MINUS-SELF nulls differ on purpose: bursty's minus-self
    # weights are spread's own (near-uniform) shape, while spread's
    # minus-self weights are bursty's own (highly clustered) shape.
    assert bursty["expected_dispersion_room_minus_self"] != spread["expected_dispersion_room_minus_self"]

    # The clean separation the old busiest-bucket-based metric lacked:
    # perfectly-even spacing now reads exactly 0, not some middling value
    # close to what random posting would produce -- across all three
    # nulls, since a 0 numerator makes every ratio exactly 0 regardless of
    # the denominator.
    assert spread["observed_dispersion"] == 0
    assert spread["dispersion_ratio_uniform"] == 0
    assert spread["dispersion_ratio_room"] == 0
    assert spread["dispersion_ratio_room_minus_self"] == 0

    # Bursty is unambiguously coordination-shaped on every null: well above
    # a 2x floor on uniform, room, AND the room-minus-self headline, and
    # strictly greater than spread's (0) on each.
    assert bursty["observed_dispersion"] > 0
    assert bursty["dispersion_ratio_uniform"] > 2.0
    assert bursty["dispersion_ratio_room"] > 2.0
    assert bursty["dispersion_ratio_room_minus_self"] > 2.0
    assert bursty["dispersion_ratio_room_minus_self"] > spread["dispersion_ratio_room_minus_self"]


def _emit_bucketed(records, seq_box, text, counts, bucket_offsets, keys):
    """Append one signed message per unit in `counts` (a per-bucket count
    list), cycling through `keys` for signer variety, at the bucket's
    representative offset. `seq_box` is a single-item list used as a
    mutable counter across repeated calls.
    """
    for bucket_index, count in enumerate(counts):
        for _ in range(count):
            seq_box[0] += 1
            key, did = keys[seq_box[0] % len(keys)]
            records.append(
                _signed(
                    key,
                    did,
                    seq_box[0],
                    text,
                    nonce=9000 + seq_box[0],
                    offset_seconds=bucket_offsets[bucket_index],
                )
            )


BUCKET_OFFSETS_6 = [5, 15, 25, 35, 45, 55]  # one representative offset per 10s bucket, 0..5


def _setup_crowd_follower(tmp_path):
    """A room with a strong activity swing (edge-heavy: buckets 0 and 5
    dominate, the middle four buckets are thin) built from a "bg text"
    template, plus a "follower text" template whose own post-to-bucket
    counts were drawn as an actual weighted-random sample from that same
    room shape (see the comment on FOLLOWER_COUNTS below) -- a plausible
    stand-in for a poster whose activity simply rides the room's own
    rhythm, not a hand-smoothed proportional split (which would UNDERSTATE
    dispersion relative to what genuine random sampling from that shape
    produces, and wrongly read as LESS bursty than the room, not "same as
    the room").
    """
    data_dir = tmp_path / "data"
    messages_path = data_dir / "rooms" / ROOM / "messages.jsonl"

    records = []
    seq_box = [0]
    bg_counts = [5, 1, 1, 1, 1, 5]
    # A reproducible sample of 14 draws from the bg_counts shape
    # (weights=[5,1,1,1,1,5]), taken once with random.Random(42).choices
    # and hardcoded here so the fixture itself has no runtime randomness.
    follower_counts = [8, 1, 0, 1, 1, 3]
    _emit_bucketed(records, seq_box, "bg text", bg_counts, BUCKET_OFFSETS_6, KEYS)
    _emit_bucketed(records, seq_box, "follower text", follower_counts, BUCKET_OFFSETS_6, KEYS)

    _write_messages(str(messages_path), records)
    return str(data_dir)


def test_crowd_follower_reads_near_one_on_room_and_minus_self(tmp_path):
    data_dir = _setup_crowd_follower(tmp_path)
    stats = compute_synchrony_stats(data_dir, room=ROOM, top_n=2, bucket_seconds=10.0)
    follower = _entry_for(stats, "follower text")

    # This is the whole point of the room-weighted null revision: a
    # template that merely follows the room's own (skewed) rhythm reads
    # well above 1 on the naive uniform null...
    assert follower["dispersion_ratio_uniform"] > 2.0

    # ...but near 1 on both the room and room-minus-self nulls, because
    # once you weight the null by the room's own shape (with or without
    # this template's own contribution), the template's timing is no
    # longer surprising.
    assert 0.5 <= follower["dispersion_ratio_room"] <= 2.0
    assert 0.5 <= follower["dispersion_ratio_room_minus_self"] <= 2.0


def _setup_self_clustered_outlier(tmp_path):
    """A flat, unremarkable room backdrop (2 posts in every 10s bucket)
    plus an "outlier text" template that is genuinely more clustered than
    that backdrop: 20 of its 21 posts land in a single bucket (bucket 2),
    with 1 more post in bucket 4 only to give it an active span wide
    enough to have more than one bucket at all.
    """
    data_dir = tmp_path / "data"
    messages_path = data_dir / "rooms" / ROOM / "messages.jsonl"

    records = []
    seq_box = [0]
    backdrop_counts = [2, 2, 2, 2, 2, 2]
    _emit_bucketed(records, seq_box, "backdrop text", backdrop_counts, BUCKET_OFFSETS_6, KEYS)
    # 20 posts in bucket 2, 1 post in bucket 4 -- same shape as this
    # module's own "bursty template" fixture, just against a flat backdrop
    # instead of no backdrop at all.
    outlier_counts_by_bucket = {2: 20, 4: 1}
    for bucket_index, count in outlier_counts_by_bucket.items():
        for _ in range(count):
            seq_box[0] += 1
            key, did = KEYS[seq_box[0] % len(KEYS)]
            records.append(
                _signed(
                    key,
                    did,
                    seq_box[0],
                    "outlier text",
                    nonce=9000 + seq_box[0],
                    offset_seconds=BUCKET_OFFSETS_6[bucket_index],
                )
            )

    _write_messages(str(messages_path), records)
    return str(data_dir)


def test_self_clustered_outlier_reads_well_above_one_on_minus_self(tmp_path):
    data_dir = _setup_self_clustered_outlier(tmp_path)
    stats = compute_synchrony_stats(data_dir, room=ROOM, top_n=2, bucket_seconds=10.0)
    outlier = _entry_for(stats, "outlier text")

    # The rigorous check: even after removing the outlier's own posts from
    # the room baseline (leaving just the flat 2-per-bucket backdrop over
    # the outlier's own active span), the outlier's own clustering is
    # still far more concentrated than that backdrop.
    assert outlier["observed_dispersion"] > 0
    assert outlier["dispersion_ratio_room_minus_self"] > 5.0


def test_same_input_reproduces_the_same_expectations(tmp_path):
    data_dir = _setup_crowd_follower(tmp_path)
    stats_a = compute_synchrony_stats(data_dir, room=ROOM, top_n=2, bucket_seconds=10.0)
    stats_b = compute_synchrony_stats(data_dir, room=ROOM, top_n=2, bucket_seconds=10.0)

    follower_a = _entry_for(stats_a, "follower text")
    follower_b = _entry_for(stats_b, "follower text")
    assert follower_a["expected_dispersion_uniform"] == follower_b["expected_dispersion_uniform"]
    assert follower_a["expected_dispersion_room"] == follower_b["expected_dispersion_room"]
    assert follower_a["expected_dispersion_room_minus_self"] == follower_b["expected_dispersion_room_minus_self"]


def test_no_did_string_anywhere_in_json_output(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_synchrony_stats(data_dir, room=ROOM, top_n=2)
    dumped = json.dumps(stats)

    for entry in stats["templates"]:
        assert set(entry.keys()) == {
            "text",
            "distinct_keys",
            "post_count",
            "active_span_seconds",
            "bucket_count",
            "occupied_bucket_count",
            "observed_dispersion",
            "expected_dispersion_uniform",
            "expected_dispersion_room",
            "expected_dispersion_room_minus_self",
            "dispersion_ratio_uniform",
            "dispersion_ratio_room",
            "dispersion_ratio_room_minus_self",
            "busiest_bucket_fraction",
        }
    for did in ALL_DIDS:
        assert did not in dumped


def test_coverage_surfaced(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_synchrony_stats(data_dir, room=ROOM, top_n=2)

    assert stats["coverage_captured_total"] == 40
    assert stats["coverage_dropped_total"] == 10
    assert stats["coverage_ratio"] == 40 / 50


def test_report_contains_required_language_and_no_dids(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_synchrony_stats(data_dir, room=ROOM, top_n=2)
    report = format_report(stats)

    assert "FLOOR" in report
    assert "10" in report  # bucket width surfaced somewhere
    assert "heartbeat-style posting" in report
    assert "not a verdict about any poster" in report
    assert "room-minus-self" in report
    assert "HEADLINE" in report
    assert "dispersion" in report
    assert "busiest" in report
    assert "checked" in report and "re-verified" in report and "failed" in report
    for did in ALL_DIDS:
        assert did not in report


def test_missing_messages_file_does_not_crash(tmp_path):
    data_dir = tmp_path / "empty_data"
    os.makedirs(str(data_dir), exist_ok=True)
    stats = compute_synchrony_stats(str(data_dir), room="lobby")
    assert stats["messages_file_found"] is False
    assert stats["signed_checked"] == 0
    report = format_report(stats)
    assert "No messages.jsonl found" in report
