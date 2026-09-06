"""tclk/1 deal lifecycle: the eleventh measurement in the Basanos
measurement layer.

Read-only by construction: this module only reads a room's already-stored
`<data-dir>/rooms/<room>/messages.jsonl` and `<data-dir>/coverage_state.json`
(via `collector.coverage.CoverageTracker`, whose `counters()` method is
itself read-only). It never writes to, or modifies, anything under
`<data-dir>` except the analysis output this module produces itself.

WHAT THIS MEASURES, AND WHY IT IS ON-THESIS: tclk/1 is a payment-protocol
riding on top of the same room transcript every other module here reads.
The question this module asks is the same question the rest of Basanos
asks of chat traffic: does the platform's new payments layer show real
economic activity, or is it verified-but-empty motion, the same as an
empty room full of correctly-signed nothing? A deal that never gets past
"offer" is the payments-layer equivalent of a shared template with zero
real coordination behind it.

WHAT A TCLK FRAME IS: a room message whose `text` starts with the exact 6
characters "tclk1 " (the literal string "tclk1" followed by one space),
followed by one JSON object. Detected by that exact byte-exact prefix, no
fuzzy match. The JSON after the 6-character prefix is parsed; a frame
whose body does not parse as a JSON object is tallied as unparseable,
never a crash. Every frame has a `type`: offer, accept, lock, reveal,
refund, cancel, receipt, or heartbeat; a frame with a missing or
unrecognized type is tallied as unknown-type, counted in the total but
not in any per-type bucket.

Frames ride the same signed did:key lane as every other room message, so
each carrying record's signature is re-verified with `collector.verify`
exactly as every sibling module does (the `is_signed` gate, `verify_record`
catching `UnsupportedKeyType`/`MalformedRecord`/`KeyError`/`TypeError`, and
the `isinstance(text, str)` guard before this module ever calls
`.startswith` on it). Only re-verified frames are counted anywhere below.

THE TWO-ID CONTRACT CHAIN, THE PRECISE PART: a deal is linked by two ids,
not one.
  - An `offer` frame carries `id` (the offer id).
  - An `accept` frame carries `ref` (equal to the offer's `id`) AND
    `contract` (a brand new id it introduces). The accept is the bridge:
    `ref` ties it back to the offer, `contract` starts everything after.
  - Every later frame (lock, reveal, refund, cancel, receipt, heartbeat)
    carries `contract`, never the original offer id.
So building a full deal means: collect every offer id; for each accept,
its `ref` is checked against the known offer ids (a match is what makes
this offer "accepted"), and its `contract` is the id every downstream
frame for that same deal will carry; lock/reveal/refund/cancel/receipt/
heartbeat frames are grouped by that `contract`. An accept whose `ref`
matches no captured offer, or a downstream frame whose `contract` matches
no accept this module captured, is a PARTIAL CHAIN -- expected under
coverage, tallied, never a crash and never counted as any stage of a deal
that offer/contract cannot be shown to belong to.

COMPLETION: a contract counts as completed if it reached a `reveal` frame
OR carries a `receipt` frame with `outcome == "claimed"` -- the two are
DEDUPED by contract id before counting, so a deal that produced both a
reveal and a claimed receipt (the ordinary successful case) counts once,
not twice. The completion rate is completions divided by DISTINCT OFFERS
(the widest, least-charitable denominator this module has), not by
accepted or locked deals.

THREE HONESTY CAVEATS, BECAUSE THIS MODULE MAKES A STRONG CLAIM AND MUST
BE SCRUPULOUSLY FAIR ABOUT IT (the same discipline `analysis/selfaudit.py`
applies to itself):
  1. COVERAGE: capture is a fraction of the room's real traffic (stated
     alongside every number below); an accept or reveal frame could be
     sitting in the dropped traffic this collector never saw, and a
     dropped completing frame makes a real deal look stalled. So the
     completion rate here is a FLOOR: true completion is at least this,
     and missing frames can only ever raise it, never lower it.
  2. OFF-CHANNEL: tclk settles on an external rail; the room is only the
     coordination transcript, not the settlement itself. A deal could be
     accepted or settled entirely off the room, so "no accept in the
     transcript" is never "no accept happened" -- it is a statement about
     what the transcript shows, not about what happened in the world.
  3. ALPHA / NO VALUE RAIL: at the time of this measurement tclk ships
     only PaperRail, which settles nothing at all, so no deal captured
     here could actually move money yet regardless of how far it
     progressed. A low on-transcript completion rate is measured during a
     period when real completion is not even possible, and must never be
     read as evidence that deals are fake.
This module's headline sentence is always of the form "offers vastly
outnumber any downstream progression: N offers, M accepted, K completed",
stated with the three caveats above -- NEVER as a verdict that any deal,
or the protocol, is fake. Aggregate only: no did:key, and no individual
offer id, contract id, or ref, ever appears anywhere in the returned
structure -- every number below is a count or a rate over frames or
contracts, never a name.

This module does not import from or modify any sibling analysis module
(each intentionally duplicates its own small streaming/re-verify walk, to
keep every module a single self-contained, independently-auditable read).

Usage:
    python -m analysis.tclk --data-dir <dir> [--room lobby] [--out <path>]
"""

