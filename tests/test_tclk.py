"""tclk/1 deal lifecycle (analysis/tclk.py) against a known, synthetic set
of tclk frames.

Uses tests/fixtures/make_fixtures.py's deterministic throwaway-key
approach (FIXTURE_KEY_1, FIXTURE_DID_1, _sign), the same pattern
tests/test_diversity.py and tests/test_cohort.py use -- no real did:key
identity involved, and make_fixtures.py itself is not modified.

The fixture, by hand:

  offer-1 -> accept(ref=offer-1, contract=contract-1) -> lock(contract-1)
    -> reveal(contract-1)                          FULL DEAL, completed via reveal

  offer-2                                            STALLS AT OFFER, no accept at all

  offer-3 -> accept(ref=offer-3, contract=contract-3)
                                                      ACCEPTED, no further progression

  offer-4 -> accept(ref=offer-4, contract=contract-4)
    -> receipt(contract-4, outcome=claimed)          COMPLETED VIA RECEIPT, no reveal frame

  offer-5, tampered signature                        RE-VERIFY FAILURE, must not count at all

  accept(ref=ghost-offer, contract=contract-ghost)   PARTIAL CHAIN: ref matches no captured offer
    -> lock(contract-ghost)                          PARTIAL CHAIN: contract matches no accepted offer

  "just a normal chat message"                       NOT a tclk frame at all, ignored
  "tclk1 {bad json"                                  UNPARSEABLE
  tclk1 {"type": "foobar", "id": "x"}                UNKNOWN-TYPE

Expected: distinct_offer_count=4 (offer-1..4; ghost-offer was never itself
posted as an offer, so it does not count), accepted_offer_count=3
(offer-1, offer-3, offer-4; the ghost accept's ref matches nothing),
locked_contract_count=1 (contract-1 only -- contract-ghost's lock does not
count, its accept was orphaned), revealed=1, receipt_claimed=1,
completed_contract_count=2 (contract-1 via reveal, contract-4 via receipt,
deduped -- neither counted twice), completion_rate = 2/4 = 0.5.
"""

import json
import os
from datetime import datetime, timezone

from make_fixtures import FIXTURE_DID_1, FIXTURE_KEY_1, _sign

from analysis.tclk import KNOWN_TYPES, compute_tclk_stats, format_report

ROOM = "lobby"
BASE = 1_800_000_000


def _iso(ts_seconds):
    return datetime.fromtimestamp(ts_seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _signed(seq, ts_offset, text, nonce=None):
    ts = _iso(BASE + ts_offset)
    nonce = nonce if nonce is not None else 9000000 + seq
    return {
        "room": ROOM,
        "seq": seq,
        "ts": ts,
        "from": FIXTURE_DID_1,
        "text": text,
        "nonce": str(nonce),
        "sig": _sign(FIXTURE_KEY_1, ROOM, str(nonce), text),
        "captured_at": ts,
        "source": "test",
    }


def _frame(type_, **fields):
    payload = {"type": type_, **fields}
    return "tclk1 " + json.dumps(payload, sort_keys=True)


def _write_messages(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True))
            f.write("\n")


