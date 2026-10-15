"""Preview stop fences exercise real services, HTTP client and status consumers."""

import ast
import asyncio
from copy import deepcopy
from dataclasses import asdict
import logging
from pathlib import Path
import socket
import types
import unittest
from unittest.mock import patch

from tests.test_client import DoorfastClient
from tests import test_station_status_entities as entities
from tests import test_webrtc as webrtc

monitor_module = entities.load_component("monitor")
stations_module = entities.load_component("stations")
setup_module = entities.load_component("setup_lifecycle")
polling_module = entities.load_component("polling")
routing_module = entities.load_component("routing")
generation_module = entities.load_component("generation")
RUNTIME = "0123456789abcdef"
REBOOT = "fedcba9876543210"
COMPONENT = Path(__file__).parents[1] / "custom_components/doorfast"


def snapshot(revision=40, *, generation=17, live=True, runtime=RUNTIME):
    return {
        "runtime_id": runtime,
        "call": {"session": "ended", "generation": 13, "station_id": ""},
        "station_call": {"operation_id": 11, "station_id": "gate_main", "stage": "ended",
                         "media_generation": 12, "call_generation": 12, "timer": "none",
                         "remaining_ms": 0, "error": "none"},
        "media": {"available": True, "status_revision": revision, "sessions": [{
            "station_id": "gate_main", "generation": generation, "purpose": "preview",
            "state": "publishing", "ready": True, "source_live": True,
            "encoder_running": True, "status_revision": revision,
        }] if live else []},
    }


class Reply:
    status = 200

    def __init__(self, payload, gate=None):
        self.payload, self.gate = deepcopy(payload), gate

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        pass

    async def json(self, **kwargs):
        if self.gate is not None:
            entered, resume = self.gate
            entered.set()
            await resume.wait()
        return deepcopy(self.payload)


class HttpBoundary:
    """Freeze each reply at request time, then release it deterministically."""

    def __init__(self):
        self.current = snapshot()
        self.requests = []
        self.gates = {}
        self.stop_error = None
        self.call_error = None
        self.call_media_generation = 17
        self.call_reply = None
        self.omit_monitor_runtime = False

    def request(self, method, url, **kwargs):
        if not url.startswith("http://synthetic.invalid/"):
            raise AssertionError("unexpected target")
        path = url.removeprefix("http://synthetic.invalid")
        self.requests.append((method, path, kwargs.get("json")))
        if (method, path) == ("GET", "/api/v1/status"):
            payload = self.current
        elif (method, path) == ("GET", "/api/v1/stations"):
            rows = [asdict(entities.station()), asdict(entities.station("gate_side", "Side gate"))]
            for row in rows:
                row["id"] = row.pop("station_id")
            payload = {"runtime_id": self.current["runtime_id"], "revision": 1, "stations": rows}
        elif (method, path) == ("GET", "/api/v1/monitor/status"):
            payload = {"runtime_id": self.current["runtime_id"], **self.current["media"]}
            if self.omit_monitor_runtime:
                payload.pop("runtime_id")
        elif (method, path) == ("POST", "/api/v1/monitor/stop"):
            if self.stop_error:
                raise self.stop_error
            if kwargs["json"] not in [{"runtime_id": RUNTIME, "station_id": station, "generation": generation}
                    for station, generation in (("gate_main", 17), ("gate_main", 18), ("gate_side", 20))]:
                raise AssertionError("wrong stop identity")
            payload = {**kwargs["json"], "state": "stopping"}
        elif (method, path) == ("POST", "/api/v1/monitor/start"):
            side = [item for item in self.current["media"]["sessions"] if item["station_id"] == "gate_side"]
            self.current = snapshot(43, generation=18)
            payload = {"runtime_id": RUNTIME, **self.current["media"]["sessions"][0]}
            self.current["media"]["sessions"].extend(side)
        elif (method, path) == ("POST", "/api/v1/monitor/viewer"):
            identity = kwargs["json"]
            if (identity.get("runtime_id") != RUNTIME or type(identity.get("active")) is not bool
                    or (identity.get("station_id"), identity.get("generation")) not in {
                        ("gate_main", 17), ("gate_main", 18), ("gate_side", 20)}):
                raise AssertionError("wrong viewer identity")
            payload = {**identity, "state": "queued"}
        elif (method, path) == ("POST", "/api/v1/call"):
            if self.call_error:
                raise self.call_error
            self.current = snapshot(44, generation=self.call_media_generation)
            self.current["station_call"].update(operation_id=12, stage="preparing",
                media_generation=self.call_media_generation, call_generation=0, timer="startup", remaining_ms=30000)
            payload = {"runtime_id": RUNTIME, "station_id": "gate_main", "operation_id": 12,
                       "generation": self.call_media_generation, "state": "preparing", "queued": True}
            if self.call_reply is not None:
                payload = self.call_reply
        else:
            raise AssertionError("unexpected HTTP route")
        return Reply(payload, self.gates.pop(path, None))


class PreviewLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        network = patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.http = HttpBoundary()
        self.client = DoorfastClient(None, "http://synthetic.invalid")
        self.client.session = self.http
        self.client.status, self.client.online = deepcopy(self.http.current), True
        self.registry = stations_module.StationRegistryCoordinator(self.client, entry_id="entry-1")
        await self.registry.async_refresh()
        self.coordinator = self.registry.monitor("gate_main")
        await self.coordinator.async_apply_status({"runtime_id": RUNTIME, **self.http.current["media"]["sessions"][0]})
        self.hass = entities.FakeHass(self.registry)
        self.sensor = entities.sensor.DoorfastPreviewStatus(self.client, "entry-1", self.registry.station("gate_main"))
        self.sensor.hass, self.sensor.entity_id = self.hass, "sensor.synthetic_preview"
        self.writes = []
        self.sensor.async_write_ha_state = lambda: self.writes.append(deepcopy(self.sensor.extra_state_attributes))
        await self.sensor.async_added_to_hass()

        async def reconcile(*args):
            pass

        def dispatch(hass, signal, data):
            for listener in tuple(hass.listeners.get(signal, ())):
                listener(data)

        self.dispatch = lambda: dispatch(self.hass, "doorfast_entry-1_STATUS", self.client.status)
        self.functions = self.install_services(self.client, self.registry, "entry-1", dispatch, reconcile)
        self.call = types.SimpleNamespace(data={"config_entry_id": "entry-1", "station_id": "gate_main"})

    def install_services(self, client, registry, entry_id, dispatch, reconcile):
        source = ast.parse((COMPONENT / "__init__.py").read_text())
        setup = next(n for n in source.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_setup_entry")
        functions = [n for n in setup.body if isinstance(n, ast.AsyncFunctionDef) and n.name in {
            "start_monitor", "stop_monitor", "sync_current_monitor", "dispatch_status", "poll"}]
        wiring = [n for n in setup.body if isinstance(n, ast.Assign) and any(
            isinstance(target, ast.Attribute) and target.attr in {"monitor_status_callback", "async_poll"}
            for target in n.targets)]
        resource = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == "_service_resource")
        functions_scope = {"hass": self.hass, "entry": types.SimpleNamespace(entry_id=entry_id),
            "client": client, "station_registry": registry,
            "provider": types.SimpleNamespace(async_reconcile_monitor=reconcile),
            "pcm_ws": types.SimpleNamespace(reconcile=reconcile, release_call=reconcile),
            "DOMAIN": "doorfast", "STATIONS_KEY": "doorfast_stations", "RING_STATUS": "ring_status",
            "HomeAssistantError": RuntimeError, "select_client": routing_module.select_client,
            "async_poll": polling_module.async_poll, "sync_monitor_state": setup_module.sync_monitor_state,
            "async_dispatcher_send": dispatch, "is_ringing": generation_module.is_ringing,
            "_LOGGER": logging.getLogger(__name__)}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[resource, *functions, *wiring], type_ignores=[])),
                     str(COMPONENT / "__init__.py"), "exec"), functions_scope)
        return functions_scope

    async def delayed_poll(self, path):
        entered, resume = asyncio.Event(), asyncio.Event()
        self.http.gates[path] = (entered, resume)
        task = asyncio.create_task(self.functions["poll"]())
        await asyncio.wait_for(entered.wait(), 1)
        self.addAsyncCleanup(self.finish_task, task, resume)
        return task, resume

    async def finish_task(self, task, resume):
        resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_delayed_nonempty_snapshot_cannot_revive_stopped_preview(self):
        self.http.current = snapshot(41)
        pending, resume = await self.delayed_poll("/api/v1/monitor/status")
        await self.functions["stop_monitor"](self.call)
        self.http.current = snapshot(42, live=False)
        resume.set()
        await asyncio.wait_for(pending, 1)
        self.assertIsNone(self.coordinator.generation)
        self.assertFalse(self.coordinator.ready)
        await self.functions["start_monitor"](self.call)
        self.assertEqual(18, self.coordinator.generation)
        self.assertEqual(1, sum(path == "/api/v1/monitor/start" for _, path, _ in self.http.requests))

    async def test_stop_ack_notifies_stopping_before_remote_completion_then_refreshes_idle(self):
        await self.functions["stop_monitor"](self.call)
        self.assertEqual(("stopping", False), (self.sensor.native_value, self.sensor.extra_state_attributes["source_live"]))
        self.assertIn("stopping", [row["phase"] for row in self.writes[1:]])
        self.http.current = snapshot(42, live=False)
        await self.functions["poll"]()
        self.assertEqual("idle", self.sensor.native_value)

    async def test_stop_service_refreshes_without_waiting_for_periodic_poll(self):
        self.http.current = snapshot(42, live=False)
        await self.functions["stop_monitor"](self.call)
        self.assertEqual("idle", self.writes[-1]["phase"])
        self.assertTrue(any(path == "/api/v1/status" for _, path, _ in self.http.requests))

    async def test_services_refresh_selected_entry_not_registration_closure(self):
        other_http = HttpBoundary()
        other_client = DoorfastClient(None, "http://synthetic.invalid")
        other_client.session = other_http
        other_client.status, other_client.online = snapshot(), True
        other_registry = stations_module.StationRegistryCoordinator(other_client, entry_id="entry-2")
        await other_registry.async_refresh()
        await other_registry.monitor("gate_main").async_apply_status({
            "runtime_id": RUNTIME, **snapshot()["media"]["sessions"][0]})
        self.hass.data["doorfast"]["entry-2"] = other_client
        self.hass.data["doorfast_stations"]["entry-2"] = other_registry
        polled_entries = []

        async def reconcile(*args):
            polled_entries.append("entry-2")

        self.install_services(other_client, other_registry, "entry-2",
            lambda *args: None, reconcile)
        call = types.SimpleNamespace(data={"config_entry_id": "entry-2", "station_id": "gate_main"})
        for service in ("stop_monitor", "start_monitor"):
            with self.subTest(service=service):
                self.http.requests.clear()
                other_http.requests.clear()
                polled_entries.clear()
                if service == "stop_monitor":
                    other_http.current = snapshot(42, live=False)
                await self.functions[service](call)
                self.assertEqual([], self.http.requests, "entry B service refreshed entry A")
                self.assertTrue(any(path == "/api/v1/status" for _, path, _ in other_http.requests))
                self.assertTrue(polled_entries)

    async def test_legacy_monitor_reply_cannot_revive_stopped_preview(self):
        self.http.omit_monitor_runtime = True
        self.http.current = snapshot(41)
        await self.functions["stop_monitor"](self.call)
        self.assertIsNone(self.coordinator.generation)
        await self.functions["start_monitor"](self.call)
        self.assertEqual(18, self.coordinator.generation)
        self.assertEqual(1, sum(path == "/api/v1/monitor/start" for _, path, _ in self.http.requests))

    async def test_failed_call_does_not_release_stop_fence(self):
        await self.functions["stop_monitor"](self.call)
        self.http.call_error = RuntimeError("synthetic call rejected")
        with self.assertRaises(RuntimeError):
            await self.coordinator.async_call()
        self.http.current = snapshot(41)
        await self.functions["poll"]()
        self.assertEqual("stopping", self.sensor.native_value)
        self.assertIsNone(self.coordinator.generation)

    async def test_accepted_call_can_borrow_preview_before_shared_status_catches_up(self):
        await self.functions["stop_monitor"](self.call)
        await self.coordinator.async_call()
        # Control acceptance is real, but an independent status reader can lag.
        self.http.current = snapshot(41)
        reply = await self.client.monitor_status()
        self.assertEqual(17, reply["sessions"][0]["generation"])

    async def test_accepted_call_with_new_media_cannot_unstop_old_preview(self):
        await self.functions["stop_monitor"](self.call)
        self.http.call_media_generation = 18
        response = await self.coordinator.async_call()
        self.assertEqual(18, response["generation"])
        self.http.current = snapshot(41)
        await self.functions["poll"]()
        self.assertEqual("stopping", self.sensor.native_value)
        self.assertIsNone(self.coordinator.generation)

    async def test_unbound_or_terminal_call_reply_does_not_release_stop_fence(self):
        await self.functions["stop_monitor"](self.call)
        for reply in ({"state": "preparing"}, {"generation": 0, "state": "preparing", "queued": True},
                      {"generation": 17, "state": "failed", "queued": False}):
            with self.subTest(reply=reply):
                self.http.call_reply = reply
                try:
                    response = await self.client.call_station("gate_main")
                except KeyError as error:
                    self.fail(f"legacy call reply no longer accepted: {error}")
                self.assertEqual(reply, response)
                self.http.current = snapshot(41)
                await self.client.refresh()
                self.assertEqual("stopping", self.sensor.native_value)

    async def assert_new_owner_does_not_adopt_old_preview(self, *, incoming):
        await self.functions["stop_monitor"](self.call)
        self.http.current = snapshot(41)
        entered, resume = asyncio.Event(), asyncio.Event()
        self.http.gates["/api/v1/monitor/status"] = (entered, resume)
        pending = asyncio.create_task(self.functions["sync_current_monitor"]())
        self.addAsyncCleanup(self.finish_task, pending, resume)
        await asyncio.wait_for(entered.wait(), 1)
        self.http.current = snapshot(44, generation=18)
        if incoming:
            self.http.current["call"] = {"session": "talking", "station_id": "gate_main", "generation": 42}
            self.http.current["media"]["sessions"][0]["purpose"] = "call"
        else:
            self.http.current["station_call"].update(operation_id=12, stage="preparing",
                media_generation=18, call_generation=0, timer="startup", remaining_ms=30000)
        await self.client.refresh()
        resume.set()
        await asyncio.wait_for(pending, 1)
        self.assertNotEqual(17, self.coordinator.generation,
            "new control owner incorrectly released a different public-generation fence")
        self.assertEqual(18, self.client.status["media"]["sessions"][0]["generation"])

    async def test_new_operation_media_owner_does_not_unstop_old_generation(self):
        await self.assert_new_owner_does_not_adopt_old_preview(incoming=False)

    async def test_new_native_media_owner_does_not_unstop_old_generation(self):
        await self.assert_new_owner_does_not_adopt_old_preview(incoming=True)

    async def test_late_stop_ack_does_not_hide_accepted_call_before_status_sample(self):
        entered, resume = asyncio.Event(), asyncio.Event()
        self.http.gates["/api/v1/monitor/stop"] = (entered, resume)
        pending = asyncio.create_task(self.client.stop_monitor(RUNTIME, "gate_main", 17))
        self.addAsyncCleanup(self.finish_task, pending, resume)
        await asyncio.wait_for(entered.wait(), 1)
        await self.coordinator.async_call()
        resume.set()
        await asyncio.wait_for(pending, 1)
        self.assertEqual("publishing", self.client.status["media"]["sessions"][0]["state"])

    async def test_legacy_delayed_reply_keeps_request_runtime_across_reboot(self):
        self.http.omit_monitor_runtime = True
        entered, resume = asyncio.Event(), asyncio.Event()
        self.http.gates["/api/v1/monitor/status"] = (entered, resume)
        pending = asyncio.create_task(self.client.monitor_status())
        self.addAsyncCleanup(self.finish_task, pending, resume)
        await asyncio.wait_for(entered.wait(), 1)
        self.http.current = snapshot(1, runtime=REBOOT)
        await self.client.refresh()
        resume.set()
        reply = await asyncio.wait_for(pending, 1)
        self.assertEqual(RUNTIME, reply["runtime_id"])
        self.assertEqual(REBOOT, self.client.status["runtime_id"])

    async def test_active_webrtc_stop_is_exact_and_reopen_uses_new_generation(self):
        main_ws, side_ws, new_ws = (webrtc.FakeWebSocket() for _ in range(3))
        provider = webrtc.DoorfastWebRTCProvider(self.hass, "entry-1", self.registry,
            "http://synthetic.invalid", webrtc.FakeSession(main_ws, side_ws, new_ws))
        self.functions["provider"] = provider
        side = {"station_id": "gate_side", "generation": 20, "purpose": "preview",
            "state": "publishing", "ready": True, "source_live": True,
            "encoder_running": True, "status_revision": 40}
        self.http.current["media"]["sessions"].append(side)
        self.client.status = deepcopy(self.http.current)
        side_coordinator = self.registry.monitor("gate_side")
        await side_coordinator.async_apply_status({"runtime_id": RUNTIME, **side})

        async def open_offer(websocket, station_id, session_id):
            messages = []
            await websocket.incoming.put({"type": "webrtc/answer", "value": "answer"})
            await asyncio.wait_for(provider.async_handle_async_webrtc_offer(
                webrtc.FakeCamera(webrtc.source(station_id)), "offer", session_id, messages.append), 1)
            self.assertFalse(any(isinstance(message, webrtc.WebRTCError) for message in messages))

        try:
            await open_offer(main_ws, "gate_main", "main-old")
            await open_offer(side_ws, "gate_side", "side")
            self.assertEqual((1, 1), (self.coordinator.viewer_count, side_coordinator.viewer_count))
            self.http.current["media"]["status_revision"] = 41
            pending, resume = await self.delayed_poll("/api/v1/monitor/status")
            await self.functions["stop_monitor"](self.call)
            self.assertTrue(main_ws.closed)
            self.assertFalse(side_ws.closed)
            self.assertEqual(1, side_coordinator.viewer_count)
            self.http.current = snapshot(42, live=False)
            self.http.current["media"]["sessions"].append(side)
            await self.functions["poll"]()
            resume.set()
            await asyncio.wait_for(pending, 1)
            self.assertIsNone(self.coordinator.generation)
            await open_offer(new_ws, "gate_main", "main-new")
            self.assertEqual((18, 1), (self.coordinator.generation, self.coordinator.viewer_count))
            self.assertFalse(new_ws.closed)
            self.assertFalse(side_ws.closed)
            self.assertEqual(1, sum(path == "/api/v1/monitor/start" for _, path, _ in self.http.requests))
            activations = [body for _, path, body in self.http.requests
                if path == "/api/v1/monitor/viewer" and body["active"]]
            self.assertEqual([("gate_main", 17), ("gate_side", 20), ("gate_main", 18)],
                [(body["station_id"], body["generation"]) for body in activations])
        finally:
            await provider.async_close_entry()
            await self.registry.async_close()

    async def test_delayed_global_snapshot_cannot_restore_live_after_stop(self):
        self.http.current = snapshot(41)
        pending, resume = await self.delayed_poll("/api/v1/status")
        await self.functions["stop_monitor"](self.call)
        self.http.current = snapshot(42, live=False)
        resume.set()
        await asyncio.wait_for(pending, 1)
        self.assertNotEqual("live", self.writes[-1]["phase"])

    async def test_delayed_empty_snapshot_cannot_clear_newer_start(self):
        self.http.current = snapshot(41, live=False)
        pending, resume = await self.delayed_poll("/api/v1/monitor/status")
        await self.functions["stop_monitor"](self.call)
        await self.functions["start_monitor"](self.call)
        resume.set()
        await asyncio.wait_for(pending, 1)
        self.assertEqual((18, True), (self.coordinator.generation, self.coordinator.ready))

    async def test_stop_fence_preserves_other_station_and_new_incoming_call(self):
        await self.functions["stop_monitor"](self.call)
        incoming = snapshot(44)
        incoming["call"] = {"session": "talking", "station_id": "gate_main", "generation": 42}
        incoming["media"]["sessions"][0]["purpose"] = "call"
        incoming["media"]["sessions"].append({"station_id": "gate_side", "generation": 20,
            "purpose": "preview", "state": "publishing", "ready": True, "source_live": True})
        self.http.current = incoming
        await self.functions["poll"]()
        self.assertEqual("in_call", self.sensor.native_value)
        self.assertTrue(self.sensor.extra_state_attributes["source_live"])
        self.assertEqual(incoming["call"], self.client.status["call"])
        self.assertEqual(incoming["media"]["sessions"][1], self.client.status["media"]["sessions"][1])

    async def test_old_stop_reply_cannot_mark_new_runtime_stopping(self):
        entered, resume = asyncio.Event(), asyncio.Event()
        self.http.gates["/api/v1/monitor/stop"] = (entered, resume)
        pending = asyncio.create_task(self.coordinator.async_stop())
        self.addAsyncCleanup(self.finish_task, pending, resume)
        await asyncio.wait_for(entered.wait(), 1)
        self.http.current = snapshot(1, runtime=REBOOT)
        await self.client.refresh()
        resume.set()
        await asyncio.wait_for(pending, 1)
        self.assertEqual(REBOOT, self.client.status["runtime_id"])
        self.assertEqual(("publishing", True), (self.client.status["media"]["sessions"][0]["state"],
            self.client.status["media"]["sessions"][0]["source_live"]))

    async def test_failed_stop_does_not_fabricate_completed_or_stopping_status(self):
        self.http.stop_error = RuntimeError("synthetic stop failure")
        with self.assertRaises(RuntimeError):
            await self.functions["stop_monitor"](self.call)
        self.assertEqual("live", self.sensor.native_value)

    async def test_stop_before_first_status_sample_still_notifies_pending_not_idle(self):
        self.client.status = snapshot(live=False)
        await self.coordinator.async_stop()
        self.assertEqual(("stopping", False), (self.writes[-1]["phase"], self.writes[-1]["source_live"]))

    async def test_stop_fence_does_not_break_new_call_borrowing_same_preview(self):
        await self.functions["stop_monitor"](self.call)
        await self.coordinator.async_call()
        await self.functions["poll"]()
        self.assertEqual("calling", self.sensor.native_value)
        self.assertEqual(12, self.sensor.extra_state_attributes["station_call_operation_id"])
        self.assertEqual((17, True), (self.coordinator.generation, self.coordinator.ready))

    async def test_late_stop_ack_preserves_new_incoming_owner(self):
        entered, resume = asyncio.Event(), asyncio.Event()
        self.http.gates["/api/v1/monitor/stop"] = (entered, resume)
        pending = asyncio.create_task(self.coordinator.async_stop())
        self.addAsyncCleanup(self.finish_task, pending, resume)
        await asyncio.wait_for(entered.wait(), 1)
        self.http.current = snapshot(44)
        self.http.current["call"] = {"session": "talking", "station_id": "gate_main", "generation": 42}
        self.http.current["media"]["sessions"][0]["purpose"] = "call"
        await self.client.refresh()
        resume.set()
        await asyncio.wait_for(pending, 1)
        self.assertEqual(("in_call", True), (self.sensor.native_value, self.sensor.extra_state_attributes["source_live"]))

    async def test_pending_stop_tolerates_unusable_foreign_station_row(self):
        foreign = {"station_id": [], "purpose": "preview", "generation": 99}
        self.client.status["media"]["sessions"].append(foreign)
        try:
            await self.coordinator.async_stop()
        except TypeError as error:
            self.fail(f"unusable foreign row broke an accepted stop: {error}")
        self.assertEqual("stopping", self.client.status["media"]["sessions"][0]["state"])
        self.assertEqual(foreign, self.client.status["media"]["sessions"][1])

    async def test_sync_fences_nonempty_result_even_without_client_projection(self):
        entered, resume = asyncio.Event(), asyncio.Event()

        async def monitor_status():
            entered.set()
            await resume.wait()
            return {"runtime_id": RUNTIME, **snapshot(41)["media"]}

        observer = types.SimpleNamespace(status=snapshot(), monitor_status=monitor_status)
        pending = asyncio.create_task(setup_module.sync_monitor_state(
            observer, self.registry, self.functions["provider"]))
        self.addAsyncCleanup(self.finish_task, pending, resume)
        await asyncio.wait_for(entered.wait(), 1)
        await self.coordinator.async_stop()
        resume.set()
        await asyncio.wait_for(pending, 1)
        self.assertIsNone(self.coordinator.generation)


if __name__ == "__main__":
    unittest.main()
