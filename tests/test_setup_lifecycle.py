from __future__ import annotations

import importlib.util
import ast
import asyncio
import logging
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / "doorfast"


class FakeServices:
    def __init__(self, names=()):
        self.names = set(names)
        self.removed = []

    def has_service(self, domain, name):
        return (domain, name) in self.names

    def async_remove(self, domain, name):
        self.removed.append((domain, name))
        self.names.discard((domain, name))


class FakeConfigEntries:
    def __init__(self, should_fail=False):
        self.should_fail = should_fail
        self.unload_calls = []

    async def async_unload_platforms(self, entry, platforms):
        self.unload_calls.append((entry.entry_id, tuple(platforms)))
        if self.should_fail:
            raise RuntimeError("platform unload failed")


class FakeHass:
    def __init__(self, service_names=()):
        self.data = {}
        self.services = FakeServices(("doorfast", name) for name in service_names)
        self.config_entries = FakeConfigEntries()


class FakeEntry:
    entry_id = "entry-1"


class FakePcm:
    def __init__(self):
        self.released = []

    async def close_entry(self, entry_id):
        self.released.append(entry_id)


class FakeMonitor:
    def __init__(self):
        self.closed = 0

    async def async_close(self):
        self.closed += 1


class FakeProvider:
    def __init__(self):
        self.closed = 0
        self.reconciled = 0

    async def async_close_entry(self):
        self.closed += 1

    async def async_reconcile_monitor(self):
        self.reconciled += 1


class FakeRegistry:
    def __init__(self):
        self.closed = 0

    async def async_close(self):
        self.closed += 1


class FakeStatusMonitor:
    def __init__(self):
        self.applied = []
        self.generation = None
        self.status_revision = 0

    async def async_apply_status(self, status):
        self.applied.append(status)


class FakeStationRegistry:
    def __init__(self):
        self.monitors = {
            "gate_main": FakeStatusMonitor(),
            "gate_side": FakeStatusMonitor(),
        }

    @property
    def station_ids(self):
        return tuple(self.monitors)

    def monitor(self, station_id):
        return self.monitors[station_id]