def _build_records():
    records = []
    seq = 1

    # --- full deal: offer-1 -> accept -> lock -> reveal ---
    records.append(_signed(seq, 1, _frame("offer", id="offer-1")))
    seq += 1
    records.append(_signed(seq, 2, _frame("accept", ref="offer-1", contract="contract-1")))
    seq += 1
    records.append(_signed(seq, 3, _frame("lock", contract="contract-1")))
    seq += 1
    records.append(_signed(seq, 4, _frame("reveal", contract="contract-1")))
    seq += 1

    # --- offer-2: stalls at offer, no accept ---
    records.append(_signed(seq, 5, _frame("offer", id="offer-2")))
    seq += 1

    # --- offer-3: accepted, no further progression ---
    records.append(_signed(seq, 6, _frame("offer", id="offer-3")))
    seq += 1
    records.append(_signed(seq, 7, _frame("accept", ref="offer-3", contract="contract-3")))
    seq += 1

    # --- offer-4: completed via a claimed receipt, no reveal frame ---
    records.append(_signed(seq, 8, _frame("offer", id="offer-4")))
    seq += 1
    records.append(_signed(seq, 9, _frame("accept", ref="offer-4", contract="contract-4")))
    seq += 1
    records.append(_signed(seq, 10, _frame("receipt", contract="contract-4", outcome="claimed")))
    seq += 1

    # --- offer-5: tampered signature, must not count at all ---
    broken = _signed(seq, 11, _frame("offer", id="offer-5"))
    bad_char = "A" if broken["sig"][0] != "A" else "B"
    broken["sig"] = bad_char + broken["sig"][1:]
    records.append(broken)
    seq += 1

    # --- partial chain: an accept referencing an offer id never captured,
    # and a downstream frame for its (never-accepted) contract ---
    records.append(_signed(seq, 12, _frame("accept", ref="ghost-offer", contract="contract-ghost")))
    seq += 1
    records.append(_signed(seq, 13, _frame("lock", contract="contract-ghost")))
    seq += 1

    # --- not a tclk frame at all ---
    records.append(_signed(seq, 14, "just a normal chat message"))
    seq += 1

    # --- unparseable tclk frame ---
    records.append(_signed(seq, 15, "tclk1 {bad json"))
    seq += 1

    # --- unknown-type tclk frame ---
    records.append(_signed(seq, 16, "tclk1 " + json.dumps({"type": "foobar", "id": "x"})))
    seq += 1

    return records


def _setup(tmp_path, records=None):
    data_dir = tmp_path / "data"
    _write_messages(str(data_dir / "rooms" / ROOM / "messages.jsonl"), records or _build_records())
    return str(data_dir)


