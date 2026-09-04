"""Operator clustering (analysis/clustering.py) against a known, synthetic
structure, run with a small --cap (3) so the "promiscuous template"
exclusion case does not require hundreds of fixture keys.

Structure (bucket_seconds/ts do not matter here, only text and signer):
- "cluster template one" and "cluster template two": both signed by
  K1, K2, K3 -> every pair among {K1, K2, K3} shares 2 bounded templates
  -> one cluster of size 3 at min_shared=2, but none at min_shared=3
  (only 2 shared templates exist between any pair).
- "single share template": signed by K4, K5 only -> shares exactly 1
  template -> must NOT link at min_shared=2.
- "chain template a" (KA, KB) and "chain template b" (KB, KC): a genuine
  single-shared-template chain -- KA-KB share 1, KB-KC share 1, KA-KC
  share 0. The anti-chaining case: at min_shared=1 this WOULD collapse
  into one cluster of 3 via transitivity through KB; at min_shared=2 it
  must NOT, since no pair meets the threshold directly.
- "promiscuous template": signed by K10, K11, K12, K13 (4 keys) -- with
  --cap 3, this exceeds the cap and is excluded from linkage entirely;
  these 4 keys never even enter keys_in_bounded_templates.
- K_SOLO: signs one unique, unshared text -- not part of any shared
  template at all, included only to make distinct_keys_overall honest.

Uses tests/fixtures/make_fixtures.py's deterministic throwaway-key
approach (extra labels beyond FIXTURE_KEY_1/2), the same pattern
tests/test_coordination.py uses -- no real did:key identity involved, and
make_fixtures.py itself is not modified.
"""

import json
import os

import pytest

from make_fixtures import _did_key, _fixture_key, _sign

from analysis.clustering import HISTOGRAM_BUCKETS, _cluster_sizes, compute_clustering_stats, format_report

ROOM = "lobby"

LABELS = ["k1", "k2", "k3", "k4", "k5", "ka", "kb", "kc", "k10", "k11", "k12", "k13", "solo"]
FIXTURE_KEYS = {label: _fixture_key(label) for label in LABELS}
FIXTURE_DIDS = {label: _did_key(key.public_key().public_bytes_raw()) for label, key in FIXTURE_KEYS.items()}

ALL_DIDS = list(FIXTURE_DIDS.values())


def _signed(label, seq, text, nonce):
    key = FIXTURE_KEYS[label]
    did = FIXTURE_DIDS[label]
    return {
        "room": ROOM,
        "seq": seq,
        "ts": f"2000-01-01T00:{seq:02d}:00.000000Z",
        "from": did,
        "text": text,
        "nonce": str(nonce),
        "sig": _sign(key, ROOM, str(nonce), text),
        "captured_at": f"2000-01-01T00:{seq:02d}:00.000000Z",
        "source": "test",
    }


def _unsigned(seq, nick, text):
    return {
        "room": ROOM,
        "seq": seq,
        "ts": f"2000-01-01T00:{seq:02d}:00.000000Z",
        "from": nick,
        "text": text,
        "nonce": None,
        "sig": None,
        "captured_at": f"2000-01-01T00:{seq:02d}:00.000000Z",
        "source": "test",
    }


def _write_messages(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True))
            f.write("\n")


T1 = "cluster template one"
T2 = "cluster template two"
T3 = "single share template"
TA = "chain template a"
TB = "chain template b"
T_PROMISCUOUS = "promiscuous template"


def _build_records():
    records = []
    seq = 0

    def emit(label, text, nonce):
        nonlocal seq
        seq += 1
        records.append(_signed(label, seq, text, nonce))

    for label in ("k1", "k2", "k3"):
        emit(label, T1, 1000 + seq)
    for label in ("k1", "k2", "k3"):
        emit(label, T2, 2000 + seq)
    for label in ("k4", "k5"):
        emit(label, T3, 3000 + seq)
    for label in ("ka", "kb"):
        emit(label, TA, 4000 + seq)
    for label in ("kb", "kc"):
        emit(label, TB, 5000 + seq)
    for label in ("k10", "k11", "k12", "k13"):
        emit(label, T_PROMISCUOUS, 6000 + seq)
    emit("solo", "solo's own unique text", 7000 + seq)

    seq += 1
    records.append(_unsigned(seq, "fixture-nick-anon", "unsigned nicks are excluded"))

    seq += 1
    broken = _signed("k1", seq, "broken text", 9999)
    bad_char = "A" if broken["sig"][0] != "A" else "B"
    broken["sig"] = bad_char + broken["sig"][1:]
    records.append(broken)

    return records


def _setup(tmp_path):
    data_dir = tmp_path / "data"
    _write_messages(str(data_dir / "rooms" / ROOM / "messages.jsonl"), _build_records())
    return str(data_dir)


def _compute(data_dir, min_shared=2):
    return compute_clustering_stats(data_dir, room=ROOM, cap=3, min_shared=min_shared)