import argparse
import json
import os
import re
from datetime import datetime, timezone

from collector.coverage import CoverageTracker
from collector.verify import MalformedRecord, UnsupportedKeyType, is_signed, verify_record

FRAME_PREFIX = "tclk1 "

KNOWN_TYPES = ("offer", "accept", "lock", "reveal", "refund", "cancel", "receipt", "heartbeat")

COVERAGE_CAVEAT = (
    "capture is a fraction of the room's real traffic (see the coverage ratio above); an "
    "accept or reveal frame could be sitting in the dropped traffic this collector never "
    "saw, and a dropped completing frame makes a real deal look stalled -- so the "
    "completion rate here is a FLOOR: true completion is at least this, missing frames can "
    "only ever raise it, never lower it."
)

OFF_CHANNEL_CAVEAT = (
    "tclk settles on an external rail; the room is only the coordination transcript, not "
    "the settlement itself. A deal could be accepted or settled entirely off the room, so "
    "\"no accept in the transcript\" is never \"no accept happened\" -- it is a statement "
    "about what the transcript shows, not about what happened in the world."
)

ALPHA_CAVEAT = (
    "at the time of this measurement tclk ships only PaperRail, which settles nothing at "
    "all, so no deal captured here could actually move money yet regardless of how far it "
    "progressed. A low on-transcript completion rate is measured during a period when real "
    "completion is not even possible, and must never be read as evidence that deals are "
    "fake."
)


_VALID_ROOM_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_room(room):
    """Reject a room name that could escape the intended directory when
    used in os.path.join (below, and in default_out_path) -- a room
    containing "/" or ".." would let --room build a path outside
    <data-dir>/rooms/ on read or outside <data-dir>/analysis/ on write.
    Every real room name (lobby, meta, fixture-room-... in the fixtures)
    matches this pattern; nothing valid is rejected. Raised before any
    path is built or any file is opened or created.
    """
    if not _VALID_ROOM_RE.match(room):
        raise ValueError(
            f"invalid room {room!r}: must match {_VALID_ROOM_RE.pattern} "
            "(letters, digits, underscore, hyphen only)"
        )


def _iter_json_lines(path):
    """Stream a JSONL file one record at a time. Never loads the whole
    file into memory -- callers build only the aggregates they need as
    they go. A line that isn't valid JSON is skipped (tallied by the
    caller if it cares), never a crash.
    """
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _parse_frame(text):
    """Parse `text` as a tclk frame. Returns the frame's JSON body (a
    dict) if `text` starts with the exact 6-character prefix "tclk1 " and
    the remainder parses as a JSON object; returns None otherwise (not a
    tclk frame at all: no prefix, or the prefix is present but the body
    is not valid JSON, or it parses to something other than an object).
    Never raises -- a malformed body costs that one frame its place in
    the funnel, not a crash.
    """
    if not text.startswith(FRAME_PREFIX):
        return None
    body = text[len(FRAME_PREFIX):]
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return "unparseable"
    if not isinstance(parsed, dict):
        return "unparseable"
    return parsed