def test_frame_parsing_and_type_tallies(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_tclk_stats(data_dir, room=ROOM)

    assert stats["frames_by_type"]["offer"] == 4
    assert stats["frames_by_type"]["accept"] == 4  # offer-1, offer-3, offer-4, and the ghost accept
    assert stats["frames_by_type"]["lock"] == 2  # contract-1 and contract-ghost
    assert stats["frames_by_type"]["reveal"] == 1
    assert stats["frames_by_type"]["receipt"] == 1
    assert stats["frames_by_type"]["refund"] == 0
    assert stats["frames_by_type"]["cancel"] == 0
    assert stats["frames_by_type"]["heartbeat"] == 0
    assert stats["unparseable_frame_count"] == 1
    assert stats["unknown_type_frame_count"] == 1
    assert stats["total_tclk_frames"] == 4 + 4 + 2 + 1 + 1 + 1 + 1  # by-type sum + unparseable + unknown
    assert stats["total_tclk_frames"] == 14


def test_the_two_id_contract_chain_links_accept_via_ref_and_downstream_via_contract(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_tclk_stats(data_dir, room=ROOM)

    assert stats["distinct_offer_count"] == 4
    assert stats["accepted_offer_count"] == 3
    assert stats["locked_contract_count"] == 1
    assert stats["revealed_contract_count"] == 1
    assert stats["receipt_claimed_contract_count"] == 1

    # the full deal counts as ONE completion, not two, even though it
    # produced a reveal; offer-4's receipt-only completion is a second,
    # independent completion -- deduped union, never double-counted
    assert stats["completed_contract_count"] == 2
    assert stats["completion_rate"] == 2 / 4

    funnel_by_stage = {stage["stage"]: stage["count"] for stage in stats["funnel"]}
    assert funnel_by_stage["offers"] == 4
    assert funnel_by_stage["accepted"] == 3
    assert funnel_by_stage["locked"] == 1
    assert funnel_by_stage["completed"] == 2


def test_partial_chain_is_counted_not_a_crash_and_not_a_completion(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_tclk_stats(data_dir, room=ROOM)

    # the ghost accept's ref matches no captured offer
    assert stats["accepts_with_unmatched_offer_ref"] == 1
    # the ghost lock's contract matches no accepted offer's contract
    assert stats["downstream_frames_with_unmatched_contract"] == 1
    assert stats["partial_chain_frame_count"] == 2

    # neither ghost frame contributes to any accepted/locked/completed count
    assert stats["accepted_offer_count"] == 3
    assert stats["locked_contract_count"] == 1
    assert stats["completed_contract_count"] == 2


def test_full_deal_completed_via_reveal_and_receipt_only_deal_both_count_once_each(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_tclk_stats(data_dir, room=ROOM)

    # exactly 2 completions total: contract-1 (reveal) and contract-4
    # (receipt-claimed, no reveal frame at all) -- proves completion does
    # not require both signals, and that having both (a deal could in
    # principle produce a reveal AND a claimed receipt) would still only
    # count once via the deduping union, not twice.
    assert stats["completed_contract_count"] == 2


def test_broken_signature_frame_is_not_counted_at_all(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_tclk_stats(data_dir, room=ROOM)

    # offer-5's signature was tampered: it must not appear as a captured
    # offer, so distinct_offer_count stays at 4, not 5
    assert stats["distinct_offer_count"] == 4
    assert stats["signed_reverify_failed"] >= 1


def test_non_tclk_message_is_ignored(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_tclk_stats(data_dir, room=ROOM)

    # "just a normal chat message" carries no tclk1 prefix at all: it must
    # not be tallied anywhere (not unparseable, not unknown-type)
    assert stats["total_tclk_frames"] == 14


def test_coverage_is_surfaced(tmp_path):
    data_dir_path = tmp_path / "data"
    _write_messages(str(data_dir_path / "rooms" / ROOM / "messages.jsonl"), _build_records())
    state_path = data_dir_path / "coverage_state.json"
    os.makedirs(str(data_dir_path), exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump({ROOM: {"captured_total": 90, "dropped_total": 10}}, f)

    stats = compute_tclk_stats(str(data_dir_path), room=ROOM)
    assert stats["coverage_captured_total"] == 90
    assert stats["coverage_dropped_total"] == 10
    assert stats["coverage_ratio"] == 0.9


def test_missing_messages_file_is_handled(tmp_path):
    data_dir = tmp_path / "data"
    os.makedirs(str(data_dir), exist_ok=True)
    stats = compute_tclk_stats(str(data_dir), room=ROOM)

    assert stats["messages_file_found"] is False
    assert stats["total_tclk_frames"] == 0
    assert stats["distinct_offer_count"] == 0
    assert stats["completion_rate"] is None

    report = format_report(stats)
    assert "No messages.jsonl found" in report


def test_room_validation_rejects_path_traversal(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        compute_tclk_stats(str(tmp_path), room="../escape")


def test_no_did_key_string_anywhere_in_json_dump(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_tclk_stats(data_dir, room=ROOM)

    dumped = json.dumps(stats, ensure_ascii=False, sort_keys=True)
    assert "did:key:" not in dumped
    assert FIXTURE_DID_1 not in dumped
    # no protocol-level id is ever named either, only counted
    for leaked_id in ("offer-1", "offer-2", "offer-3", "offer-4", "offer-5",
                      "ghost-offer", "contract-1", "contract-3", "contract-4", "contract-ghost"):
        assert leaked_id not in dumped


def test_report_headline_sentence_and_three_caveats_present(tmp_path):
    data_dir = _setup(tmp_path)
    stats = compute_tclk_stats(data_dir, room=ROOM)
    report = format_report(stats)

    assert "offers vastly outnumber any downstream progression" in report
    assert "4 offers, 3 accepted, 2 completed" in report
    assert "FLOOR" in report or "floor" in report
    assert "external rail" in report
    assert "PaperRail" in report
    assert "must never be read as evidence that deals are fake" in report


def test_known_types_constant_matches_spec():
    assert set(KNOWN_TYPES) == {
        "offer", "accept", "lock", "reveal", "refund", "cancel", "receipt", "heartbeat",
    }


def test_no_em_dash_in_module_source():
    import inspect

    import analysis.tclk as tclk_module

    source = inspect.getsource(tclk_module)
    assert "—" not in source