def _pass_for(stats, min_shared):
    return next(p for p in stats["passes"] if p["min_shared"] == min_shared)


def test_reverify_counts(tmp_path):
    data_dir = _setup(tmp_path)
    stats = _compute(data_dir)

    # 3+3+2+2+2+4+1 = 17 fixture messages + 1 broken = 18 signed checked;
    # the unsigned nick is excluded before it is ever counted as "checked".
    assert stats["signed_checked"] == 18
    assert stats["signed_reverified"] == 17
    assert stats["signed_reverify_failed"] == 1


def test_bounded_and_promiscuous_template_counts(tmp_path):
    data_dir = _setup(tmp_path)
    stats = _compute(data_dir)

    # Bounded (2 <= signers <= cap=3): T1, T2, T3, TA, TB = 5. Promiscuous
    # (> cap=3 signers): the 4-signer template = 1, excluded from linkage.
    assert stats["bounded_template_count"] == 5
    assert stats["excluded_promiscuous_template_count"] == 1
    assert stats["distinct_shared_templates"] == 6  # 5 bounded + 1 promiscuous

    # K10-K13's only template is promiscuous and excluded, and solo's text
    # is not shared at all -- neither group ever enters bounded linkage.
    # keys_in_bounded_templates = {k1,k2,k3,k4,k5,ka,kb,kc} = 8.
    assert stats["keys_in_bounded_templates_count"] == 8
    assert stats["distinct_keys_overall"] == 13  # k1..k5, ka..kc, k10..k13, solo


def test_cluster_distribution_at_min_shared_2_hand_calculated(tmp_path):
    data_dir = _setup(tmp_path)
    stats = _compute(data_dir, min_shared=2)
    p2 = _pass_for(stats, 2)

    # k1/k2/k3 share BOTH T1 and T2 -> every pair has shared-count 2 ->
    # all three link into one cluster of size 3.
    # k4/k5 share only T3 (count 1) -> no link, both singletons.
    # ka/kb share only TA (count 1), kb/kc share only TB (count 1), and
    # ka/kc share nothing -> no pair meets the threshold -> all three
    # singletons (the anti-chaining case, see the dedicated test below).
    # Cluster sizes over the 8 keys in bounded templates: [3, 1, 1, 1, 1, 1]
    assert p2["cluster_count"] == 6
    assert p2["multi_key_cluster_count"] == 1
    assert p2["singleton_count"] == 5
    assert p2["largest_cluster_size"] == 3
    assert p2["size_histogram"] == {
        "2": 0,
        "3-5": 1,
        "6-10": 0,
        "11-50": 0,
        "51-200": 0,
        "201-1000": 0,
        "1000+": 0,
    }


def test_cluster_distribution_at_min_shared_3_hand_calculated(tmp_path):
    data_dir = _setup(tmp_path)
    stats = _compute(data_dir, min_shared=2)  # always also reports min_shared + 1 = 3
    p3 = _pass_for(stats, 3)

    # No pair anywhere shares 3+ bounded templates (the max is 2, between
    # k1/k2/k3) -- every one of the 8 keys is its own singleton cluster.
    assert p3["cluster_count"] == 8
    assert p3["multi_key_cluster_count"] == 0
    assert p3["singleton_count"] == 8
    assert p3["largest_cluster_size"] == 1
    assert all(count == 0 for count in p3["size_histogram"].values())


def test_pair_sharing_only_one_template_does_not_link(tmp_path):
    data_dir = _setup(tmp_path)
    stats = _compute(data_dir, min_shared=2)
    p2 = _pass_for(stats, 2)

    # Already implied by the size distribution above (5 singletons, one
    # cluster of 3), but stated as its own assertion: k4/k5 sharing only
    # "single share template" contributes 2 of those 5 singletons, not a
    # 2-key cluster.
    assert p2["size_histogram"]["2"] == 0


def test_anti_chaining_fix_exposed_directly_on_cluster_sizes():
    # The load-bearing test: a genuine single-shared-template chain
    # (ka-kb share one template, kb-kc share a DIFFERENT one, ka-kc share
    # none) must NOT collapse into one cluster at min_shared=2, but WOULD
    # at min_shared=1 via transitivity through kb -- exactly the false
    # giant-component behavior the 2-shared-template threshold exists to
    # prevent. Exercised directly on _cluster_sizes with placeholder keys,
    # isolating the union-find logic from the rest of the pipeline.
    keys = ["ka", "kb", "kc"]
    pair_counts = {("ka", "kb"): 1, ("kb", "kc"): 1}

    chained = _cluster_sizes(keys, pair_counts, threshold=1)
    assert sorted(chained) == [3]  # ka, kb, kc all collapse into one cluster

    not_chained = _cluster_sizes(keys, pair_counts, threshold=2)
    assert sorted(not_chained) == [1, 1, 1]  # no pair meets the threshold