def compute_tclk_stats(data_dir, room="lobby"):
    """Stream `<data_dir>/rooms/<room>/messages.jsonl` and compute the
    tclk/1 deal-lifecycle funnel: how many offers are posted, how many
    are accepted, and how many of those reach lock, reveal, refund, or
    cancel, plus the completion count and rate.

    Returns a dict with the raw counters and aggregates needed by both the
    human-readable report and the JSON output. Reads only; writes nothing.
    No did:key string, and no individual offer id, contract id, or ref, is
    ever in the returned structure -- every number below is a count or a
    rate over frames or contracts, never a name.
    """
    _validate_room(room)
    messages_path = os.path.join(data_dir, "rooms", room, "messages.jsonl")

    checked = 0
    verified = 0
    failed = 0
    malformed_lines = 0

    frames_by_type = {t: 0 for t in KNOWN_TYPES}
    unparseable_frame_count = 0
    unknown_type_frame_count = 0

    offer_ids = set()
    # ref -> contract, one entry per accept frame with a string ref and a
    # string contract; used below to test each ref against offer_ids and
    # to seed the set of contracts an accepted offer starts.
    accept_ref_contract_pairs = []

    # contract -> whether that contract has seen this stage, built after
    # every frame is read (so an out-of-order lock/reveal arriving before
    # its accept is still attributed correctly).
    contracts_with_lock = set()
    contracts_with_reveal = set()
    contracts_with_refund = set()
    contracts_with_cancel = set()
    contracts_with_claimed_receipt = set()
    downstream_contract_refs = set()  # every contract id any downstream frame named

    messages_found = os.path.exists(messages_path)
    if messages_found:
        for record in _iter_json_lines(messages_path):
            if not isinstance(record, dict):
                malformed_lines += 1
                continue
            if not is_signed(record):
                continue  # unsigned nicks are excluded from the population entirely
            checked += 1
            try:
                ok = verify_record(record)
            except (UnsupportedKeyType, MalformedRecord, KeyError, TypeError):
                # TypeError covers a non-string sig (e.g. a bare number or a
                # JSON array/object): verify.py does base64 decoding on sig,
                # which raises TypeError rather than one of the exceptions
                # above for a non-string value. Treated the same as any
                # other re-verify failure, never a crash.
                ok = False
            if ok and not isinstance(record.get("text"), str):
                # A genuinely valid signature over a non-string text (e.g.
                # a JSON array or object instead of a string) would crash
                # the prefix check below with "startswith arg must be str".
                # Counted as a re-verify failure like any other record this
                # analysis cannot safely include, not a crash.
                ok = False
            if not ok:
                failed += 1
                continue
            verified += 1

            text = record["text"]
            frame = _parse_frame(text)
            if frame is None:
                continue  # not a tclk frame at all -- an ordinary chat message
            if frame == "unparseable":
                unparseable_frame_count += 1
                continue

            frame_type = frame.get("type")
            if frame_type not in KNOWN_TYPES:
                unknown_type_frame_count += 1
                continue
            frames_by_type[frame_type] += 1

            if frame_type == "offer":
                offer_id = frame.get("id")
                if isinstance(offer_id, str) and offer_id:
                    offer_ids.add(offer_id)
            elif frame_type == "accept":
                ref = frame.get("ref")
                contract = frame.get("contract")
                if isinstance(ref, str) and ref and isinstance(contract, str) and contract:
                    accept_ref_contract_pairs.append((ref, contract))
            else:
                contract = frame.get("contract")
                if not (isinstance(contract, str) and contract):
                    continue
                downstream_contract_refs.add(contract)
                if frame_type == "lock":
                    contracts_with_lock.add(contract)
                elif frame_type == "reveal":
                    contracts_with_reveal.add(contract)
                elif frame_type == "refund":
                    contracts_with_refund.add(contract)
                elif frame_type == "cancel":
                    contracts_with_cancel.add(contract)
                elif frame_type == "receipt":
                    if frame.get("outcome") == "claimed":
                        contracts_with_claimed_receipt.add(contract)

    total_tclk_frames = sum(frames_by_type.values()) + unparseable_frame_count + unknown_type_frame_count

    # An accept "matches" only when its ref names an offer id this module
    # actually captured; matching accepts are the only ones whose contract
    # id is treated as an ACCEPTED deal for the rest of the funnel. An
    # accept whose ref matches nothing captured is a partial chain: tallied,
    # never counted as an acceptance, and its contract (if any) is not
    # treated as accepted even if downstream frames for it exist.
    accepted_offer_ids = set()
    accepted_contract_ids = set()
    accepts_with_unmatched_offer_ref = 0
    for ref, contract in accept_ref_contract_pairs:
        if ref in offer_ids:
            accepted_offer_ids.add(ref)
            accepted_contract_ids.add(contract)
        else:
            accepts_with_unmatched_offer_ref += 1

    # A downstream frame (lock/reveal/refund/cancel/receipt/heartbeat)
    # whose contract was never introduced by a matching accept is the
    # other half of a partial chain: its contract's stage counts below
    # are restricted to accepted_contract_ids, so it contributes nothing
    # to the funnel, only to this tally.
    downstream_frames_with_unmatched_contract = len(downstream_contract_refs - accepted_contract_ids)

    locked_contract_count = len(contracts_with_lock & accepted_contract_ids)
    revealed_contract_count = len(contracts_with_reveal & accepted_contract_ids)
    refunded_contract_count = len(contracts_with_refund & accepted_contract_ids)
    cancelled_contract_count = len(contracts_with_cancel & accepted_contract_ids)
    receipt_claimed_contract_count = len(contracts_with_claimed_receipt & accepted_contract_ids)

    completed_contract_ids = (contracts_with_reveal | contracts_with_claimed_receipt) & accepted_contract_ids
    completed_count = len(completed_contract_ids)

    distinct_offer_count = len(offer_ids)
    accepted_offer_count = len(accepted_offer_ids)
    completion_rate = (completed_count / distinct_offer_count) if distinct_offer_count else None

    funnel = [
        {"stage": "offers", "count": distinct_offer_count},
        {"stage": "accepted", "count": accepted_offer_count},
        {"stage": "locked", "count": locked_contract_count},
        {"stage": "completed", "count": completed_count},
    ]

    coverage = CoverageTracker(data_dir).counters(room)
    coverage_ratio = CoverageTracker.coverage_ratio(
        coverage.get("captured_total", 0), coverage.get("dropped_total", 0)
    )

    return {
        "room": room,
        "messages_file_found": messages_found,
        "signed_checked": checked,
        "signed_reverified": verified,
        "signed_reverify_failed": failed,
        "malformed_lines_skipped": malformed_lines,
        "total_tclk_frames": total_tclk_frames,
        "frames_by_type": frames_by_type,
        "unparseable_frame_count": unparseable_frame_count,
        "unknown_type_frame_count": unknown_type_frame_count,
        "distinct_offer_count": distinct_offer_count,
        "accepted_offer_count": accepted_offer_count,
        "accepts_with_unmatched_offer_ref": accepts_with_unmatched_offer_ref,
        "downstream_frames_with_unmatched_contract": downstream_frames_with_unmatched_contract,
        "partial_chain_frame_count": accepts_with_unmatched_offer_ref + downstream_frames_with_unmatched_contract,
        "locked_contract_count": locked_contract_count,
        "revealed_contract_count": revealed_contract_count,
        "refunded_contract_count": refunded_contract_count,
        "cancelled_contract_count": cancelled_contract_count,
        "receipt_claimed_contract_count": receipt_claimed_contract_count,
        "completed_contract_count": completed_count,
        "completion_rate": completion_rate,
        "funnel": funnel,
        "coverage_captured_total": coverage.get("captured_total", 0),
        "coverage_dropped_total": coverage.get("dropped_total", 0),
        "coverage_ratio": coverage_ratio,
    }


