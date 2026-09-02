"""The message and snapshot cadences fire independently, off a monotonic
clock, at the configured ratio -- not tied together as one shared interval.
"""

from helpers import FakeClient

from collector.config import Config
from collector.core import Collector


class FakeClock:
    """A monotonic clock + sleep_fn pair where sleeping is exactly what
    advances the clock -- no real wall time involved, so a multi-thousand-
    second simulated span costs nothing to run.
    """

    def __init__(self, start=0.0):
        self.now = start
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _empty_page(room):
    return {"room": room, "count": 0, "first_seq": None, "last_seq": 0, "generation": 0, "messages": []}


def _make_collector(tmp_path, message_interval, snapshot_interval, rooms=("lobby",)):
    client = FakeClient(
        rooms_overview={"rooms": [], "total": 0},
        room_pages={"events": _empty_page("events"), **{r: _empty_page(r) for r in rooms}},
    )
    config = Config(
        data_dir=str(tmp_path),
        rooms=list(rooms),
        message_interval=message_interval,
        snapshot_interval=snapshot_interval,
    )
    return Collector(client, config), client


def _count_calls(client, kind, room=None):
    if kind == "rooms_overview":
        return sum(1 for c in client.calls if c[0] == "rooms_overview")
    return sum(1 for c in client.calls if c[0] == "room_page" and (room is None or c[1] == room))


def test_message_cadence_fires_every_tick_it_is_due(tmp_path):
    # message_interval == snapshot_interval == 0 means both are "always
    # due": every tick should fire both. Sanity check on the due-check
    # itself before testing the interesting ratio case below.
    collector, client = _make_collector(tmp_path, message_interval=0, snapshot_interval=0)
    clock = FakeClock()

    collector.run_loop(stop_after=5, sleep_fn=clock.sleep, monotonic_fn=clock.monotonic)

    assert _count_calls(client, "room_page", "lobby") == 5
    assert _count_calls(client, "rooms_overview") == 5


def test_cadences_fire_independently_at_configured_ratio(tmp_path):
    message_interval = 5.0
    snapshot_interval = 300.0
    collector, client = _make_collector(
        tmp_path, message_interval=message_interval, snapshot_interval=snapshot_interval
    )
    clock = FakeClock()

    simulated_span = 3000.0  # 10 full snapshot cycles, 600 full message cycles
    # The loop's sleep is capped at ~1s, so ticks approximately track
    # wall-clock seconds once nothing is due; give it comfortable headroom.
    stop_after = int(simulated_span) + 20

    collector.run_loop(stop_after=stop_after, sleep_fn=clock.sleep, monotonic_fn=clock.monotonic)

    assert clock.now >= simulated_span  # the simulated span was actually covered

    message_fires = _count_calls(client, "room_page", "events")
    snapshot_fires = _count_calls(client, "rooms_overview")

    # Fires at t = 0, interval, 2*interval, ... up to the last tick at or
    # before clock.now. +/-1 tolerance for the exact tick clock.now lands on.
    expected_message_fires = int(clock.now // message_interval) + 1
    expected_snapshot_fires = int(clock.now // snapshot_interval) + 1

    assert abs(message_fires - expected_message_fires) <= 1
    assert abs(snapshot_fires - expected_snapshot_fires) <= 1

    # The headline requirement: message drains run far more often than
    # snapshots, at (approximately) the configured ratio.
    ratio = message_fires / snapshot_fires
    expected_ratio = snapshot_interval / message_interval  # 60
    assert expected_ratio * 0.9 <= ratio <= expected_ratio * 1.15

    # Every room fetch (lobby) happens exactly alongside every events fetch
    # -- the message cadence moves both together.
    assert _count_calls(client, "room_page", "lobby") == message_fires


def test_message_due_is_never_delayed_more_than_one_snapshot_call(tmp_path):
    """When both cadences are due on the same tick, the message drain must
    run before the snapshot -- so a snapshot in flight can delay a due
    message drain by at most that one call, never queue behind more.
    """
    call_order = []

    class OrderTrackingClient:
        def get_rooms_overview(self, timeout=None, max_attempts=None, backoff_cap=None):
            call_order.append("snapshot")
            return {"rooms": [], "total": 0}

        def get_room_page(
            self, room, since=0, wait=0, limit=None, timeout=None, max_attempts=None, backoff_cap=None
        ):
            call_order.append(f"message:{room}")
            return _empty_page(room)

    config = Config(data_dir=str(tmp_path), rooms=["lobby"], message_interval=5, snapshot_interval=5)
    from collector.core import Collector as _Collector

    collector = _Collector(OrderTrackingClient(), config)
    clock = FakeClock()

    collector.run_loop(stop_after=1, sleep_fn=clock.sleep, monotonic_fn=clock.monotonic)

    # Both were due on tick 1 (t=0); message work must be issued first.
    assert call_order[0].startswith("message:")
    assert "snapshot" in call_order
    assert call_order.index("snapshot") > call_order.index("message:events")
