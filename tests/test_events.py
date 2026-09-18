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
            monitor_event(generation=1, event_id=1, status_revision=2)
        )

        self.assertTrue(
            gate.accept_monitor(old_runtime, 9, 7, "runtime-a")
        )
        self.assertTrue(
            gate.accept_monitor(new_runtime, 1, 2, "runtime-b")
        )


if __name__ == "__main__":
    unittest.main()

class ProcessEventTest(unittest.IsolatedAsyncioTestCase):
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
