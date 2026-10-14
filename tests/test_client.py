"""Tests for command payloads emitted by the Doorfast client."""

import asyncio
import importlib.util
import struct
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, call


ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / "doorfast"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


aiohttp = types.ModuleType("aiohttp")
aiohttp.ClientTimeout = lambda **kwargs: kwargs
sys.modules.setdefault("aiohttp", aiohttp)
sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
helpers = sys.modules.setdefault(
    "homeassistant.helpers", types.ModuleType("homeassistant.helpers")
)
aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
aiohttp_client.async_get_clientsession = lambda hass: None
helpers.aiohttp_client = aiohttp_client
sys.modules.setdefault("homeassistant.helpers.aiohttp_client", aiohttp_client)

package = types.ModuleType("custom_components.doorfast")
package.__path__ = [str(COMPONENT)]
sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
sys.modules["custom_components.doorfast"] = package
load_module("custom_components.doorfast.const", COMPONENT / "const.py")
load_module("custom_components.doorfast.generation", COMPONENT / "generation.py")
client_module = load_module("custom_components.doorfast.client", COMPONENT / "client.py")
DoorfastClient = client_module.DoorfastClient
client_types_module = sys.modules["custom_components.doorfast.client_types"]
DoorfastStation = client_types_module.DoorfastStation


class FakeResponse:
    def __init__(self, status, body=b"", headers=None, before_read=None):
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.before_read = before_read

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def read(self):
        if self.before_read is not None:
            await self.before_read()
        return self.body


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.responses.pop(0)


def video_client(*responses):
    client = DoorfastClient.__new__(DoorfastClient)
    client.base_url = "http://doorfast/cgi-bin/doorfast"
    client.session = FakeSession(*responses)
    client.status = {
        "runtime_id": "aaaaaaaaaaaaaaaa",
        "call": {"generation": 7},
        "video": {"ready": True, "generation": 7, "frame_no": 12},
    }
    client.online = True
    client._video_generation = None
    client._video_etag = None
    client._video_frame = None
    client._audio_generation = None
    client._audio_revision = None
    client._audio_etag = None
    client._refresh_sequence = 0
    return client


def audio_client(*responses):
    client = DoorfastClient.__new__(DoorfastClient)
    client.base_url = "http://doorfast/cgi-bin/doorfast"
    client.session = FakeSession(*responses)
    client.status = {
        "runtime_id": "aaaaaaaaaaaaaaaa",
        "call": {"generation": 7},
        "audio": {
            "snapshot_ready": True,
            "generation": 7,
            "snapshot_packet_count": 40,
            "snapshot_previous_packet_count": 30,
        },
    }
    client.online = True
    client._audio_generation = None
    client._audio_revision = None
    client._audio_etag = None
    client._video_generation = None
    client._video_etag = None
    client._video_frame = None
    client._refresh_sequence = 0
    return client


