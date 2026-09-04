"""Finding 26 (medium): cross-process fan-out (Redis pub/sub) is lossy by
contract, and neither the server nor the client checked sequence continuity
on a live socket — fanout.py's own docstring claimed "the client already
reconciles gaps against the room event log", which was false: the client
only ever read `lastSequence` forward (web/js/socket.js), never compared a
delivered sequence against what it expected next, so a dropped publish (a
Redis restart, the subscribe loop's backoff window, a failed publish) left
a permanent hole nothing noticed until an unrelated reconnect.

The client's own continuity check (see the new e2e coverage in
tests/e2e/test_fix_realtime_client_gap_resync.py) now sends a
`resync_request` message when it detects a gap. This file covers the
server's half: `RealtimeHub.record_sequence_gap` (called from
websocket.py's `resync_request` handling) is the counter that backs it, and
it is deliberately optional on `HubMetrics` (see the hub.py docstring) so
it stays a no-op rather than an AttributeError against a metrics object
that does not implement it (any test double included).

Round 2 (critic): the resync request itself was proven, but nothing on
`/metrics` counted it — `record_sequence_gap` had nowhere real to land.
`multiplayer.metrics.Metrics` now implements it and renders
`xyzzy_sequence_gaps_total`; `test_a_client_resync_request_increments_the_metrics_endpoint`
below scrapes the real `/metrics` endpoint end to end.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from multiplayer.realtime.hub import RealtimeHub
from multiplayer.server import create_app

TOKENS = {"owner-token": "owner"}
OWNER = {"Authorization": "Bearer owner-token"}


class _FakeGapMetrics:
    def __init__(self) -> None:
        self.gaps = 0

    def record_sequence_gap(self) -> None:
        self.gaps += 1


async def test_record_sequence_gap_calls_an_optional_metrics_hook() -> None:
    metrics = _FakeGapMetrics()
    hub = RealtimeHub(metrics=metrics)  # type: ignore[arg-type]

    hub.record_sequence_gap()
    hub.record_sequence_gap()

    assert metrics.gaps == 2


async def test_record_sequence_gap_is_a_silent_no_op_without_the_hook() -> None:
    """`record_sequence_gap` is deliberately not a required `HubMetrics`
    method (see hub.py's docstring): a hub with no metrics at all, or a
    metrics stub that only ever implemented the older
    `record_subscriber_queue_overflow`, must not raise.
    """
    hub = RealtimeHub()
    hub.record_sequence_gap()  # must not raise

    class _MetricsWithoutGapCounter:
        def record_subscriber_queue_overflow(self) -> None:
            pass

    hub2 = RealtimeHub(metrics=_MetricsWithoutGapCounter())
    hub2.record_sequence_gap()  # must not raise (AttributeError) either


def _bootstrap(client: TestClient, headers: dict[str, str], room_name: str) -> str:
    response = client.post(
        "/api/v1/me/bootstrap",
        headers=headers,
        json={"display_name": "Owner", "room_name": room_name},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["room"]["room_id"])


def test_resync_request_message_is_accepted_and_does_not_close_the_socket() -> None:
    """A client that detects a gap sends {"type": "resync_request", ...}
    and keeps its socket open (it is already reloading its own state over
    HTTP) — the server's only job is to count it, which is what the earlier
    tests above prove `record_sequence_gap` does.
    """
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, OWNER, "Gap room")
        with client.websocket_connect(f"/ws?room_id={room_id}", headers=OWNER) as websocket:
            assert websocket.receive_json()["type"] == "connected"
            websocket.send_json(
                {"type": "resync_request", "room_id": room_id, "expected": 5, "got": 9}
            )
            # Still alive and answering ordinary traffic afterward.
            websocket.send_json({"type": "ping"})
            assert websocket.receive_json() == {"type": "pong"}


def _scrape(client: TestClient, metric: str) -> int:
    response = client.get("/metrics")
    assert response.status_code == 200, response.text
    for line in response.text.splitlines():
        if line.startswith(f"{metric} "):
            return int(line.split()[-1])
    raise AssertionError(f"{metric} not found in /metrics output")


def test_a_client_resync_request_increments_the_metrics_endpoint() -> None:
    """End to end: the client's resync_request (proven above to keep the
    socket open) is what the round 2 critic found had no counter backing
    it. `/metrics` must show the increment, not just `RealtimeHub`'s own
    in-memory state (already covered by the unit tests above).
    """
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, OWNER, "Gap room")

        before = _scrape(client, "xyzzy_sequence_gaps_total")

        with client.websocket_connect(f"/ws?room_id={room_id}", headers=OWNER) as websocket:
            assert websocket.receive_json()["type"] == "connected"
            websocket.send_json(
                {"type": "resync_request", "room_id": room_id, "expected": 5, "got": 9}
            )
            websocket.send_json({"type": "ping"})
            assert websocket.receive_json() == {"type": "pong"}

        after_one = _scrape(client, "xyzzy_sequence_gaps_total")
        assert after_one == before + 1

        # A second, independent gap report on a second socket increments
        # again — this is a counter, not a one-shot flag.
        with client.websocket_connect(f"/ws?room_id={room_id}", headers=OWNER) as websocket:
            assert websocket.receive_json()["type"] == "connected"
            websocket.send_json(
                {"type": "resync_request", "room_id": room_id, "expected": 12, "got": 20}
            )
            websocket.send_json({"type": "ping"})
            assert websocket.receive_json() == {"type": "pong"}

        after_two = _scrape(client, "xyzzy_sequence_gaps_total")
        assert after_two == before + 2
