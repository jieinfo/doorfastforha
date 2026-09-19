"""Tests for Doorfast push event validation and ordering."""
import importlib.util
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).parents[1] / "custom_components" / "doorfast" / "events.py"
SPEC = importlib.util.spec_from_file_location("doorfast_events", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
EventGate = MODULE.EventGate
validate_event = MODULE.validate_event


def event(generation=7, name="incoming_call", event_id=1):
    return {"schema_version": 1, "event_id": event_id, "event": name, "generation": generation, "timestamp_ms": 1000}


def monitor_event(
    generation=9,
    name="monitor_publishing",
    event_id=4,
    status_revision=7,
    status=None,
    runtime_id="runtime-a",
    station_id="gate_main",
):
    if status is None:
        status = {
            "state": "publishing",
            "encoder_running": True,
            "queue_drops": 0,
            "relay_failures": 0,
            "failure": "",
        }
    return {
        "schema_version": 1,
        "event_id": event_id,
        "event": name,
        "generation": generation,
        "status_revision": status_revision,
        "timestamp_ms": 1000,
        "status": status,
        "runtime_id": runtime_id,
        "station_id": station_id,
    }


class PushEventTest(unittest.TestCase):
    def test_rejects_malformed_event(self):
        for payload in ({}, event(name="unknown"), event(generation=0), {**event(), "schema_version": 2}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    validate_event(payload)

    def test_suppresses_duplicate_generation_and_event(self):
        gate = EventGate()
        payload = validate_event(event())
        self.assertTrue(gate.accept(payload))
        self.assertFalse(gate.accept(payload))
        self.assertTrue(gate.accept(validate_event(event(name="hangup", event_id=2))))

    def test_evicts_old_duplicate_keys_with_bounded_memory(self):
        gate = EventGate(capacity=1)
        self.assertTrue(gate.accept(validate_event(event(event_id=1))))
        self.assertTrue(gate.accept(validate_event(event(name="hangup", event_id=2))))
        self.assertTrue(gate.accept(validate_event(event(event_id=3))))

    def test_rejects_stale_generation(self):
        gate = EventGate()
        self.assertTrue(gate.accept(validate_event(event(generation=8))))
        self.assertFalse(gate.accept(validate_event(event(generation=7, event_id=2)), current_generation=8))
        self.assertFalse(gate.accept(validate_event(event(generation=9, event_id=3)), current_generation=10))

    def test_call_gate_resets_high_water_for_new_runtime(self):
        gate = EventGate()

        self.assertTrue(
            gate.accept(validate_event(event(generation=8)), 8, "0123456789abcdef")
        )
        self.assertTrue(
            gate.accept(
                validate_event(event(generation=1, event_id=1)),
                1,
                "fedcba9876543210",
            )
        )

    def test_validates_real_monitor_relay_shape(self):
        payload = validate_event(monitor_event())

        self.assertEqual("monitor_publishing", payload["event"])
        self.assertEqual(7, payload["status_revision"])
        self.assertEqual("publishing", payload["status"]["state"])

    def test_rejects_malformed_or_oversized_monitor_status(self):
        invalid = (
            monitor_event(status_revision=0),
            monitor_event(status_revision=True),
            {key: value for key, value in monitor_event().items() if key != "status"},
            monitor_event(status="publishing"),
            monitor_event(status={"detail": "x" * 17000}),
            {key: value for key, value in monitor_event().items() if key != "runtime_id"},
            {key: value for key, value in monitor_event().items() if key != "station_id"},
        )
        for payload in invalid:
            with self.subTest(payload_type=type(payload.get("status"))):
                with self.assertRaises(ValueError):
                    validate_event(payload)

    def test_rejects_sensitive_monitor_fields_at_any_depth(self):
        for field in ("password", "token", "authorization", "sdp", "candidate", "url"):
            with self.subTest(field=field):
                payload = monitor_event(
                    status={"state": "publishing", "nested": [{field: "secret"}]}
                )
                with self.assertRaises(ValueError):
                    validate_event(payload)

    def test_monitor_gate_rejects_duplicate_id_and_stale_revision(self):
        gate = EventGate()
        first = validate_event(monitor_event(event_id=4, status_revision=7))
        duplicate_id = validate_event(
            monitor_event(
                name="monitor_stopped", event_id=4, status_revision=8
            )
        )
        stale_revision = validate_event(
            monitor_event(event_id=5, status_revision=6)
        )
        next_generation = validate_event(
            monitor_event(generation=10, event_id=6, status_revision=1)
        )

        self.assertTrue(gate.accept_monitor(first, 9, 7))
        self.assertFalse(gate.accept_monitor(duplicate_id, 9, 8))
        self.assertFalse(gate.accept_monitor(stale_revision, 9, 7))
        self.assertTrue(gate.accept_monitor(next_generation, 10, 1))

    def test_monitor_gate_rejects_conflicting_event_at_same_revision(self):
        gate = EventGate()
        first = validate_event(monitor_event(event_id=4, status_revision=7))
        conflict = validate_event(
            monitor_event(
                name="monitor_failed", event_id=5, status_revision=7
            )
        )

        self.assertTrue(gate.accept_monitor(first, 9, 7, "runtime-a"))
        self.assertFalse(gate.accept_monitor(conflict, 9, 7, "runtime-a"))

    def test_monitor_gate_resets_high_water_for_new_runtime(self):
        gate = EventGate()
        old_runtime = validate_event(
            monitor_event(generation=9, event_id=4, status_revision=7)
        )
        new_runtime = validate_event(
            monitor_event(
                generation=1,
                event_id=1,
                status_revision=2,
                runtime_id="runtime-b",
            )
        )

        self.assertTrue(
            gate.accept_monitor(old_runtime, 9, 7, "runtime-a")
        )
        self.assertTrue(
            gate.accept_monitor(new_runtime, 1, 2, "runtime-b")
        )

    def test_monitor_gate_is_scoped_by_runtime_and_station(self):
        gate = EventGate()
        main = validate_event(monitor_event())
        side = validate_event(monitor_event(station_id="gate_side"))

        self.assertTrue(gate.accept_monitor(main, 9, 7, "runtime-a"))
        self.assertTrue(gate.accept_monitor(side, 9, 7, "runtime-a"))
        self.assertFalse(gate.accept_monitor(main, 9, 7, "runtime-a"))

    def test_monitor_gate_rejects_wrong_runtime_without_poisoning_station(self):
        gate = EventGate()
        wrong = validate_event(monitor_event(runtime_id="runtime-b"))
        correct = validate_event(monitor_event())

        self.assertFalse(gate.accept_monitor(wrong, 9, 7, "runtime-a"))
        self.assertTrue(gate.accept_monitor(correct, 9, 7, "runtime-a"))


if __name__ == "__main__":
    unittest.main()

class ProcessEventTest(unittest.IsolatedAsyncioTestCase):
    async def test_monitor_relay_targets_one_station_then_authoritative_sync(self):
        class Monitor:
            def __init__(self, station_id):
                self.runtime_id = "runtime-a"
                self.station_id = station_id
                self.generation = 7
                self.status_revision = 4
                self.applied = []

            async def async_apply_status(self, payload):
                self.applied.append(payload)

        class Registry:
            def __init__(self):
                self.items = {
                    "gate_main": Monitor("gate_main"),
                    "gate_side": Monitor("gate_side"),
                }

            def monitor(self, station_id):
                return self.items[station_id]

        class Client:
            status = {"runtime_id": "runtime-a"}
            online = True

            async def refresh(self):
                return self.status

        registry = Registry()
        client = Client()
        synchronized = []

        async def sync_monitor(station_id, relay_event):
            synchronized.append((station_id, relay_event["event_id"]))
            await registry.monitor(station_id).async_apply_status(relay_event)

        accepted = await MODULE.process_event(
            monitor_event(station_id="gate_side", event_id=10,
                          generation=7, status_revision=5),
            client,
            EventGate(),
            lambda *_: None,
            lambda _: False,
            monitor=registry,
            sync_monitor=sync_monitor,
        )

        self.assertEqual(200, accepted[0])
        self.assertEqual([("gate_side", 10)], synchronized)
        self.assertEqual([], registry.items["gate_main"].applied)
        self.assertEqual([10], [item["event_id"] for item in registry.items["gate_side"].applied])

    async def test_monitor_relay_rejects_wrong_station_runtime_stale_and_duplicate(self):
        class Monitor:
            runtime_id = "runtime-a"
            generation = 7
            status_revision = 5

            async def async_apply_status(self, _payload):
                self.fail("rejected event must not update a coordinator")

        class Registry:
            def __init__(self):
                self.main = Monitor()

            def monitor(self, station_id):
                if station_id != "gate_main":
                    raise KeyError(station_id)
                return self.main

        class Client:
            status = {"runtime_id": "runtime-a"}
            online = True

            async def refresh(self):
                return self.status

        registry = Registry()
        client = Client()
        gate = EventGate()

        async def sync_monitor(_station_id, _relay_event):
            self.fail("rejected event must not synchronize")

        payloads = (
            monitor_event(station_id="gate_side", event_id=11, generation=7,
                          status_revision=6),
            monitor_event(runtime_id="runtime-b", event_id=12, generation=7,
                          status_revision=6),
            monitor_event(event_id=13, generation=7, status_revision=4),
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                status, body = await MODULE.process_event(
                    payload, client, gate, lambda *_: None, lambda _: False,
                    monitor=registry, sync_monitor=sync_monitor,
                )
                self.assertEqual(202, status)
                self.assertEqual("duplicate_or_stale", body["reason"])

        first = monitor_event(event_id=14, generation=7, status_revision=6)

        async def accepted_sync(_station_id, _relay_event):
            return None

        status, _ = await MODULE.process_event(
            first, client, gate, lambda *_: None, lambda _: False,
            monitor=registry,
            sync_monitor=accepted_sync,
        )
        self.assertEqual(200, status)
        status, _ = await MODULE.process_event(
            first, client, gate, lambda *_: None, lambda _: False,
            monitor=registry,
            sync_monitor=accepted_sync,
        )
        self.assertEqual(202, status)

    async def test_monitor_relay_returns_503_when_authoritative_sync_fails(self):
        class Monitor:
            runtime_id = "runtime-a"
            generation = 7
            status_revision = 4

        class Registry:
            def monitor(self, station_id):
                if station_id != "gate_main":
                    raise KeyError(station_id)
                return Monitor()

        class Client:
            status = {"runtime_id": "runtime-a"}
            online = True

            async def refresh(self):
                return self.status

        async def sync_monitor(_station_id, _relay_event):
            raise RuntimeError("monitor endpoint unavailable")

        status, body = await MODULE.process_event(
            monitor_event(event_id=15, generation=7, status_revision=5),
            Client(),
            EventGate(),
            lambda *_: self.fail("failed synchronization must not dispatch"),
            lambda _: False,
            monitor=Registry(),
            sync_monitor=sync_monitor,
        )

        self.assertEqual(503, status)
        self.assertEqual("Doorfast status unavailable", body["error"])

    async def test_monitor_relay_rejects_runtime_changed_after_refresh(self):
        class Monitor:
            runtime_id = "runtime-a"
            generation = 7
            status_revision = 4

        class Registry:
            def monitor(self, station_id):
                if station_id != "gate_main":
                    raise KeyError(station_id)
                return Monitor()

        class Client:
            status = {"runtime_id": "runtime-b"}
            online = True

            async def refresh(self):
                return self.status

        status, body = await MODULE.process_event(
            monitor_event(event_id=16, generation=7, status_revision=5),
            Client(),
            EventGate(),
            lambda *_: self.fail("old runtime must not dispatch"),
            lambda _: False,
            monitor=Registry(),
            sync_monitor=lambda *_: self.fail("old runtime must not synchronize"),
        )

        self.assertEqual(202, status)
        self.assertEqual("duplicate_or_stale", body["reason"])

    async def test_refreshes_before_dispatch_and_derives_ring_from_status(self):
        class Client:
            status = {"call": {"generation": 7, "session": "ringing"}}
            online = False
            async def refresh(self):
                self.status = {"call": {"generation": 7, "session": "ringing"}}
                self.refreshed = True
        client, seen = Client(), []
        status, body = await MODULE.process_event(event(), client, EventGate(), lambda kind, value: seen.append((kind, value)), lambda value: value["call"]["session"] == "ringing")
        self.assertEqual((200, {"status": "success"}), (status, body))
        self.assertTrue(client.refreshed)
        self.assertEqual(["status", "latest_event", "ring_status"], [item[0] for item in seen])
        self.assertTrue(seen[-1][1])

    async def test_handles_malformed_refreshed_call_status(self):
        class Client:
            status = {"call": None}
            online = True
            async def refresh(self):
                self.status = {"call": None}
        status, _ = await MODULE.process_event(event(), Client(), EventGate(), lambda *_: None, lambda _: False)
        self.assertEqual(200, status)

    async def test_returns_503_when_refresh_fails(self):
        class Client:
            status = {}
            online = True
            async def refresh(self):
                raise RuntimeError("offline")
        status, body = await MODULE.process_event(event(), Client(), EventGate(), lambda *_: self.fail("must not dispatch"), lambda _: False)
        self.assertEqual(503, status)
        self.assertEqual("Doorfast status unavailable", body["error"])

    async def test_returns_202_for_duplicate_or_stale_event(self):
        class Client:
            status = {"call": {"generation": 8}}
            online = True
            async def refresh(self):
                return self.status
        gate, client = EventGate(), Client()
        dispatch = lambda *_: None
        self.assertEqual(200, (await MODULE.process_event(event(generation=8), client, gate, dispatch, lambda _: False))[0])
        status, body = await MODULE.process_event(event(generation=8, event_id=2), client, gate, dispatch, lambda _: False)
        self.assertEqual(202, status)
        self.assertEqual("duplicate_or_stale", body["reason"])

    async def test_returns_400_for_invalid_payload(self):
        class Client:
            status = {}
            online = True
        status, body = await MODULE.process_event({}, Client(), EventGate(), lambda *_: self.fail("must not dispatch"), lambda _: False)
        self.assertEqual(400, status)

    async def test_monitor_event_is_gated_against_media_not_call_generation(self):
        class Client:
            status = {}
            online = True

            async def refresh(self):
                self.status = {
                    "runtime_id": "runtime-a",
                    "call": {"generation": 42, "session": "idle"},
                    "media": {
                        "generation": 9,
                        "status_revision": 7,
                        "state": "publishing",
                    },
                }
                return self.status

        seen = []
        status, body = await MODULE.process_event(
            monitor_event(),
            Client(),
            EventGate(),
            lambda kind, value: seen.append((kind, value)),
            lambda _: False,
        )

        self.assertEqual((200, {"status": "success"}), (status, body))
        self.assertEqual("monitor_publishing", seen[1][1]["event"])

    async def test_monitor_event_older_than_refreshed_media_is_ignored(self):
        class Client:
            status = {}
            online = True

            async def refresh(self):
                self.status = {
                    "runtime_id": "runtime-a",
                    "media": {
                        "generation": 9,
                        "status_revision": 8,
                        "state": "viewing",
                    }
                }
                return self.status

        status, body = await MODULE.process_event(
            monitor_event(status_revision=7),
            Client(),
            EventGate(),
            lambda *_: self.fail("stale monitor event must not dispatch"),
            lambda _: False,
        )

        self.assertEqual(202, status)
        self.assertEqual("duplicate_or_stale", body["reason"])

    async def test_monitor_event_for_unknown_station_is_ignored_without_dispatch(self):
        class Client:
            status = {}
            online = True

            async def refresh(self):
                self.status = {
                    "runtime_id": "runtime-a",
                    "media": {"sessions": []},
                }

        status, body = await MODULE.process_event(
            monitor_event(station_id="gate_side"),
            Client(),
            EventGate(),
            lambda *_: self.fail("wrong station must not dispatch"),
            lambda _: False,
            ("gate_main",),
        )

        self.assertEqual(202, status)
        self.assertEqual("duplicate_or_stale", body["reason"])