class ControlPayloadTest(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_started_before_control_change_cannot_replace_authoritative_status(self):
        client = DoorfastClient.__new__(DoorfastClient)
        fresh = {"runtime_id": "0123456789abcdef", "call": {"session": "talking", "generation": 42}}
        client.status = fresh
        client._refresh_sequence = 0
        client._station_control_epochs = {}
        client._video_generation = client._audio_generation = None
        entered, resume = asyncio.Event(), asyncio.Event()
        async def request(*args):
            entered.set()
            await resume.wait()
            return {"runtime_id": "0123456789abcdef", "call": {"session": "idle", "generation": 0}}
        client._request = request
        refresh = asyncio.create_task(client.refresh())
        await entered.wait()
        client._station_control_epochs["gate_main"] = 1
        resume.set()
        await refresh
        self.assertIs(fresh, client.status)

    async def test_hangup_reply_rejects_changed_operation_before_success(self):
        client = DoorfastClient.__new__(DoorfastClient)
        client.status = {"runtime_id": "0123456789abcdef"}
        client._request = AsyncMock(return_value={"runtime_id": "0123456789abcdef",
            "station_id": "gate_main", "queued": True, "state": "ending", "generation": 9, "operation_id": 4})
        with self.assertRaises(ValueError):
            await client.hangup_station("gate_main", operation_id=3, generation=9)

    async def test_real_delayed_refresh_after_new_call_preserves_sensor_and_coordinator(self):
        from tests.test_station_status_entities import sensor, station
        from custom_components.doorfast.monitor import MonitorCoordinator
        client = DoorfastClient(None, "http://synthetic.invalid")
        client.online = True
        client.status = {"runtime_id": "0123456789abcdef", "call": {"session": "idle", "generation": 0},
                         "media": {"sessions": []}}
        entered, resume = asyncio.Event(), asyncio.Event()
        async def request(method, path, payload=None):
            if method == "GET":
                entered.set()
                await resume.wait()
                return {"runtime_id": "0123456789abcdef", "call": {"session": "idle", "generation": 0}}
            return {"runtime_id": "0123456789abcdef", "station_id": "gate_main", "operation_id": 3,
                    "generation": 9, "queued": True, "state": "preparing"}
        client._request = request
        monitor = MonitorCoordinator(client, "0123456789abcdef", "gate_main")
        refresh = asyncio.create_task(client.refresh())
        await entered.wait()
        await monitor.async_call()
        fresh = {"runtime_id": "0123456789abcdef", "station_call": {"operation_id": 3,
            "station_id": "gate_main", "stage": "talking", "media_generation": 9, "call_generation": 42,
            "timer": "session", "remaining_ms": 12000, "error": "none"},
            "call": {"session": "talking", "generation": 42, "station_id": "gate_main"},
            "media": {"sessions": [{"station_id": "gate_main", "generation": 9, "purpose": "call",
                                    "state": "publishing", "source_live": False, "ready": True}]}}
        client.status = fresh
        await monitor.async_apply_call_status(fresh)
        entity = sensor.DoorfastPreviewStatus(client, "entry-1", station())
        self.assertEqual("in_call", entity.native_value)
        resume.set()
        await refresh
        await monitor.async_apply_call_status(client.status)
        self.assertEqual("in_call", entity.native_value)
        self.assertEqual("talking", monitor.call_state)
        self.assertFalse(entity.extra_state_attributes["source_live"])

    async def test_station_http_structured_errors_become_stable_codes(self):
        class Response:
            status = 409
            def raise_for_status(self): raise RuntimeError("HTTP secret URL")
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def json(self, **kwargs): return {"error": {"code": "busy", "message": "secret URL"}}
        class Session:
            def request(self, *args, **kwargs): return Response()
        client = DoorfastClient.__new__(DoorfastClient)
        client.base_url = "http://synthetic.invalid"
        client.session = Session()
        client.status = {"runtime_id": "0123456789abcdef"}
        with self.assertRaises(Exception) as raised:
            await client.call_station("gate_main")
        self.assertEqual("busy", getattr(raised.exception, "code", None))

        class Unknown(Response):
            async def json(self, **kwargs): return {"error": {"code": "secret-url-code"}}
        client.session = types.SimpleNamespace(request=lambda *args, **kwargs: Unknown())
        with self.assertRaises(Exception) as raised:
            await client.call_station("gate_main")
        self.assertEqual("unknown", getattr(raised.exception, "code", None))
        self.assertNotIn("secret", str(raised.exception))
        from custom_components.doorfast.monitor import MonitorCoordinator
        for body, expected in (({"error": {"code": "station_unavailable", "message": "secret"}}, "station_unavailable"),
                               (["secret"], "unknown"), ({"error": {"code": "secret"}}, "unknown")):
            class Failure(Response):
                async def json(self, **kwargs): return body
            client.session = types.SimpleNamespace(request=lambda *args, **kwargs: Failure())
            coordinator = MonitorCoordinator(client, "0123456789abcdef", "gate_main")
            with self.assertRaises(RuntimeError):
                await coordinator.async_call()
            self.assertEqual(expected, coordinator.call_error)

            with self.assertRaises(RuntimeError):
                await coordinator.async_hangup()
            self.assertEqual(expected, coordinator.call_error)

    async def test_actual_c_admission_errors_keep_stable_coordinator_categories(self):
        from custom_components.doorfast.monitor import MonitorCoordinator

        cases = (("capacity_busy", "busy"), ("station_not_found", "station_unavailable"),
                 ("station_disabled", "station_unavailable"), ("route_unavailable", "station_unavailable"),
                 ("encoder_failed", "media_failed"), ("runtime_mismatch", "unknown"),
                 ("operation_mismatch", "unknown"), ("generation_mismatch", "unknown"),
                 ("unclassified_secret", "unknown"))
        stable = ("none", "busy", "station_unavailable", "ack_timeout", "dispatch_failed", "dispatch_timeout",
                  "media_failed", "preempted", "cancelled", "runtime_reset", "unknown")
        for code, expected in (*cases, *((code, code) for code in stable)):
            for action in ("call", "hangup"):
                with self.subTest(wire_code=code, action=action):
                    body = {"error": {"code": code, "message": "synthetic secret URL"}}
                    if code == "capacity_busy":
                        body["error"].update(configured_capacity=2, effective_capacity=2, active_encoders=2)

                    class Response:
                        status = 409
                        async def __aenter__(self): return self
                        async def __aexit__(self, *args): return False
                        async def json(self, **kwargs): return body

                    requests = []
                    def request(method, url, **kwargs):
                        requests.append((method, url, kwargs["json"]))
                        return Response()

                    client = DoorfastClient(None, "http://synthetic.invalid")
                    client.status = {"runtime_id": "0123456789abcdef"}
                    client.session = types.SimpleNamespace(request=request)
                    coordinator = MonitorCoordinator(client, "0123456789abcdef", "gate_main")
                    with self.assertRaises(RuntimeError) as raised:
                        await (coordinator.async_call() if action == "call" else coordinator.async_hangup())
                    self.assertEqual(expected, coordinator.call_error)
                    self.assertEqual(expected, raised.exception.code)
                    self.assertNotIn("secret", str(raised.exception))
                    self.assertEqual([("POST", f"http://synthetic.invalid/api/v1/{action}",
                        {"runtime_id": "0123456789abcdef", "station_id": "gate_main"})], requests)

    async def test_old_operation_snapshot_cannot_replace_newer_shared_status(self):
        client = DoorfastClient(None, "http://synthetic.invalid")
        operation = {"operation_id": 4, "station_id": "gate_main", "stage": "calling",
            "media_generation": 9, "call_generation": 42, "timer": "confirmation",
            "remaining_ms": 800, "error": "none"}
        client.status = {"runtime_id": "0123456789abcdef", "station_call": operation}
        client._request = AsyncMock(return_value={"runtime_id": "0123456789abcdef",
            "station_call": {**operation, "operation_id": 3, "stage": "ended", "timer": "none", "remaining_ms": 0}})
        await client.refresh()
        self.assertEqual(4, client.status["station_call"]["operation_id"])

    async def test_strict_station_json_at_actual_http_transport_boundary(self):
        class Response:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            def raise_for_status(self): pass
            async def json(self, **kwargs): return self.payload

        class Session:
            def __init__(self): self.sent = []
            def request(self, method, url, **kwargs):
                self.sent.append((method, url, kwargs["json"]))
                response = Response()
                response.payload = {"runtime_id": "0123456789abcdef", "station_id": "gate_main",
                                    "operation_id": 3, "generation": 9, "state": "preparing", "queued": True}
                return response

        client = DoorfastClient.__new__(DoorfastClient)
        client.status = {"runtime_id": "0123456789abcdef"}
        client.base_url = "http://synthetic.invalid"
        client.session = Session()
        await client.call_station("gate_main", 9303, 9302, 45)
        await client.hangup_station("gate_main", "ha", generation=9, operation_id=3)
        self.assertEqual([
            ("POST", "http://synthetic.invalid/api/v1/call",
             {"runtime_id": "0123456789abcdef", "station_id": "gate_main"}),
            ("POST", "http://synthetic.invalid/api/v1/hangup",
             {"runtime_id": "0123456789abcdef", "station_id": "gate_main", "generation": 9, "operation_id": 3})
        ], client.session.sent)

    async def test_station_call_and_form_hangup_bind_runtime_and_station(self):
        client = DoorfastClient.__new__(DoorfastClient)
        client.status = {"runtime_id": "0123456789abcdef", "call": {"generation": 7}}
        client._request = AsyncMock(side_effect=[{"state": "calling"}, {"state": "idle"}])

        self.assertEqual(
            {"state": "calling"},
            await client.call_station("gate_main", duration_seconds=45),
        )
        self.assertEqual(
            {"state": "idle"}, await client.hangup_station("gate_main", reason="ha")
        )
        self.assertEqual(
            [
                call(
                    "POST",
                    "/api/v1/call",
                    {
                        "runtime_id": "0123456789abcdef",
                        "station_id": "gate_main",
                    },
                ),
                call(
                    "POST",
                    "/api/v1/hangup",
                    {
                        "runtime_id": "0123456789abcdef",
                        "station_id": "gate_main",
                    },
                ),
            ],
            client._request.await_args_list,
        )

    async def test_station_controls_reject_invalid_station_and_duration(self):
        client = DoorfastClient.__new__(DoorfastClient)
        client.status = {"runtime_id": "0123456789abcdef"}
        client._request = AsyncMock()
        for station_id in ("Gate-main", "", None):
            with self.assertRaises(ValueError):
                await client.call_station(station_id)
            with self.assertRaises(ValueError):
                await client.hangup_station(station_id)
        with self.assertRaises(ValueError):
            await client.call_station("gate_main", duration_seconds=0)
        client._request.assert_not_awaited()
    async def test_binds_all_controls_to_current_runtime(self):
        client = DoorfastClient.__new__(DoorfastClient)
        client.status = {
            "runtime_id": "0123456789abcdef",
            "call": {"generation": 7, "station_id": "gate_main"},
        }
        client._request = AsyncMock(return_value={"queued": True})

        self.assertEqual({"queued": True}, await client.answer())
        self.assertEqual({"queued": True}, await client.hangup(reason=1))
        self.assertEqual({"queued": True}, await client.unlock())
        self.assertEqual({"queued": True}, await client.call_elevator("down"))

        self.assertEqual(
            [
                call(
                    "POST",
                    "/api/v1/answer",
                    {
                        "runtime_id": "0123456789abcdef",
                        "generation": 7,
                        "primary_media_port": 8303,
                        "secondary_media_port": 8302,
                        "duration_seconds": 120,
                    },
                ),
                call(
                    "POST",
                    "/api/v1/hangup",
                    {
                        "runtime_id": "0123456789abcdef",
                        "generation": 7,
                        "reason": 1,
                    },
                ),
                call(
                    "POST",
                    "/api/v1/unlock",
                    {"station_id": "gate_main"},
                ),
                call(
                    "POST",
                    "/api/v1/call_elevator",
                    {"runtime_id": "0123456789abcdef", "direction": "down"},
                ),
            ],
            client._request.await_args_list,
        )

    async def test_rejects_controls_without_valid_runtime(self):
        client = DoorfastClient.__new__(DoorfastClient)
        client._request = AsyncMock()

        for runtime_id in (None, "", "0123456789abcdeF", "short"):
            with self.subTest(runtime_id=runtime_id):
                client.status = {
                    "runtime_id": runtime_id,
                    "call": {"generation": 7},
                }
                with self.assertRaises(ValueError):
                    await client.answer()
                with self.assertRaises(ValueError):
                    await client.hangup()
                with self.assertRaises(ValueError):
                    await client.call_elevator()
        client._request.assert_not_awaited()

    async def test_station_unlock_does_not_require_runtime_id_or_generation(self):
        client = DoorfastClient.__new__(DoorfastClient)
        client.status = {}
        client._request = AsyncMock(return_value={"submitted": True})

        self.assertEqual({"submitted": True}, await client.unlock_station("gate_main"))
        client._request.assert_awaited_once_with(
            "POST", "/api/v1/unlock", {"station_id": "gate_main"}
        )

class MonitorPayloadTest(unittest.IsolatedAsyncioTestCase):
    async def test_station_hangup_validates_optional_operation_and_control_keys(self):
        client = DoorfastClient.__new__(DoorfastClient)
        client.status = {"runtime_id": "0123456789abcdef"}
        client._request = AsyncMock(return_value={"queued": True, "state": "ending"})
        await client.hangup_station("gate_main", generation=42, operation_id=3)
        self.assertEqual({"runtime_id": "0123456789abcdef", "station_id": "gate_main",
                          "generation": 42, "operation_id": 3}, client._request.await_args.args[2])
        for bad in (True, 0, -1, 2**64, "3"):
            with self.assertRaises(ValueError):
                await client.hangup_station("gate_main", operation_id=bad)

    async def test_call_rejects_malformed_or_conflicting_reply_identity(self):
        client = DoorfastClient.__new__(DoorfastClient)
        client.status = {"runtime_id": "0123456789abcdef"}
        valid = {"runtime_id": "0123456789abcdef", "station_id": "gate_main",
                 "operation_id": 3, "generation": 9, "state": "preparing", "queued": True}
        for changes in ({"operation_id": True}, {"generation": 2**64},
                        {"station_id": "gate_side"}, {"runtime_id": "fedcba9876543210"},
                        {"state": "secret http://password"}, {"queued": 1}):
            client._request = AsyncMock(return_value={**valid, **changes})
            with self.assertRaises(ValueError):
                await client.call_station("gate_main")

    async def test_uses_exact_monitor_endpoints_and_payloads(self):
        client = DoorfastClient.__new__(DoorfastClient)
        client._request = AsyncMock(
            side_effect=[
                {"state": "queued", "generation": 9},
                {"state": "publishing", "generation": 9},
                {"state": "queued", "generation": 9, "active": True},
                {"state": "stopping", "generation": 9},
            ]
        )

        self.assertEqual(
            {"state": "queued", "generation": 9},
            await client.start_monitor("runtime-a", "gate_main"),
        )
        self.assertEqual(
            {"state": "publishing", "generation": 9},
            await client.monitor_status(),
        )
        self.assertEqual(
            {"state": "queued", "generation": 9, "active": True},
            await client.set_monitor_viewer("runtime-a", "gate_main", 9, True),
        )
        self.assertEqual(
            {"state": "stopping", "generation": 9},
            await client.stop_monitor("runtime-a", "gate_main", 9),
        )
        self.assertEqual(
            [
                call(
                    "POST",
                    "/api/v1/monitor/start",
                    {"runtime_id": "runtime-a", "station_id": "gate_main"},
                ),
                call("GET", "/api/v1/monitor/status"),
                call(
                    "POST",
                    "/api/v1/monitor/viewer",
                    {
                        "runtime_id": "runtime-a",
                        "station_id": "gate_main",
                        "generation": 9,
                        "active": True,
                    },
                ),
                call(
                    "POST",
                    "/api/v1/monitor/stop",
                    {
                        "runtime_id": "runtime-a",
                        "station_id": "gate_main",
                        "generation": 9,
                    },
                ),
            ],
            client._request.await_args_list,
        )

    async def test_rejects_invalid_monitor_generation_and_active_flag(self):
        client = DoorfastClient.__new__(DoorfastClient)
        client._request = AsyncMock()

        for generation in (None, 0, -1, True, "9"):
            with self.subTest(generation=generation):
                with self.assertRaises(ValueError):
                    await client.stop_monitor("runtime-a", "gate_main", generation)
                with self.assertRaises(ValueError):
                    await client.set_monitor_viewer(
                        "runtime-a", "gate_main", generation, True
                    )
        for active in (None, 0, 1, "true"):
            with self.subTest(active=active):
                with self.assertRaises(ValueError):
                    await client.set_monitor_viewer(
                        "runtime-a", "gate_main", 9, active
                    )

        client._request.assert_not_awaited()


def station_payload(**overrides):
    payload = {
        "id": "gate_main",
        "name": "Main Gate",
        "logical_address": "32:02:01:00:02:00",
        "enabled": True,
        "stream_name": "doorfast_gate_main",
        "route_source": "discovered",
        "route_fresh": True,
        "monitorable": True,
        "last_seen_ms": 123456,
    }
    payload.update(overrides)
    return payload


class StationContractTest(unittest.IsolatedAsyncioTestCase):
    def test_parses_a_reachable_station(self):
        station = DoorfastStation.from_payload(station_payload())

        self.assertEqual("gate_main", station.station_id)
        self.assertEqual("Main Gate", station.name)
        self.assertTrue(station.reachable)

    def test_disabled_station_is_not_reachable(self):
        station = DoorfastStation.from_payload(station_payload(enabled=False))

        self.assertFalse(station.reachable)

    def test_rejects_malformed_station_fields(self):
        malformed_fields = (
            {"id": "Gate-main"},
            {"name": ""},
            {"name": "Main G\u00e1te"},
            {"logical_address": "31:02:01:00:02:00"},
            {"enabled": 1},
            {"stream_name": "doorfast gate"},
            {"route_source": "cached"},
            {"route_fresh": "true"},
            {"monitorable": 1},
            {"last_seen_ms": True},
            {"last_seen_ms": -1},
        )

        for overrides in malformed_fields:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    DoorfastStation.from_payload(station_payload(**overrides))

    async def test_returns_validated_station_snapshot(self):
        client = DoorfastClient.__new__(DoorfastClient)
        client._request = AsyncMock(
            return_value={
                "runtime_id": "0123456789abcdef",
                "revision": 3,
                "stations": [station_payload()],
            }
        )

        snapshot = await client.stations()

        self.assertEqual("0123456789abcdef", snapshot.runtime_id)
        self.assertEqual(3, snapshot.revision)
        self.assertEqual((DoorfastStation.from_payload(station_payload()),), snapshot.stations)
        client._request.assert_awaited_once_with("GET", "/api/v1/stations")

    async def test_rejects_duplicate_station_ids_and_wrong_runtime_ids(self):
        client = DoorfastClient.__new__(DoorfastClient)
        invalid_snapshots = (
            {
                "runtime_id": "not-a-runtime-id",
                "revision": 3,
                "stations": [station_payload()],
            },
            {
                "runtime_id": "0123456789abcdef",
                "revision": 3,
                "stations": [station_payload(), station_payload()],
            },
        )

        for payload in invalid_snapshots:
            with self.subTest(payload=payload):
                client._request = AsyncMock(return_value=payload)
                with self.assertRaises(ValueError):
                    await client.stations()

    async def test_rejects_duplicate_station_stream_names(self):
        client = DoorfastClient.__new__(DoorfastClient)
        client._request = AsyncMock(
            return_value={
                "runtime_id": "0123456789abcdef",
                "revision": 3,
                "stations": [
                    station_payload(),
                    station_payload(
                        id="gate_side",
                        name="Side Gate",
                        logical_address="32:02:01:00:02:01",
                    ),
                ],
            }
        )

        with self.assertRaisesRegex(ValueError, "duplicate station stream names"):
            await client.stations()

    async def test_rejects_non_object_station_snapshots(self):
        client = DoorfastClient.__new__(DoorfastClient)

        for payload in (None, [], 3, "station snapshot"):
            with self.subTest(payload=payload):
                client._request = AsyncMock(return_value=payload)
                with self.assertRaisesRegex(
                    ValueError, "station snapshot must be an object"
                ):
                    await client.stations()


class VideoFrameTest(unittest.IsolatedAsyncioTestCase):
    async def test_binds_request_to_generation_and_reuses_unchanged_frame(self):
        client = video_client(
            FakeResponse(
                200,
                b"jpeg-12",
                {"X-Doorfast-Generation": "7", "ETag": '"df-7-12"'},
            ),
            FakeResponse(304),
        )

        self.assertEqual(b"jpeg-12", await client.latest_video_frame())
        self.assertEqual(b"jpeg-12", await client.latest_video_frame())

        first = client.session.requests[0][1]
        second = client.session.requests[1][1]
        self.assertEqual({"generation": 7}, first["params"])
        self.assertEqual({}, first["headers"])
        self.assertEqual('"df-7-12"', second["headers"]["If-None-Match"])

    async def test_clears_cache_when_status_moves_to_another_call(self):
        client = video_client()
        client._video_generation = 7
        client._video_etag = '"df-7-12"'
        client._video_frame = b"old-call"
        client._request = AsyncMock(
            return_value={
                "call": {"generation": 8},
                "video": {"ready": False, "generation": 0},
            }
        )

        await client.refresh()

        self.assertIsNone(client._video_generation)
        self.assertIsNone(client._video_etag)
        self.assertIsNone(client._video_frame)

    async def test_rejects_mismatched_response_generation(self):
        client = video_client(
            FakeResponse(
                200,
                b"wrong-call",
                {"X-Doorfast-Generation": "8", "ETag": '"df-8-1"'},
            )
        )

        self.assertIsNone(await client.latest_video_frame())
        self.assertIsNone(client._video_frame)

    async def test_handles_stale_and_transient_responses(self):
        client = video_client(FakeResponse(503), FakeResponse(409))
        client._video_generation = 7
        client._video_etag = '"df-7-12"'
        client._video_frame = b"jpeg-12"

        self.assertEqual(b"jpeg-12", await client.latest_video_frame())
        self.assertIsNone(await client.latest_video_frame())
        self.assertIsNone(client._video_frame)

    async def test_drops_old_response_when_runtime_changes_with_same_generation(self):
        read_started = asyncio.Event()
        release_read = asyncio.Event()

        async def block_read():
            read_started.set()
            await release_read.wait()

        client = video_client(
            FakeResponse(
                200,
                b"old-runtime-frame",
                {"X-Doorfast-Generation": "7", "ETag": '"df-7-12"'},
                before_read=block_read,
            )
        )
        task = asyncio.create_task(client.latest_video_frame())
        await read_started.wait()
        client.status = {
            "runtime_id": "bbbbbbbbbbbbbbbb",
            "call": {"generation": 7},
            "video": {"ready": True, "generation": 7, "frame_no": 1},
        }
        client._clear_video_cache()
        release_read.set()

        self.assertIsNone(await task)
        self.assertIsNone(client._video_generation)
        self.assertIsNone(client._video_etag)
        self.assertIsNone(client._video_frame)


class AudioChunkTest(unittest.IsolatedAsyncioTestCase):
    async def test_v1_and_legacy_readers_keep_independent_cursors_on_same_client(self):
        # A v1 read must not replace the legacy cursor or require legacy WAV validity.
        class StreamingBody:
            def __init__(self, body):
                self.body = body

            async def read(self, size):
                part, self.body = self.body[:size], self.body[size:]
                return part

        pcm = bytes(1600)
        wav = struct.pack(
            "<4sI4s4sIHHIIHH4sI", b"RIFF", 1636, b"WAVE", b"fmt ",
            16, 1, 1, 8000, 16000, 2, 16, b"data", 1600,
        ) + pcm
        modern = FakeResponse(200, headers={
            "X-Doorfast-Runtime-Id": "aaaaaaaaaaaaaaaa",
            "X-Doorfast-Station-Id": "gate_main",
            "X-Doorfast-Generation": "7",
            "X-Doorfast-Audio-Previous-Revision": "100",
            "X-Doorfast-Audio-Revision": "110",
            "X-Doorfast-Audio-Dropped-Bytes": "0",
            "X-Doorfast-Audio-Discontinuity": "0",
            "X-Doorfast-Audio-Timestamp-Ms": "1000",
            "X-Doorfast-Audio-Latest-Timestamp-Ms": "1000",
            "X-Doorfast-Audio-Format": "s16le",
            "X-Doorfast-Audio-Sample-Rate": "8000",
            "X-Doorfast-Audio-Channels": "1",
        })
        modern.content = StreamingBody(wav)
        legacy = lambda previous, revision: FakeResponse(
            200, f"RIFFfake-{revision}-WAVE".encode(), {
                "X-Doorfast-Generation": "7",
                "X-Doorfast-Audio-Previous-Revision": str(previous),
                "X-Doorfast-Audio-Revision": str(revision),
                "ETag": f'"df-audio-7-{revision}"',
            },
        )
        client = audio_client(legacy(30, 40), modern, legacy(40, 50))
        self.assertEqual(b"RIFFfake-40-WAVE", await client.latest_audio_chunk())
        identity = client_types_module.AudioIdentity("aaaaaaaaaaaaaaaa", "gate_main", 7)
        reply = await client.read_audio_chunk(identity, after=100)
        self.assertEqual(pcm, reply.chunk.pcm)
        self.assertEqual(110, reply.chunk.revision)
        self.assertEqual(40, client._audio_revision)
        self.assertEqual('"df-audio-7-40"', client._audio_etag)
        self.assertEqual(7, client._audio_generation)
        self.assertEqual(b"RIFFfake-50-WAVE", await client.latest_audio_chunk())
        self.assertEqual({"runtime": "aaaaaaaaaaaaaaaa", "station_id": "gate_main",
                          "generation": 7, "after": 100},
                         client.session.requests[1][1]["params"])
        self.assertEqual({"generation": 7, "after": 40},
                         client.session.requests[2][1]["params"])

    async def test_advances_cursor_without_replaying_unchanged_audio(self):
        client = audio_client(
            FakeResponse(
                200,
                b"RIFFchunk-40-WAVE",
                {
                    "X-Doorfast-Generation": "7",
                    "X-Doorfast-Audio-Previous-Revision": "30",
                    "X-Doorfast-Audio-Revision": "40",
                    "ETag": '"df-audio-7-40"',
                },
            ),
            FakeResponse(304),
        )

        self.assertEqual(b"RIFFchunk-40-WAVE", await client.latest_audio_chunk())
        self.assertIsNone(await client.latest_audio_chunk())

        first = client.session.requests[0][1]
        second = client.session.requests[1][1]
        self.assertEqual({"generation": 7}, first["params"])
        self.assertEqual(
            {"generation": 7, "after": 40}, second["params"]
        )
        self.assertEqual(
            '"df-audio-7-40"', second["headers"]["If-None-Match"]
        )

    async def test_accepts_only_the_next_chunk_in_the_revision_chain(self):
        client = audio_client(
            FakeResponse(
                200,
                b"RIFFchunk-30-WAVE",
                {
                    "X-Doorfast-Generation": "7",
                    "X-Doorfast-Audio-Previous-Revision": "20",
                    "X-Doorfast-Audio-Revision": "30",
                    "ETag": '"df-audio-7-30"',
                },
            ),
            FakeResponse(
                200,
                b"RIFFwrong-chain-WAVE",
                {
                    "X-Doorfast-Generation": "7",
                    "X-Doorfast-Audio-Previous-Revision": "29",
                    "X-Doorfast-Audio-Revision": "40",
                },
            ),
        )
        client._audio_generation = 7
        client._audio_revision = 20
        client._audio_etag = '"df-audio-7-20"'

        self.assertEqual(b"RIFFchunk-30-WAVE", await client.latest_audio_chunk())
        self.assertIsNone(await client.latest_audio_chunk())
        self.assertIsNone(client._audio_revision)

    async def test_rejects_stale_initial_chunk_and_malformed_headers(self):
        client = audio_client(
            FakeResponse(
                200,
                b"stale",
                {
                    "X-Doorfast-Generation": "7",
                    "X-Doorfast-Audio-Previous-Revision": "20",
                    "X-Doorfast-Audio-Revision": "30",
                },
            ),
            FakeResponse(
                200,
                b"malformed",
                {
                    "X-Doorfast-Generation": "7",
                    "X-Doorfast-Audio-Previous-Revision": 30,
                    "X-Doorfast-Audio-Revision": "40",
                },
            ),
        )

        self.assertIsNone(await client.latest_audio_chunk())
        self.assertIsNone(await client.latest_audio_chunk())
        self.assertIsNone(client._audio_generation)

    async def test_accepts_initial_chunk_newer_than_polled_status(self):
        client = audio_client(
            FakeResponse(
                200,
                b"RIFFchunk-50-WAVE",
                {
                    "X-Doorfast-Generation": "7",
                    "X-Doorfast-Audio-Previous-Revision": "40",
                    "X-Doorfast-Audio-Revision": "50",
                    "ETag": '"df-audio-7-50"',
                },
            )
        )

        self.assertEqual(b"RIFFchunk-50-WAVE", await client.latest_audio_chunk())
        self.assertEqual(50, client._audio_revision)

    async def test_ignores_mismatched_etag_without_skipping_next_chunk(self):
        client = audio_client(
            FakeResponse(
                200,
                b"RIFFchunk-40-WAVE",
                {
                    "X-Doorfast-Generation": "7",
                    "X-Doorfast-Audio-Previous-Revision": "30",
                    "X-Doorfast-Audio-Revision": "40",
                    "ETag": '"df-audio-7-50"',
                },
            ),
            FakeResponse(
                200,
                b"RIFFchunk-50-WAVE",
                {
                    "X-Doorfast-Generation": "7",
                    "X-Doorfast-Audio-Previous-Revision": "40",
                    "X-Doorfast-Audio-Revision": "50",
                    "ETag": '"df-audio-7-50"',
                },
            ),
        )

        self.assertEqual(b"RIFFchunk-40-WAVE", await client.latest_audio_chunk())
        self.assertEqual(b"RIFFchunk-50-WAVE", await client.latest_audio_chunk())
        second = client.session.requests[1][1]
        self.assertEqual({"generation": 7, "after": 40}, second["params"])
        self.assertEqual({}, second["headers"])

    async def test_drops_old_response_when_generation_changes_during_read(self):
        read_started = asyncio.Event()
        release_read = asyncio.Event()

        async def block_read():
            read_started.set()
            await release_read.wait()

        client = audio_client(
            FakeResponse(
                200,
                b"RIFFold-call-WAVE",
                {
                    "X-Doorfast-Generation": "7",
                    "X-Doorfast-Audio-Previous-Revision": "30",
                    "X-Doorfast-Audio-Revision": "40",
                    "ETag": '"df-audio-7-40"',
                },
                before_read=block_read,
            )
        )
        task = asyncio.create_task(client.latest_audio_chunk())
        await read_started.wait()
        client.status = {
            "call": {"generation": 8},
            "audio": {
                "snapshot_ready": True,
                "generation": 8,
                "snapshot_packet_count": 2,
            },
        }
        client._clear_audio_cursor()
        release_read.set()

        self.assertIsNone(await task)
        self.assertIsNone(client._audio_generation)
        self.assertIsNone(client._audio_revision)

    async def test_drops_old_response_when_runtime_changes_with_same_generation(self):
        read_started = asyncio.Event()
        release_read = asyncio.Event()

        async def block_read():
            read_started.set()
            await release_read.wait()

        client = audio_client(
            FakeResponse(
                200,
                b"RIFFold-runtime-WAVE",
                {
                    "X-Doorfast-Generation": "7",
                    "X-Doorfast-Audio-Previous-Revision": "30",
                    "X-Doorfast-Audio-Revision": "40",
                    "ETag": '"df-audio-7-40"',
                },
                before_read=block_read,
            )
        )
        task = asyncio.create_task(client.latest_audio_chunk())
        await read_started.wait()
        client.status = {
            "runtime_id": "bbbbbbbbbbbbbbbb",
            "call": {"generation": 7},
            "audio": {
                "snapshot_ready": True,
                "generation": 7,
                "snapshot_packet_count": 2,
            },
        }
        client._clear_audio_cursor()
        release_read.set()

        self.assertIsNone(await task)
        self.assertIsNone(client._audio_generation)
        self.assertIsNone(client._audio_revision)
        self.assertIsNone(client._audio_etag)

    async def test_out_of_order_refresh_cannot_restore_old_call_audio(self):
        audio_started = asyncio.Event()
        release_audio = asyncio.Event()
        old_refresh_started = asyncio.Event()
        release_old_refresh = asyncio.Event()

        async def block_audio():
            audio_started.set()
            await release_audio.wait()

        client = audio_client(
            FakeResponse(
                200,
                b"RIFFold-call-WAVE",
                {
                    "X-Doorfast-Generation": "7",
                    "X-Doorfast-Audio-Previous-Revision": "30",
                    "X-Doorfast-Audio-Revision": "40",
                    "ETag": '"df-audio-7-40"',
                },
                before_read=block_audio,
            )
        )
        refresh_calls = 0

        async def refresh_response(method, path, payload=None):
            nonlocal refresh_calls
            refresh_calls += 1
            if refresh_calls == 1:
                old_refresh_started.set()
                await release_old_refresh.wait()
                return {
                    "call": {"generation": 7},
                    "audio": {
                        "snapshot_ready": True,
                        "generation": 7,
                        "snapshot_packet_count": 40,
                    },
                }
            return {
                "call": {"generation": 8},
                "audio": {
                    "snapshot_ready": True,
                    "generation": 8,
                    "snapshot_packet_count": 2,
                },
            }

        client._request = refresh_response
        audio_task = asyncio.create_task(client.latest_audio_chunk())
        await audio_started.wait()
        old_refresh = asyncio.create_task(client.refresh())
        await old_refresh_started.wait()
        new_refresh = asyncio.create_task(client.refresh())
        await new_refresh
        release_old_refresh.set()
        old_result = await old_refresh
        release_audio.set()

        self.assertEqual(8, client.status["call"]["generation"])
        self.assertEqual(7, old_result["call"]["generation"])
        self.assertIsNone(await audio_task)
        self.assertIsNone(client._audio_generation)

    async def test_resynchronizes_expired_cursor_but_retries_transient_failure(self):
        client = audio_client(FakeResponse(503), FakeResponse(409))
        client._audio_generation = 7
        client._audio_revision = 30
        client._audio_etag = '"df-audio-7-30"'

        self.assertIsNone(await client.latest_audio_chunk())
        self.assertEqual(30, client._audio_revision)
        self.assertIsNone(await client.latest_audio_chunk())
        self.assertIsNone(client._audio_generation)
        self.assertIsNone(client._audio_revision)

    async def test_refresh_clears_cursor_when_call_generation_changes(self):
        client = audio_client()
        client._audio_generation = 7
        client._audio_revision = 40
        client._audio_etag = '"df-audio-7-40"'
        client._request = AsyncMock(
            return_value={
                "call": {"generation": 8},
                "audio": {"snapshot_ready": False, "generation": 0},
            }
        )

        await client.refresh()

        self.assertIsNone(client._audio_generation)
        self.assertIsNone(client._audio_revision)
        self.assertIsNone(client._audio_etag)

if __name__ == "__main__":
    unittest.main()