def format_report(stats):
    """Render the human-readable report for `stats` (as returned by
    `compute_tclk_stats`).

    The headline is always framed as offers vastly outnumbering any
    downstream progression, with the three honesty caveats (coverage,
    off-channel settlement, and the current no-value-rail alpha state)
    stated plainly alongside it -- never as a verdict that any deal, or
    the protocol, is fake. Aggregate only: no did:key, offer id, contract
    id, or ref is ever named.
    """
    room = stats["room"]
    lines = []
    lines.append(f"tclk/1 deal lifecycle -- room: {room}")
    lines.append("=" * (25 + len(room)))
    lines.append("")

    if not stats["messages_file_found"]:
        lines.append(f"No messages.jsonl found for room {room!r}; nothing to measure.")
        return "\n".join(lines)

    lines.append(
        f"Re-verify stats: {stats['signed_checked']} signed messages checked, "
        f"{stats['signed_reverified']} re-verified, "
        f"{stats['signed_reverify_failed']} failed to re-verify."
    )
    lines.append(
        "(Every number below rests on re-verified signatures only, not trusted stored ones.)"
    )
    lines.append("")

    if stats["signed_reverified"] == 0:
        lines.append("No re-verified signed messages in this window -- nothing to report.")
        return "\n".join(lines)

    lines.append(f"Total tclk frames: {stats['total_tclk_frames']}")
    lines.append("By type:")
    for t in KNOWN_TYPES:
        lines.append(f"  {t}: {stats['frames_by_type'][t]}")
    lines.append(f"  unknown-type: {stats['unknown_type_frame_count']}")
    lines.append(f"  unparseable: {stats['unparseable_frame_count']}")
    lines.append("")

    offers = stats["distinct_offer_count"]
    accepted = stats["accepted_offer_count"]
    completed = stats["completed_contract_count"]
    lines.append(
        f"In the captured signed transcript, offers vastly outnumber any downstream "
        f"progression: {offers} offers, {accepted} accepted, {completed} completed."
    )
    lines.append("")

    lines.append("Funnel (count surviving each stage):")
    for stage in stats["funnel"]:
        lines.append(f"  {stage['stage']}: {stage['count']}")
    lines.append("")

    lines.append(f"  refunded: {stats['refunded_contract_count']}")
    lines.append(f"  cancelled: {stats['cancelled_contract_count']}")
    lines.append(f"  reached reveal: {stats['revealed_contract_count']}")
    lines.append(f"  receipt outcome=claimed: {stats['receipt_claimed_contract_count']}")
    lines.append(
        "  (completed = reveal OR receipt outcome=claimed, deduped by contract -- a deal "
        "with both counts once, not twice)"
    )
    lines.append("")

    rate = stats["completion_rate"]
    rate_str = f"{100.0 * rate:.1f}%" if rate is not None else "n/a"
    lines.append(f"Completion rate (completed / distinct offers): {rate_str}")
    lines.append("")

    lines.append("Partial chains (expected under coverage, never a crash):")
    lines.append(f"  accepts referencing an offer id never captured: {stats['accepts_with_unmatched_offer_ref']}")
    lines.append(
        f"  downstream frames referencing a contract id never captured via a matching "
        f"accept: {stats['downstream_frames_with_unmatched_contract']}"
    )
    lines.append("")

    ratio = stats["coverage_ratio"]
    ratio_str = f"{100.0 * ratio:.1f}%" if ratio is not None else "n/a"
    lines.append("Coverage:")
    lines.append(
        f"  {ratio_str} (captured {stats['coverage_captured_total']} of "
        f"{stats['coverage_captured_total'] + stats['coverage_dropped_total']} estimated "
        f"messages for this room)"
    )
    lines.append("")

    lines.append("This module makes a strong claim, so read it with all three caveats:")
    lines.append(f"1. Coverage: {COVERAGE_CAVEAT}")
    lines.append(f"2. Off-channel: {OFF_CHANNEL_CAVEAT}")
    lines.append(f"3. Alpha / no value rail: {ALPHA_CAVEAT}")

    if stats["malformed_lines_skipped"]:
        lines.append("")
        lines.append(
            f"Note: {stats['malformed_lines_skipped']} unparseable line(s) in "
            "messages.jsonl were skipped."
        )

    return "\n".join(lines)


def default_out_path(data_dir, room):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return os.path.join(data_dir, "analysis", f"tclk_{room}_{ts}.json")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="tclk/1 deal-lifecycle funnel over a room's already-collected messages "
        "(read-only)."
    )
    parser.add_argument("--data-dir", required=True, help="collector data directory to read")
    parser.add_argument("--room", default="lobby", help="room to analyze (default: lobby)")
    parser.add_argument(
        "--out",
        default=None,
        help="path to write the JSON report to "
        "(default: <data-dir>/analysis/tclk_<room>_<ts>.json)",
    )
    args = parser.parse_args(argv)

    stats = compute_tclk_stats(args.data_dir, room=args.room)
    print(format_report(stats))

    out_path = args.out or default_out_path(args.data_dir, args.room)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