def test_promiscuous_template_keys_are_not_clustered(tmp_path):
    data_dir = _setup(tmp_path)
    stats = _compute(data_dir, min_shared=2)

    # k10-k13's only shared template exceeds --cap and is excluded from
    # linkage entirely -- they never even enter keys_in_bounded_templates,
    # let alone a cluster. If they had leaked in as 4 more singletons,
    # keys_in_bounded_templates_count would be 12, not 8, and the
    # min_shared=2 pass's cluster_count would be 10, not 6.
    assert stats["keys_in_bounded_templates_count"] == 8
    p2 = _pass_for(stats, 2)
    assert p2["cluster_count"] == 6


def test_no_did_string_anywhere_in_json_output(tmp_path):
    data_dir = _setup(tmp_path)
    stats = _compute(data_dir)
    dumped = json.dumps(stats)

    for did in ALL_DIDS:
        assert did not in dumped
    assert "did:key:" not in dumped


def test_output_shape_carries_no_membership_field(tmp_path):
    # Adversarial: walk the returned dict's shape and confirm it contains
    # ONLY the documented count/size/histogram/threshold fields -- nothing
    # that could be a membership list, a representative key, or any other
    # channel for key identity.
    data_dir = _setup(tmp_path)
    stats = _compute(data_dir)

    assert set(stats.keys()) == {
        "room",
        "cap",
        "min_shared",
        "messages_file_found",
        "signed_checked",
        "signed_reverified",
        "signed_reverify_failed",
        "malformed_lines_skipped",
        "distinct_keys_overall",
        "distinct_shared_templates",
        "bounded_template_count",
        "excluded_promiscuous_template_count",
        "keys_in_bounded_templates_count",
        "passes",
        "coverage_captured_total",
        "coverage_dropped_total",
        "coverage_ratio",
    }
    for entry in stats["passes"]:
        assert set(entry.keys()) == {
            "min_shared",
            "cluster_count",
            "multi_key_cluster_count",
            "singleton_count",
            "largest_cluster_size",
            "size_histogram",
        }
        assert set(entry["size_histogram"].keys()) == set(HISTOGRAM_BUCKETS)
        for value in entry["size_histogram"].values():
            assert isinstance(value, int)


def test_output_values_are_never_key_shaped(tmp_path):
    # A stronger adversarial pass: recursively walk every leaf value in
    # the entire returned structure and confirm none of them is a string
    # at all (every leaf in a correct output is an int, a float, a bool,
    # or None -- "room" and the histogram bucket labels are the only
    # strings, and those are asserted separately, by name, above). If a
    # membership list or a stray DID ever leaked into this structure, it
    # would necessarily show up as a string leaf that is not one of the
    # known label fields.
    data_dir = _setup(tmp_path)
    stats = _compute(data_dir)

    known_string_fields = {"room"}

    def walk(obj, path):
        if isinstance(obj, dict):
            for key, value in obj.items():
                walk(value, path + (key,))
        elif isinstance(obj, list):
            for item in obj:
                walk(item, path)
        else:
            if isinstance(obj, str):
                field_name = path[-1] if path else None
                assert field_name in known_string_fields or field_name in HISTOGRAM_BUCKETS or obj in HISTOGRAM_BUCKETS, (
                    f"unexpected string leaf at {path}: {obj!r}"
                )

    walk(stats, ())


def test_coverage_surfaced(tmp_path):
    data_dir = tmp_path / "data"
    _write_messages(str(data_dir / "rooms" / ROOM / "messages.jsonl"), _build_records())
    coverage_state = {ROOM: {"captured_total": 50, "dropped_total": 5}}
    os.makedirs(str(data_dir), exist_ok=True)
    with open(data_dir / "coverage_state.json", "w", encoding="utf-8") as f:
        json.dump(coverage_state, f)

    stats = _compute(str(data_dir))

    assert stats["coverage_captured_total"] == 50
    assert stats["coverage_dropped_total"] == 5
    assert stats["coverage_ratio"] == 50 / 55


def test_report_contains_required_language_and_no_dids(tmp_path):
    data_dir = _setup(tmp_path)
    stats = _compute(data_dir)
    report = format_report(stats)

    assert "FLOOR" in report
    assert "heartbeat-style posting" in report
    assert "not a verdict about any poster" in report
    assert "never a proof of a single operator" in report
    assert "checked" in report and "re-verified" in report and "failed" in report
    for did in ALL_DIDS:
        assert did not in report


def test_missing_messages_file_does_not_crash(tmp_path):
    data_dir = tmp_path / "empty_data"
    os.makedirs(str(data_dir), exist_ok=True)
    stats = compute_clustering_stats(str(data_dir), room="lobby")
    assert stats["messages_file_found"] is False
    assert stats["signed_checked"] == 0
    report = format_report(stats)
    assert "No messages.jsonl found" in report


def test_invalid_room_is_rejected(tmp_path):
    data_dir = tmp_path / "data"
    os.makedirs(str(data_dir), exist_ok=True)
    with pytest.raises(ValueError):
        compute_clustering_stats(str(data_dir), room="../../escaped")