def load_module():
    # Import the helper without loading Home Assistant-dependent package code.
    package = types.ModuleType("custom_components.doorfast")
    package.__path__ = [str(COMPONENT)]
    sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
    sys.modules["custom_components.doorfast"] = package
    const_spec = importlib.util.spec_from_file_location(
        "custom_components.doorfast.const", COMPONENT / "const.py"
    )
    const_module = importlib.util.module_from_spec(const_spec)
    sys.modules["custom_components.doorfast.const"] = const_module
    assert const_spec.loader is not None
    const_spec.loader.exec_module(const_module)
    spec = importlib.util.spec_from_file_location(
        "custom_components.doorfast.setup_lifecycle", COMPONENT / "setup_lifecycle.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


MODULE = load_module()


class AudioEntryLifecycleTest(unittest.IsolatedAsyncioTestCase):
    """Execute actual entry functions; substitute only HA and HTTP boundaries."""

    async def test_audio_fixture_keeps_one_module_and_exception_identity(self):
        import importlib

        qualified = importlib.import_module("tests.test_audio_contract")
        natural = importlib.import_module("test_audio_contract")
        self.assertIs(qualified, natural)
        self.assertIs(qualified.AudioStreamError,
                      sys.modules[f"{qualified.PACKAGE}.client_types"].AudioStreamError)

    async def asyncSetUp(self):
        import test_audio_contract as audio
        self.audio = audio
        self.http = audio.HttpBoundary()
        self.client = audio.client_module.DoorfastClient(None, "http://synthetic.invalid")
        self.client.session = self.http
        self.client.status = audio.status()
        self.hass = FakeHass()
        self.hass.async_create_task = asyncio.create_task
        self.hass.data["doorfast"] = {"entry-1": self.client}
        self.manager = audio.ws.PcmWebSocketManager(self.hass)
        self.hass.data["doorfast_pcm_ws"] = self.manager
        self.entry = types.SimpleNamespace(
            entry_id="entry-1", data={"server_address": "http://synthetic.invalid"}, options={},
            async_on_unload=lambda *args: None, add_update_listener=lambda *args: None,
        )
        self.forward_entered = asyncio.Event()
        self.forward_resume = asyncio.Event()
        self.forward_error = None
        self.unload_result = True
        self.provider = FakeProvider()

        async def forward(*args):
            self.forward_entered.set()
            await self.forward_resume.wait()
            if self.forward_error:
                raise self.forward_error

        async def unload(*args):
            return self.unload_result

        async def noop(*args, **kwargs):
            pass

        self.hass.config_entries = types.SimpleNamespace(
            async_forward_entry_setups=forward, async_unload_platforms=unload,
        )
        self.hass.http = types.SimpleNamespace(register_view=lambda *args: None)
        self.hass.services.async_register = lambda *args: None
        const = sys.modules[MODULE.__package__ + ".const"]
        namespace = {key: value for key, value in vars(const).items() if key.isupper()}
        namespace.update(
            PcmWebSocketManager=audio.ws.PcmWebSocketManager,
            DoorfastClient=lambda *args: self.client,
            StationRegistryCoordinator=lambda *args, **kwargs: types.SimpleNamespace(
                station_ids=(), async_refresh=noop, async_close=noop),
            DoorfastWebRTCProvider=lambda *args, **kwargs: self.provider,
            EventGate=object, DoorfastEventView=lambda *args: types.SimpleNamespace(),
            async_poll=__import__(MODULE.__package__ + ".polling", fromlist=["async_poll"]).async_poll,
            sync_monitor_state=MODULE.sync_monitor_state,
            rollback_entry_setup=MODULE.rollback_entry_setup,
            async_register_frontend=noop, async_unregister_frontend=noop,
            async_register_webrtc_provider=lambda *args: lambda: None,
            async_dispatcher_send=lambda *args: None, is_ringing=lambda *args: False,
            dr=types.SimpleNamespace(async_get=lambda hass: types.SimpleNamespace(async_get_or_create=lambda **kwargs: None)),
            async_track_time_interval=lambda *args: lambda: None,
            _async_update_listener=lambda *args: None, timedelta=lambda **kwargs: None,
            _LOGGER=logging.getLogger(__name__), SERVICE_NAMES=(),
        )
        source = ast.parse((COMPONENT / "__init__.py").read_text())
        functions = [node for node in source.body if isinstance(node, ast.AsyncFunctionDef)
                     and node.name in {"async_setup_entry", "async_unload_entry"}]
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *functions], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), str(COMPONENT / "__init__.py"), "exec"), namespace)
        self.setup_entry = namespace["async_setup_entry"]
        self.unload_entry = namespace["async_unload_entry"]

    async def asyncTearDown(self):
        self.forward_resume.set()
        await self.manager.release_entry("entry-1")

    async def listen(self):
        connection = self.audio.Connection()
        await self.manager.listen_start(connection, {
            "id": 1, "config_entry_id": "entry-1", "station_id": "gate_main",
        })
        return connection

    async def assert_closed(self):
        connection = await self.listen()
        self.assertEqual(["not_loaded"], [error.code for error in connection.errors])
        await self.manager.start(connection, {"id": 2, "config_entry_id": "entry-1"})
        self.assertEqual("not_loaded", connection.errors[-1].code)
        self.assertEqual([], connection.results)

    async def test_runtime_replacement_poll_and_service_revoke_initial_microphone_refresh(self):
        from importlib import import_module

        audio = self.audio
        stations = import_module(f"{audio.PACKAGE}.stations")
        polling = import_module(f"{audio.PACKAGE}.polling")
        routing = import_module(f"{audio.PACKAGE}.routing")
        generation = import_module(f"{audio.PACKAGE}.generation")
        registry = stations.StationRegistryCoordinator(self.client, entry_id="entry-1")
        self.hass.data["doorfast_stations"] = {"entry-1": registry}
        station_row = {"id": "gate_main", "name": "Main gate", "logical_address": "32:02:01:00:02:01",
            "enabled": True, "stream_name": "doorfast_gate_main", "route_source": "configured",
            "route_fresh": True, "monitorable": True, "last_seen_ms": 0}
        mic_entered, mic_resume = asyncio.Event(), asyncio.Event()
        monitor_entered, monitor_resume = asyncio.Event(), asyncio.Event()
        original_request = self.http.request
        first_refresh = True
        hangups, dispatched = [], []

        class InitialStatus(audio.Response):
            async def __aenter__(response):
                mic_entered.set()
                await mic_resume.wait()
                return response

        class MonitorStatus(audio.Response):
            async def __aenter__(response):
                monitor_entered.set()
                await monitor_resume.wait()
                return response

        def request(method, url, **kwargs):
            nonlocal first_refresh
            if url.endswith("/stations"):
                return audio.Response(payload={"runtime_id": self.http.current_status["runtime_id"],
                    "revision": 1, "stations": [station_row]})
            if url.endswith("/monitor/status"):
                return MonitorStatus(payload={"runtime_id": audio.REBOOT_RUNTIME, "sessions": []})
            if url.endswith("/status") and first_refresh:
                first_refresh = False
                return InitialStatus(payload=audio.status(audio.REBOOT_RUNTIME))
            if url.endswith("/hangup"):
                hangups.append(kwargs["json"])
                return audio.Response(payload={"runtime_id": audio.REBOOT_RUNTIME, "station_id": "gate_main",
                    "generation": 0, "operation_id": 0, "queued": True, "state": "ending"})
            return original_request(method, url, **kwargs)

        self.http.request = request
        await registry.async_refresh()
        old = registry.monitor("gate_main")
        source = ast.parse((COMPONENT / "__init__.py").read_text())
        setup = next(node for node in source.body if isinstance(node, ast.AsyncFunctionDef)
                     and node.name == "async_setup_entry")
        functions = [node for node in setup.body if isinstance(node, ast.AsyncFunctionDef)
                     and node.name in {"sync_current_monitor", "dispatch_status", "poll", "hangup_station"}]
        service_resource = next(node for node in source.body if isinstance(node, ast.FunctionDef)
                                and node.name == "_service_resource")
        namespace = {"hass": self.hass, "entry": self.entry, "client": self.client, "station_registry": registry,
            "pcm_ws": self.manager, "provider": self.provider, "DOMAIN": "doorfast", "STATIONS_KEY": "doorfast_stations",
            "RING_STATUS": "ring_status", "HomeAssistantError": RuntimeError, "select_client": routing.select_client,
            "async_poll": polling.async_poll, "sync_monitor_state": MODULE.sync_monitor_state,
            "async_dispatcher_send": lambda *args: dispatched.append(args), "is_ringing": generation.is_ringing,
            "_LOGGER": logging.getLogger(__name__)}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[service_resource, *functions], type_ignores=[])),
                     str(COMPONENT / "__init__.py"), "exec"), namespace)
        connection = audio.Connection()
        opening = asyncio.create_task(self.manager.start(connection, {
            "id": 1, "config_entry_id": "entry-1", "station_id": "gate_main", "generation": 7}))
        poll = None
        try:
            await mic_entered.wait()
            pending = next(iter(self.manager._pending_cleanups.values()))
            self.assertIsNone(pending.producer.call_identity)
            self.http.current_status = audio.status(audio.REBOOT_RUNTIME)
            poll = asyncio.create_task(namespace["poll"]())
            await monitor_entered.wait()
            self.assertEqual(audio.REBOOT_RUNTIME, registry.snapshot.runtime_id)
            self.assertIsNot(old, registry.monitor("gate_main"))
            self.assertTrue(old._unloaded)
            self.assertFalse(poll.done())
            await namespace["hangup_station"](types.SimpleNamespace(data={
                "config_entry_id": "entry-1", "station_id": "gate_main"}))
            self.assertEqual([{"runtime_id": audio.REBOOT_RUNTIME, "station_id": "gate_main"}], hangups)
            mic_resume.set()
            await opening
            monitor_resume.set()
            await poll
            self.assertEqual([], connection.results)
            self.assertEqual(["not_found"], [error.code for error in connection.errors])
            self.assertEqual(audio.ws.AudioIdentity(audio.REBOOT_RUNTIME, "gate_main", 7), pending.revoked_identity)
            self.assertEqual({}, self.manager._captures)
            self.assertEqual({}, self.manager._starting)
            self.assertEqual({}, self.manager._pending_cleanups)
            self.assertEqual([("session", {"runtime": audio.REBOOT_RUNTIME, "generation": "7"}),
                              ("end", {"runtime": audio.REBOOT_RUNTIME, "generation": "7"})],
                [(url.rsplit("/", 1)[-1], params) for url, params in self.http.producer_requests])
            self.assertEqual("talking", self.client.status["call"]["session"])
            self.assertEqual("ending", registry.monitor("gate_main").call_state)
            self.assertEqual(1, self.provider.reconciled)
            self.assertEqual(2, len(dispatched))
        finally:
            mic_resume.set()
            monitor_resume.set()
            await opening
            if poll is not None:
                await poll
            await registry.async_close()

    async def test_actual_unload_closes_admission_before_blocked_producer_end(self):
        owner = await self.listen()
        lid = owner.results[-1]["listener_id"]
        await self.manager.start(owner, {"id": 2, "config_entry_id": "entry-1", "listener_id": lid})
        entered, resume = asyncio.Event(), asyncio.Event()
        original_request = self.http.request
        class EndResponse(self.audio.Response):
            async def __aenter__(self):
                entered.set()
                await resume.wait()
                return self
        def request(method, url, **kwargs):
            reply = original_request(method, url, **kwargs)
            return EndResponse(payload=reply.payload) if url.endswith("/session/end") else reply
        self.http.request = request
        task = asyncio.create_task(self.unload_entry(self.hass, self.entry))
        await entered.wait()
        try:
            await self.assert_closed()
            await self.manager.listen_read(owner, {"id": 3, "config_entry_id": "entry-1", "listener_id": lid})
            self.assertEqual("not_found", owner.errors[-1].code)
            self.assertFalse(self.manager._listeners)
            self.assertFalse(self.manager._captures)
        finally:
            resume.set()
            self.assertTrue(await task)
        await self.assert_closed()

    async def test_actual_false_unload_reopens_only_new_ownership(self):
        old = await self.listen()
        self.unload_result = False
        self.assertFalse(await self.unload_entry(self.hass, self.entry))
        new = await self.listen()
        self.assertEqual([], new.errors)
        self.assertNotEqual(old.results[-1]["listener_id"], new.results[-1]["listener_id"])
        self.assertEqual(1, len(self.manager._listeners))

    async def test_actual_unload_failure_or_cancellation_keeps_admission_closed(self):
        for phase in ("platform", "provider", "cancel"):
            with self.subTest(phase=phase):
                self.hass.data["doorfast"] = {"entry-1": self.client}
                self.manager.admit_entry("entry-1")
                entered, resume = asyncio.Event(), asyncio.Event()
                async def fail(*args):
                    if phase == "cancel":
                        entered.set()
                        await resume.wait()
                    raise RuntimeError("synthetic teardown failure")
                if phase == "provider":
                    self.hass.data["doorfast_webrtc_providers"] = {"entry-1": types.SimpleNamespace(async_close_entry=fail)}
                    async def success(*args): return True
                    self.hass.config_entries.async_unload_platforms = success
                else:
                    self.hass.config_entries.async_unload_platforms = fail
                task = asyncio.create_task(self.unload_entry(self.hass, self.entry))
                if phase == "cancel":
                    await entered.wait()
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError): await task
                else:
                    with self.assertRaises(RuntimeError): await task
                await self.assert_closed()

    async def test_actual_setup_admits_only_after_success_and_rollback_stays_closed(self):
        for outcome in ("failure", "cancel", "success"):
            with self.subTest(outcome=outcome):
                self.forward_entered.clear()
                self.forward_resume.clear()
                self.forward_error = RuntimeError("synthetic setup failure") if outcome == "failure" else None
                task = asyncio.create_task(self.setup_entry(self.hass, self.entry))
                await self.forward_entered.wait()
                try:
                    await self.assert_closed()
                finally:
                    if outcome == "cancel": task.cancel()
                    self.forward_resume.set()
                if outcome == "failure":
                    with self.assertRaises(RuntimeError): await task
                    await self.assert_closed()
                elif outcome == "cancel":
                    with self.assertRaises(asyncio.CancelledError): await task
                    await self.assert_closed()
                else:
                    self.assertTrue(await task)
                    self.assertEqual([], (await self.listen()).errors)

    async def test_ordinary_release_keeps_entry_available(self):
        first = await self.listen()
        await self.manager.release_entry("entry-1")
        second = await self.listen()
        self.assertEqual([], second.errors)
        self.assertNotEqual(first.results[-1]["listener_id"], second.results[-1]["listener_id"])


class SetupRollbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_status_sync_retains_answer_then_reports_terminal_publication(self):
        from tests.test_webrtc import (
            DoorfastWebRTCProvider, FakeRegistry as WebRTCRegistry,
            FakeHass as WebRTCHass, FakeSession, FakeWebSocket,
            FakeCamera, WebRTCError, source,
        )

        registry = WebRTCRegistry()
        registry.station_ids = tuple(registry.monitors)
        websocket = FakeWebSocket()
        provider = DoorfastWebRTCProvider(
            WebRTCHass(), "entry-1", registry, "http://127.0.0.1:1984",
            FakeSession(websocket),
        )
        messages = []
        await websocket.incoming.put({"type": "webrtc/answer", "value": "answer"})
        await provider.async_handle_async_webrtc_offer(
            FakeCamera(source("gate_main")), "offer", "main", messages.append
        )
        status = {"station_id": "gate_main", "generation": 9,
                  "state": "requesting", "ready": False, "encoder_running": True}

        async def monitor_status():
            return {"runtime_id": "runtime-a", "sessions": [status]}

        client = types.SimpleNamespace(
            status={"runtime_id": "runtime-a"}, monitor_status=monitor_status
        )
        await MODULE.sync_monitor_state(client, registry, provider, station_id="gate_main")
        self.assertFalse(websocket.closed)
        self.assertEqual(0, registry.monitors["gate_main"].released)
        status["encoder_running"] = False
        await MODULE.sync_monitor_state(client, registry, provider, station_id="gate_main")
        await provider.async_close_entry()
        self.assertTrue(websocket.closed)
        self.assertEqual(1, registry.monitors["gate_main"].released)
        self.assertEqual(["doorfast_publisher_ended"],
                         [m.value for m in messages if isinstance(m, WebRTCError)])

    async def test_poll_does_not_clear_monitor_start_in_flight(self):
        registry = FakeStationRegistry()
        provider = FakeProvider()
        client = types.SimpleNamespace(status={"runtime_id": "runtime-a"})

        async def monitor_status():
            registry.monitors["gate_main"].generation = 7
            registry.monitors["gate_main"].status_revision = 1
            return {"runtime_id": "runtime-a", "status_revision": 9, "sessions": []}

        client.monitor_status = monitor_status
        await MODULE.sync_monitor_state(client, registry, provider)

        self.assertEqual([], registry.monitors["gate_main"].applied)
        self.assertEqual(
            ["idle"],
            [item.get("state") for item in registry.monitors["gate_side"].applied],
        )

    async def test_poll_clears_generation_missing_before_and_after_request(self):
        registry = FakeStationRegistry()
        registry.monitors["gate_main"].generation = 7
        registry.monitors["gate_main"].status_revision = 3
        provider = FakeProvider()
        client = types.SimpleNamespace(status={"runtime_id": "runtime-a"})

        async def monitor_status():
            return {"runtime_id": "runtime-a", "status_revision": 9, "sessions": []}

        client.monitor_status = monitor_status
        await MODULE.sync_monitor_state(
            client, registry, provider, station_id="gate_main"
        )

        self.assertEqual(
            ["idle"],
            [item.get("state") for item in registry.monitors["gate_main"].applied],
        )

    async def test_polls_authoritative_sessions_by_station_only(self):
        registry = FakeStationRegistry()
        provider = FakeProvider()
        client = types.SimpleNamespace(
            status={"runtime_id": "runtime-a"},
            monitor_status=lambda: None,
        )

        async def monitor_status():
            return {
                "runtime_id": "runtime-a",
                "sessions": [
                    {"station_id": "gate_main", "generation": 7,
                     "state": "publishing", "status_revision": 4},
                    {"station_id": "gate_side", "generation": 7,
                     "state": "viewing", "status_revision": 5},
                    {"station_id": "unknown", "generation": 9,
                     "state": "publishing", "status_revision": 6},
                ],
            }

        client.monitor_status = monitor_status
        await MODULE.sync_monitor_state(client, registry, provider)

        self.assertEqual(
            [
                {"runtime_id": "runtime-a", "station_id": "gate_main",
                 "generation": 7, "state": "publishing", "status_revision": 4}
            ],
            registry.monitors["gate_main"].applied,
        )
        self.assertEqual(
            [
                {"runtime_id": "runtime-a", "station_id": "gate_side",
                 "generation": 7, "state": "viewing", "status_revision": 5}
            ],
            registry.monitors["gate_side"].applied,
        )
        self.assertEqual(1, provider.reconciled)

    async def test_session_uses_global_revision_when_it_is_newer(self):
        registry = FakeStationRegistry()
        provider = FakeProvider()
        client = types.SimpleNamespace(status={"runtime_id": "runtime-a"})

        async def monitor_status():
            return {
                "runtime_id": "runtime-a",
                "status_revision": 143,
                "sessions": [
                    {
                        "station_id": "gate_main",
                        "generation": 7,
                        "state": "publishing",
                        "status_revision": 110,
                    }
                ],
            }

        client.monitor_status = monitor_status
        await MODULE.sync_monitor_state(client, registry, provider)

        self.assertEqual(143, registry.monitors["gate_main"].applied[0]["status_revision"])

    async def test_later_poll_replaces_a_relay_accelerated_station_state(self):
        registry = FakeStationRegistry()
        provider = FakeProvider()
        client = types.SimpleNamespace(status={"runtime_id": "runtime-a"})
        replies = iter((
            {
                "runtime_id": "runtime-a",
                "sessions": [
                    {"station_id": "gate_main", "generation": 7,
                     "state": "publishing", "status_revision": 4},
                    {"station_id": "gate_side", "generation": 7,
                     "state": "viewing", "status_revision": 5},
                ],
            },
            {
                "runtime_id": "runtime-a",
                "sessions": [
                    {"station_id": "gate_main", "generation": 7,
                     "state": "idle", "status_revision": 5},
                    {"station_id": "gate_side", "generation": 7,
                     "state": "viewing", "status_revision": 5},
                ],
            },
        ))

        async def monitor_status():
            return next(replies)

        client.monitor_status = monitor_status
        await MODULE.sync_monitor_state(client, registry, provider)
        await MODULE.sync_monitor_state(client, registry, provider)

        self.assertEqual("idle", registry.monitors["gate_main"].applied[-1]["state"])
        self.assertEqual("viewing", registry.monitors["gate_side"].applied[-1]["state"])

    async def test_relay_sync_updates_only_the_matching_station(self):
        registry = FakeStationRegistry()
        provider = FakeProvider()
        client = types.SimpleNamespace(status={"runtime_id": "runtime-a"})

        async def monitor_status():
            return {
                "runtime_id": "runtime-a",
                "sessions": [
                    {"station_id": "gate_main", "generation": 7,
                     "state": "publishing", "status_revision": 4},
                    {"station_id": "gate_side", "generation": 7,
                     "state": "viewing", "status_revision": 5},
                ],
            }

        client.monitor_status = monitor_status
        relay_event = {
            "runtime_id": "runtime-a",
            "station_id": "gate_main",
            "event": "monitor_preempted",
            "generation": 7,
            "status_revision": 5,
        }
        await MODULE.sync_monitor_state(
            client,
            registry,
            provider,
            station_id="gate_main",
            relay_event=relay_event,
        )

        self.assertEqual(
            ["publishing", "monitor_preempted"],
            [item.get("state", item.get("event"))
             for item in registry.monitors["gate_main"].applied],
        )
        self.assertEqual([], registry.monitors["gate_side"].applied)

    async def test_syncs_polled_media_then_reconciles_provider(self):
        monitor = FakeStatusMonitor()
        provider = FakeProvider()
        client = types.SimpleNamespace(
            status={"media": {"state": "publishing", "generation": 9}}
        )

        await MODULE.sync_monitor_state(client, monitor, provider)

        self.assertEqual([client.status["media"]], monitor.applied)
        self.assertEqual(1, provider.reconciled)

    async def test_syncs_relay_event_after_authoritative_media(self):
        monitor = FakeStatusMonitor()
        provider = FakeProvider()
        client = types.SimpleNamespace(
            status={
                "media": {"state": "idle", "generation": 0},
                "media_event": {
                    "event": "monitor_preempted",
                    "generation": 9,
                },
            }
        )

        await MODULE.sync_monitor_state(
            client, monitor, provider, include_event=True
        )

        self.assertEqual(
            [client.status["media"], client.status["media_event"]],
            monitor.applied,
        )
        self.assertEqual(1, provider.reconciled)

    async def test_cleans_all_resources_when_first_entry_setup_fails(self):
        hass = FakeHass(("unlock", "call_elevator", "answer", "hangup"))
        hass.data["doorfast"] = {FakeEntry.entry_id: object()}
        hass.data["doorfast_event_views"] = {FakeEntry.entry_id: object()}
        hass.data["doorfast_monitors"] = {FakeEntry.entry_id: object()}
        hass.data["doorfast_webrtc_providers"] = {FakeEntry.entry_id: object()}
        hass.data["doorfast_webrtc_unsubscribers"] = {
            FakeEntry.entry_id: object()
        }
        hass.data["doorfast_stations"] = {FakeEntry.entry_id: object()}
        pcm = FakePcm()
        monitor = FakeMonitor()
        provider = FakeProvider()
        registry = FakeRegistry()
        provider_unregistered = []
        unregistered = []

        async def unregister_frontend(_hass, entry_id):
            unregistered.append(entry_id)

        await MODULE.rollback_entry_setup(
            hass,
            FakeEntry(),
            pcm_ws=pcm,
            unregister_frontend=unregister_frontend,
            platforms_forward_attempted=True,
            frontend_registered=True,
            view_created=True,
            service_names=("unlock", "call_elevator", "answer", "hangup"),
            monitor=monitor,
            provider=provider,
            station_registry=registry,
            unregister_webrtc=lambda: provider_unregistered.append(True),
        )

        self.assertEqual(pcm.released, [FakeEntry.entry_id])
        self.assertEqual(unregistered, [FakeEntry.entry_id])
        self.assertNotIn("doorfast", hass.data)
        self.assertNotIn("doorfast_event_views", hass.data)
        self.assertNotIn("doorfast_monitors", hass.data)
        self.assertNotIn("doorfast_webrtc_providers", hass.data)
        self.assertNotIn("doorfast_webrtc_unsubscribers", hass.data)
        self.assertNotIn("doorfast_stations", hass.data)
        self.assertEqual(1, monitor.closed)
        self.assertEqual(1, provider.closed)
        self.assertEqual(1, registry.closed)
        self.assertEqual([True], provider_unregistered)
        self.assertEqual(
            hass.services.removed,
            [("doorfast", name) for name in ("unlock", "call_elevator", "answer", "hangup")],
        )

    async def test_rollback_invalidates_real_audio_before_provider_wait(self):
        import asyncio
        from test_audio_websocket import Client, SubConnection, ws
        hass = FakeHass()
        client = Client()
        hass.data["doorfast"] = {FakeEntry.entry_id: client}
        hass.async_create_task = asyncio.create_task
        manager = ws.PcmWebSocketManager(hass)
        connection = SubConnection()
        await manager.listen_start(connection, {"id": 1, "config_entry_id": FakeEntry.entry_id,
                                               "station_id": "gate_main"})
        listener_id = connection.results[-1][1]["listener_id"]
        listener = manager._listeners[listener_id]
        await manager.start(connection, {"id": 2, "config_entry_id": FakeEntry.entry_id,
                                         "listener_id": listener_id})
        capture_id = connection.results[-1][1]["capture_id"]
        entered = asyncio.Event()
        resume = asyncio.Event()

        class BlockingProvider:
            async def async_close_entry(self):
                entered.set()
                await resume.wait()

        async def unregister(*args): pass
        task = asyncio.create_task(MODULE.rollback_entry_setup(
            hass, FakeEntry(), pcm_ws=manager, unregister_frontend=unregister,
            platforms_forward_attempted=False, frontend_registered=False,
            view_created=False, service_names=(), provider=BlockingProvider()))
        await entered.wait()
        try:
            self.assertNotIn(listener_id, manager._listeners)
            self.assertNotIn(capture_id, manager._captures)
            self.assertTrue(listener.hub.closed)
            self.assertTrue(listener.lease_timer.cancelled())
            self.assertEqual({}, connection.subscriptions)
            blocked = SubConnection()
            await manager.listen_start(blocked, {
                "id": 3, "config_entry_id": FakeEntry.entry_id, "station_id": "gate_main",
            })
            self.assertEqual("not_loaded", blocked.errors[-1][1])
        finally:
            resume.set()
            await task
            blocked = SubConnection()
            await manager.listen_start(
                blocked,
                {
                    "id": 3,
                    "config_entry_id": FakeEntry.entry_id,
                    "station_id": "gate_main",
                },
            )
            self.assertEqual("not_loaded", blocked.errors[-1][1])
            await manager.release_entry(FakeEntry.entry_id)

    async def test_preserves_resources_owned_by_another_entry(self):
        hass = FakeHass(("unlock", "call_elevator", "answer", "hangup"))
        hass.data["doorfast"] = {FakeEntry.entry_id: object(), "entry-2": object()}
        hass.data["doorfast_event_views"] = {FakeEntry.entry_id: object(), "entry-2": object()}
        hass.data["doorfast_stations"] = {FakeEntry.entry_id: object(), "entry-2": object()}
        pcm = FakePcm()
        registry = FakeRegistry()

        async def unregister_frontend(_hass, _entry_id):
            raise AssertionError("frontend was not registered for this failed entry")

        hass.config_entries.should_fail = True
        await MODULE.rollback_entry_setup(
            hass,
            FakeEntry(),
            pcm_ws=pcm,
            unregister_frontend=unregister_frontend,
            platforms_forward_attempted=True,
            frontend_registered=False,
            view_created=True,
            service_names=("unlock", "call_elevator", "answer", "hangup"),
            station_registry=registry,
        )

        self.assertEqual(pcm.released, [FakeEntry.entry_id])
        self.assertEqual(set(hass.data["doorfast"]), {"entry-2"})
        self.assertEqual(set(hass.data["doorfast_event_views"]), {"entry-2"})
        self.assertEqual(set(hass.data["doorfast_stations"]), {"entry-2"})
        self.assertEqual(1, registry.closed)
        self.assertEqual(hass.services.removed, [])


if __name__ == "__main__":
    unittest.main()
