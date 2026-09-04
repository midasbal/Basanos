"""--room is used to build filesystem paths (messages_path, and the
default output path) in all four analysis modules with no validation, so
a room containing ".." or "/" could escape <data-dir>/rooms/ on read and
<data-dir>/analysis/ on write (reproduced in the audit: --room
'../../escaped' wrote outside analysis/). Fixed by rejecting any room that
is not a conservative slug (letters, digits, underscore, hyphen) before
any path is built. These tests confirm the rejection happens before any
file is touched, and that valid rooms are completely unaffected.
"""

import os

import pytest

from analysis.coordination import compute_coordination_stats
from analysis.diurnal import compute_diurnal_stats
from analysis.duplication import compute_duplication_stats
from analysis.synchrony import compute_synchrony_stats

COMPUTE_FUNCTIONS = [
    compute_duplication_stats,
    compute_coordination_stats,
    compute_synchrony_stats,
    compute_diurnal_stats,
]

INVALID_ROOMS = [
    "../../escaped",
    "../../../../../../tmp/pwned",
    "lobby/../../escaped",
    "a/b",
]


def _listing_before_and_after(data_dir_parent):
    """A snapshot of every path under and around data_dir_parent, to prove
    nothing was created anywhere as a side effect of a rejected call.
    """
    found = []
    for root, dirs, files in os.walk(data_dir_parent):
        for name in dirs + files:
            found.append(os.path.join(root, name))
    return set(found)


@pytest.mark.parametrize("compute", COMPUTE_FUNCTIONS)
@pytest.mark.parametrize("bad_room", INVALID_ROOMS)
def test_invalid_room_is_rejected_before_any_path_is_touched(tmp_path, compute, bad_room):
    data_dir = tmp_path / "data"
    os.makedirs(str(data_dir), exist_ok=True)

    before = _listing_before_and_after(str(tmp_path))

    with pytest.raises(ValueError):
        compute(str(data_dir), room=bad_room)

    after = _listing_before_and_after(str(tmp_path))
    assert after == before  # nothing created, nowhere, by the rejected call


@pytest.mark.parametrize("compute", COMPUTE_FUNCTIONS)
def test_valid_room_still_works_exactly_as_before(tmp_path, compute):
    data_dir = tmp_path / "data"
    os.makedirs(str(data_dir / "rooms" / "lobby"), exist_ok=True)

    # No messages.jsonl at all is a normal, already-handled case in every
    # module (messages_file_found=False) -- this only proves a valid room
    # reaches that same existing code path instead of being rejected.
    stats = compute(str(data_dir), room="lobby")

    assert stats["messages_file_found"] is False
    assert stats["signed_checked"] == 0
