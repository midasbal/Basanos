"""Nonces store as strings (exact digits), including ones past 2^53, and
verification still works against the stored string form.

Uses the fixture's oversized nonce (message seq 102 in
lobby_page.fixture.json, signed by the throwaway fixture key) -- no real
did:key identity involved.
"""

from helpers import FakeClient, load_fixture

from collector.core import RoomFollower
from collector.storage import read_jsonl
from collector.verify import build_signing_payload, verify_record


def _find_fixture_message_with_big_nonce():
    """The fixture message whose nonce exceeds 2**53 -- past the point a
    JS frontend's JSON.parse would silently round an integer.
    """
    page = load_fixture("lobby_page.fixture.json")
    for m in page["messages"]:
        if m.get("nonce", 0) and m["nonce"] > 2**53:
            return page["room"], m
    raise AssertionError("fixture no longer has a nonce > 2**53 -- update make_fixtures.py")


def test_fixture_big_nonce_exceeds_2_pow_53():
    room, m = _find_fixture_message_with_big_nonce()
    assert m["nonce"] > 2**53
    assert len(str(m["nonce"])) == 19


def test_follower_stores_nonce_as_string_for_fixture_big_nonce_record(tmp_path):
    room, m = _find_fixture_message_with_big_nonce()
    page = {
        "room": room,
        "count": 1,
        "first_seq": m["seq"],
        "last_seq": m["seq"],
        "generation": 0,
        "messages": [m],
    }
    client = FakeClient(room_pages={room: page})
    follower = RoomFollower(client, str(tmp_path), room, source="test")

    result = follower.fetch_and_store()
    assert result["new_count"] == 1

    stored = read_jsonl(follower.messages_path)
    assert len(stored) == 1
    rec = stored[0]

    # Stored as a string, and it's the exact same digits -- no float/JS
    # rounding happened on our side either.
    assert isinstance(rec["nonce"], str)
    assert rec["nonce"] == str(m["nonce"])
    assert rec["nonce"] == "1700000000123456789"


def test_stored_string_nonce_reverifies_byte_identical_to_int_nonce():
    room, m = _find_fixture_message_with_big_nonce()

    # f"{room}|{nonce}|{text}" must be byte-identical whether nonce is the
    # original int or the stored str(int) -- Python ints are arbitrary
    # precision, so building the payload from either produces the same
    # bytes, and the signature (computed once, over one fixed payload)
    # verifies against both.
    payload_from_int = build_signing_payload(room, m["nonce"], m["text"])
    payload_from_str = build_signing_payload(room, str(m["nonce"]), m["text"])
    assert payload_from_int == payload_from_str

    record_with_string_nonce = {
        "room": room,
        "from": m["from"],
        "text": m["text"],
        "nonce": str(m["nonce"]),  # exactly what the collector now stores
        "sig": m["sig"],
    }
    assert verify_record(record_with_string_nonce) is True
