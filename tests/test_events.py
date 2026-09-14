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
