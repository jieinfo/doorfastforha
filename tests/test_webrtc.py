"""Tests for station-aware go2rtc WebRTC signaling."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


class WebRTCMessage:
    def __init__(self, value=None, *args, **kwargs):
        self.value = value
        self.args = args
        self.__dict__.update(kwargs)


class WebRTCAnswer(WebRTCMessage):
    pass


class WebRTCCandidate(WebRTCMessage):
    pass


class WebRTCError(WebRTCMessage):
    pass


class CameraWebRTCProvider:
    pass


camera_module = types.ModuleType("homeassistant.components.camera")
camera_module.Camera = object
camera_module.CameraWebRTCProvider = CameraWebRTCProvider
camera_module.WebRTCAnswer = WebRTCAnswer
camera_module.WebRTCCandidate = WebRTCCandidate
camera_module.WebRTCError = WebRTCError
homeassistant = types.ModuleType("homeassistant")
components = types.ModuleType("homeassistant.components")
components.camera = camera_module
homeassistant.components = components
sys.modules.setdefault("homeassistant", homeassistant)
sys.modules.setdefault("homeassistant.components", components)
sys.modules["homeassistant.components.camera"] = camera_module

webrtc_models = types.ModuleType("webrtc_models")


class RTCIceCandidateInit:
    def __init__(self, candidate):
        self.candidate = candidate


webrtc_models.RTCIceCandidateInit = RTCIceCandidateInit
sys.modules["webrtc_models"] = webrtc_models

ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / "doorfast"
package = types.ModuleType("custom_components.doorfast")
package.__path__ = [str(COMPONENT)]
sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
sys.modules["custom_components.doorfast"] = package


def load_component(name):
    spec = importlib.util.spec_from_file_location(
        f"custom_components.doorfast.{name}", COMPONENT / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


load_component("config_helpers")
load_component("const")
monitor_module = load_component("monitor")
MonitorLease = monitor_module.MonitorLease
webrtc_module = load_component("webrtc")
DoorfastWebRTCProvider = webrtc_module.DoorfastWebRTCProvider
HomeAssistantError = webrtc_module.HomeAssistantError


class FakeCamera:
    def __init__(self, source):
        self.source = source

    async def stream_source(self):
        return self.source


class FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.incoming = asyncio.Queue()
        self.closed = False

    async def send_json(self, message):
        self.sent.append(message)

    async def receive_json(self):
        return await self.incoming.get()

    async def close(self):
        self.closed = True
        await self.incoming.put(None)


class FakeSession:
    def __init__(self, *websockets):
        self.websockets = list(websockets)
        self.urls = []

    async def ws_connect(self, url, **kwargs):
        self.urls.append(url)
        self.kwargs = kwargs
        return self.websockets.pop(0)


class BlockingSession:
    async def ws_connect(self, url, **kwargs):
        await asyncio.Event().wait()


class FakeCoordinator:
    def __init__(self, generation, leases=None):
        self.generation = generation
        self.ready = True
        self.acquired = 0
        self.leases = list(leases or [MonitorLease(generation, 1, 0)])
        self.released_leases = []
        self._active_leases = set()

    @property
    def released(self):
        return len(self.released_leases)

    @property
    def lease(self):
        return self.leases[min(self.acquired - 1, len(self.leases) - 1)]

    async def async_acquire_viewer(self):
        self.acquired += 1
        lease = self.leases[min(self.acquired - 1, len(self.leases) - 1)]
        self._active_leases.add(lease)
        return lease

    async def async_wait_ready(self, generation, timeout=10):
        assert generation == self.generation
        assert timeout is None

    async def async_release_viewer(self, lease):
        self.released_leases.append(lease)
        self._active_leases.discard(lease)

    def lease_active(self, lease):
        return lease in self._active_leases

    def invalidate_leases(self):
        self._active_leases.clear()


class FakeRegistry:
    def __init__(self):
        self.stations = {
            "gate_main": types.SimpleNamespace(stream_name="doorfast_gate_main"),
            "gate_side": types.SimpleNamespace(stream_name="doorfast_gate_side"),
        }
        self.monitors = {
            "gate_main": FakeCoordinator(9),
            "gate_side": FakeCoordinator(12),
        }

    def station(self, station_id):
        return self.stations[station_id]

    def monitor(self, station_id):
        return self.monitors[station_id]


class FakeHass:
    def __init__(self):
        self.tasks = []

    def async_create_task(self, awaitable):
        task = asyncio.create_task(awaitable)
        self.tasks.append(task)
        return task


def source(station_id, entry_id="entry-1"):
    return f"doorfast://{entry_id}/station/{station_id}/preview"


class ProviderTest(unittest.IsolatedAsyncioTestCase):
    def make_provider(self, session=None, base="http://127.0.0.1:1984"):
        registry = FakeRegistry()
        provider = DoorfastWebRTCProvider(
            FakeHass(), "entry-1", registry, base, session or FakeSession()
        )
        return provider, registry

    async def test_go2rtc_credentials_are_sent_to_websocket(self):
        websocket = FakeWebSocket()
        session = FakeSession(websocket)
        provider = DoorfastWebRTCProvider(
            FakeHass(),
            "entry-1",
            FakeRegistry(),
            "http://127.0.0.1:1984",
            session,
            go2rtc_username="doorfast",
            go2rtc_password="secret",
        )

        await self.open_offer(provider, websocket, "gate_main", "auth")

        self.assertEqual("doorfast", session.kwargs["auth"].login)
        self.assertEqual("secret", session.kwargs["auth"].password)
        await provider.async_close_entry()

    async def test_ws_connect_cancellation_releases_viewer_lease(self):
        provider, registry = self.make_provider(BlockingSession())
        task = asyncio.create_task(
            provider.async_handle_async_webrtc_offer(
                FakeCamera(source("gate_main")), "offer", "connect-cancel", lambda _: None
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(1, registry.monitors["gate_main"].released)

    async def open_offer(self, provider, websocket, station_id, session_id):
        messages = []
        task = asyncio.create_task(
            provider.async_handle_async_webrtc_offer(
                FakeCamera(source(station_id)), "offer", session_id, messages.append
            )
        )
        await asyncio.sleep(0)
        await websocket.incoming.put({"type": "webrtc/answer", "value": "answer"})
        await asyncio.wait_for(task, 1)
        return messages

    async def test_url_selection_and_strict_station_source(self):
        provider, _registry = self.make_provider(
            base="https://go2rtc.example/base"
        )

        self.assertEqual(
            "wss://go2rtc.example/base/api/ws?src=doorfast_gate_main",
            provider.websocket_url("doorfast_gate_main"),
        )
        self.assertEqual(
            "wss://go2rtc.example/base/api/ws?src=doorfast%20gate%2Fmain",
            provider.websocket_url("doorfast gate/main"),
        )
        self.assertTrue(provider.async_is_supported(source("gate_main")))
        self.assertFalse(provider.async_is_supported(source("gate_main", "entry-2")))
        self.assertFalse(provider.async_is_supported(source("unknown")))
        self.assertFalse(provider.async_is_supported("rtsp://secret@example"))

    async def test_two_station_offers_use_distinct_streams_and_coordinators(self):
        main_ws = FakeWebSocket()
        side_ws = FakeWebSocket()
        session = FakeSession(main_ws, side_ws)
        provider, registry = self.make_provider(session)

        main_messages = await self.open_offer(provider, main_ws, "gate_main", "main")
        side_messages = await self.open_offer(provider, side_ws, "gate_side", "side")

        self.assertEqual(
            [
                "ws://127.0.0.1:1984/api/ws?src=doorfast_gate_main",
                "ws://127.0.0.1:1984/api/ws?src=doorfast_gate_side",
            ],
            session.urls,
        )
        self.assertEqual({}, session.kwargs)
        self.assertIsInstance(main_messages[0], WebRTCAnswer)
        self.assertIsInstance(side_messages[0], WebRTCAnswer)
        self.assertEqual(1, registry.monitors["gate_main"].acquired)
        self.assertEqual(1, registry.monitors["gate_side"].acquired)
        await provider.async_close_entry()

    async def test_one_station_error_leaves_other_session_open(self):
        main_ws = FakeWebSocket()
        side_ws = FakeWebSocket()
        provider, registry = self.make_provider(FakeSession(main_ws, side_ws))
        await self.open_offer(provider, main_ws, "gate_main", "main")
        await self.open_offer(provider, side_ws, "gate_side", "side")

        await main_ws.incoming.put({"type": "error", "value": "failed"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertTrue(main_ws.closed)
        self.assertFalse(side_ws.closed)
        self.assertNotIn("main", provider._sessions)
        self.assertIn("side", provider._sessions)
        self.assertEqual(1, registry.monitors["gate_main"].released)
        self.assertEqual(0, registry.monitors["gate_side"].released)
        await provider.async_close_entry()

    async def test_retries_when_go2rtc_producer_is_not_ready(self):
        first_ws = FakeWebSocket()
        second_ws = FakeWebSocket()
        session = FakeSession(first_ws, second_ws)
        provider, registry = self.make_provider(session)
        messages = []

        task = asyncio.create_task(
            provider.async_handle_async_webrtc_offer(
                FakeCamera(source("gate_main")), "offer", "retry", messages.append
            )
        )
        await asyncio.sleep(0)
        await first_ws.incoming.put(
            {"type": "error", "value": "streams: unknown error"}
        )
        await asyncio.sleep(0)
        await second_ws.incoming.put({"type": "webrtc/answer", "value": "answer"})
        await asyncio.wait_for(task, 2)

        self.assertEqual(
            [
                "ws://127.0.0.1:1984/api/ws?src=doorfast_gate_main",
                "ws://127.0.0.1:1984/api/ws?src=doorfast_gate_main",
            ],
            session.urls,
        )
        self.assertTrue(first_ws.closed)
        self.assertEqual(1, len(messages))
        self.assertIsInstance(messages[-1], WebRTCAnswer)
        self.assertEqual(1, registry.monitors["gate_main"].acquired)
        self.assertEqual(0, registry.monitors["gate_main"].released)

        await provider.async_close_entry()
        self.assertEqual(1, registry.monitors["gate_main"].released)

    async def test_invalid_go2rtc_message_fails_without_retrying(self):
        websocket = FakeWebSocket()
        provider, registry = self.make_provider(FakeSession(websocket))
        task = asyncio.create_task(
            provider.async_handle_async_webrtc_offer(
                FakeCamera(source("gate_main")), "offer", "invalid", lambda _: None
            )
        )
        await asyncio.sleep(0)
        await websocket.incoming.put({"type": "unexpected", "value": "bad"})
        with self.assertRaises(Exception) as context:
            await asyncio.wait_for(task, 1)
        self.assertIn("unsupported go2rtc WebSocket message", str(context.exception))
        self.assertEqual(1, len(provider._session.urls))
        self.assertEqual(1, registry.monitors["gate_main"].released)

    async def test_retries_through_slow_producer_startup(self):
        websockets = [FakeWebSocket() for _ in range(6)]
        session = FakeSession(*websockets)
        provider, registry = self.make_provider(session)
        messages = []
        task = asyncio.create_task(
            provider.async_handle_async_webrtc_offer(
                FakeCamera(source("gate_main")), "offer", "slow", messages.append
            )
        )

        with patch.object(webrtc_module, "_NEGOTIATION_RETRY_DELAY", 0):
            for websocket in websockets[:-1]:
                await asyncio.sleep(0)
                await websocket.incoming.put(
                    {"type": "error", "value": "streams: unknown error"}
                )
                await asyncio.sleep(0)
            await asyncio.sleep(0)
            await websockets[-1].incoming.put(
                {"type": "webrtc/answer", "value": "answer"}
            )
            await asyncio.wait_for(task, 1)

        self.assertEqual(6, len(session.urls))
        self.assertEqual(1, len(messages))
        self.assertIsInstance(messages[0], WebRTCAnswer)
        self.assertEqual(1, registry.monitors["gate_main"].acquired)
        self.assertEqual(0, registry.monitors["gate_main"].released)
        await provider.async_close_entry()

    async def test_retries_beyond_previous_attempt_limit_until_producer_recovers(self):
        websockets = [FakeWebSocket() for _ in range(13)]
        provider, registry = self.make_provider(FakeSession(*websockets))
        messages = []
        task = asyncio.create_task(
            provider.async_handle_async_webrtc_offer(
                FakeCamera(source("gate_main")), "offer", "long-retry", messages.append
            )
        )

        with patch.object(webrtc_module, "_NEGOTIATION_RETRY_DELAY", 0):
            for websocket in websockets[:-1]:
                await asyncio.sleep(0)
                await websocket.incoming.put(
                    {"type": "error", "value": "not ready"}
                )
                await asyncio.sleep(0)
            await asyncio.sleep(0)
            await websockets[-1].incoming.put(
                {"type": "webrtc/answer", "value": "answer"}
            )
            await asyncio.wait_for(task, 1)

        self.assertEqual(1, len(messages))
        self.assertIsInstance(messages[-1], WebRTCAnswer)
        self.assertEqual(0, registry.monitors["gate_main"].released)
        await provider.async_close_entry()
        self.assertEqual(1, registry.monitors["gate_main"].released)

    async def test_retrying_offer_stays_alive_until_home_assistant_closes_session(self):
        first_ws = FakeWebSocket()
        second_ws = FakeWebSocket()
        provider, registry = self.make_provider(FakeSession(first_ws, second_ws))
        task = asyncio.create_task(
            provider.async_handle_async_webrtc_offer(
                FakeCamera(source("gate_main")), "offer", "retry-forever", lambda _: None
            )
        )

        with patch.object(webrtc_module, "_NEGOTIATION_RETRY_DELAY", 0):
            await asyncio.sleep(0)
            await first_ws.incoming.put({"type": "error", "value": "not ready"})
            await asyncio.sleep(0)
            await second_ws.incoming.put({"type": "error", "value": "not ready"})
            await asyncio.sleep(0)
            provider.async_close_session("retry-forever")

            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)

        self.assertEqual(1, registry.monitors["gate_main"].released)
        await provider.async_close_entry()

    async def test_reused_session_id_cancels_previous_retry(self):
        first_ws = FakeWebSocket()
        replacement_ws = FakeWebSocket()
        provider, registry = self.make_provider(FakeSession(first_ws, replacement_ws))
        first = asyncio.create_task(
            provider.async_handle_async_webrtc_offer(
                FakeCamera(source("gate_main")), "offer", "reused", lambda _: None
            )
        )
        await asyncio.sleep(0)
        await first_ws.incoming.put({"type": "error", "value": "not ready"})
        await asyncio.sleep(0)

        second = asyncio.create_task(
            provider.async_handle_async_webrtc_offer(
                FakeCamera(source("gate_main")), "offer", "reused", lambda _: None
            )
        )
        for _ in range(20):
            if len(provider._session.urls) == 2:
                break
            await asyncio.sleep(0)
        self.assertEqual(2, len(provider._session.urls))
        await replacement_ws.incoming.put(
            {"type": "webrtc/answer", "value": "replacement"}
        )
        await asyncio.wait_for(second, 1)
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(first, 1)
        self.assertEqual(2, registry.monitors["gate_main"].acquired)
        self.assertEqual(1, registry.monitors["gate_main"].released)
        await provider.async_close_entry()
        self.assertEqual(2, registry.monitors["gate_main"].released)

    async def test_late_cleanup_for_reused_session_releases_only_old_lease(self):
        old_ws = FakeWebSocket()
        new_ws = FakeWebSocket()
        coordinator = FakeCoordinator(
            9,
            [MonitorLease(9, 1, 0), MonitorLease(10, 2, 0)],
        )
        registry = FakeRegistry()
        registry.monitors["gate_main"] = coordinator
        provider = DoorfastWebRTCProvider(
            FakeHass(), "entry-1", registry, "http://127.0.0.1:1984",
            FakeSession(old_ws, new_ws),
        )

        await self.open_offer(provider, old_ws, "gate_main", "reused")
        old_state = provider._sessions["reused"]
        deferred_cleanup = []
        provider._hass.async_create_task = deferred_cleanup.append
        provider.async_close_session("reused")

        coordinator.generation = 10
        await self.open_offer(provider, new_ws, "gate_main", "reused")
        await deferred_cleanup.pop()

        self.assertEqual([MonitorLease(9, 1, 0)], coordinator.released_leases)
        self.assertEqual(MonitorLease(10, 2, 0), provider._sessions["reused"].lease)
        await provider.async_close_entry()

    async def test_close_during_initial_offer_wait_releases_lease(self):
        websocket = FakeWebSocket()
        provider, registry = self.make_provider(FakeSession(websocket))
        task = asyncio.create_task(
            provider.async_handle_async_webrtc_offer(
                FakeCamera(source("gate_main")), "offer", "initial-close", lambda _: None
            )
        )
        await asyncio.sleep(0)
        provider.async_close_session("initial-close")

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        await asyncio.gather(*provider._hass.tasks)
        self.assertEqual(1, registry.monitors["gate_main"].released)

    async def test_parallel_cleanup_and_offer_cancel_release_lease_once(self):
        class GatedReleaseCoordinator(FakeCoordinator):
            def __init__(self):
                super().__init__(9)
                self.release_started = asyncio.Event()
                self.allow_release = asyncio.Event()

            async def async_release_viewer(self, lease):
                self.released_leases.append(lease)
                if len(self.released_leases) == 1:
                    self.release_started.set()
                    await self.allow_release.wait()
                self._active_leases.discard(lease)

        websocket = FakeWebSocket()
        coordinator = GatedReleaseCoordinator()
        registry = FakeRegistry()
        registry.monitors["gate_main"] = coordinator
        provider = DoorfastWebRTCProvider(
            FakeHass(), "entry-1", registry, "http://127.0.0.1:1984",
            FakeSession(websocket),
        )
        offer_task = asyncio.create_task(
            provider.async_handle_async_webrtc_offer(
                FakeCamera(source("gate_main")), "offer", "parallel", lambda _: None
            )
        )
        await asyncio.sleep(0)
        state = provider._sessions["parallel"]
        cleanup_task = asyncio.create_task(
            provider._cleanup_session("parallel", state)
        )
        await coordinator.release_started.wait()

        offer_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await offer_task
        coordinator.allow_release.set()
        await cleanup_task

        self.assertEqual([state.lease], coordinator.released_leases)

    async def test_parallel_cleanup_and_answer_error_release_lease_once(self):
        class GatedCloseWebSocket(FakeWebSocket):
            def __init__(self):
                super().__init__()
                self.close_started = asyncio.Event()
                self.allow_close = asyncio.Event()

            async def close(self):
                self.closed = True
                self.close_started.set()
                await self.allow_close.wait()
                await self.incoming.put(None)

        class GatedReleaseCoordinator(FakeCoordinator):
            def __init__(self):
                super().__init__(9)
                self.release_started = asyncio.Event()
                self.allow_release = asyncio.Event()

            async def async_release_viewer(self, lease):
                self.released_leases.append(lease)
                if len(self.released_leases) == 1:
                    self.release_started.set()
                    await self.allow_release.wait()
                self._active_leases.discard(lease)

        websocket = GatedCloseWebSocket()
        coordinator = GatedReleaseCoordinator()
        registry = FakeRegistry()
        registry.monitors["gate_main"] = coordinator
        provider = DoorfastWebRTCProvider(
            FakeHass(), "entry-1", registry, "http://127.0.0.1:1984",
            FakeSession(websocket),
        )
        offer_task = asyncio.create_task(
            provider.async_handle_async_webrtc_offer(
                FakeCamera(source("gate_main")), "offer", "answer-error", lambda _: None
            )
        )
        await asyncio.sleep(0)
        state = provider._sessions["answer-error"]
        cleanup_task = asyncio.create_task(
            provider._cleanup_session("answer-error", state)
        )
        await websocket.close_started.wait()
        state.answer.set_exception(HomeAssistantError("answer failed"))
        websocket.allow_close.set()
        await coordinator.release_started.wait()

        with self.assertRaises(HomeAssistantError):
            await offer_task
        coordinator.allow_release.set()
        await cleanup_task

        self.assertEqual([state.lease], coordinator.released_leases)

    async def test_parallel_cleanup_and_retry_invalidation_release_lease_once(self):
        class GatedCloseWebSocket(FakeWebSocket):
            def __init__(self):
                super().__init__()
                self.close_started = asyncio.Event()
                self.allow_close = asyncio.Event()

            async def close(self):
                self.closed = True
                self.close_started.set()
                await self.allow_close.wait()
                await self.incoming.put(None)

        class GatedReleaseCoordinator(FakeCoordinator):
            def __init__(self):
                super().__init__(9)
                self.release_started = asyncio.Event()
                self.allow_release = asyncio.Event()

            async def async_release_viewer(self, lease):
                self.released_leases.append(lease)
                if len(self.released_leases) == 1:
                    self.release_started.set()
                    await self.allow_release.wait()
                self._active_leases.discard(lease)

        first_ws = GatedCloseWebSocket()
        coordinator = GatedReleaseCoordinator()
        registry = FakeRegistry()
        registry.monitors["gate_main"] = coordinator
        provider = DoorfastWebRTCProvider(
            FakeHass(), "entry-1", registry, "http://127.0.0.1:1984",
            FakeSession(first_ws),
        )
        offer_task = asyncio.create_task(
            provider.async_handle_async_webrtc_offer(
                FakeCamera(source("gate_main")), "offer", "retry-error", lambda _: None
            )
        )
        await asyncio.sleep(0)
        state = provider._sessions["retry-error"]
        cleanup_task = asyncio.create_task(
            provider._cleanup_session("retry-error", state)
        )
        await first_ws.close_started.wait()
        state.answer.set_exception(webrtc_module._ProducerNotReadyError("not ready"))
        first_ws.allow_close.set()
        await coordinator.release_started.wait()
        coordinator.allow_release.set()
        await cleanup_task

        with patch.object(webrtc_module, "_NEGOTIATION_RETRY_DELAY", 0):
            with self.assertRaises(RuntimeError):
                await offer_task

        self.assertEqual([state.lease], coordinator.released_leases)

    async def test_release_error_after_cleanup_claim_does_not_retry_release(self):
        class FailingReleaseCoordinator(FakeCoordinator):
            async def async_release_viewer(self, lease):
                self.released_leases.append(lease)
                raise RuntimeError("release failed")

        websocket = FakeWebSocket()
        coordinator = FailingReleaseCoordinator(9)
        registry = FakeRegistry()
        registry.monitors["gate_main"] = coordinator
        provider = DoorfastWebRTCProvider(
            FakeHass(), "entry-1", registry, "http://127.0.0.1:1984",
            FakeSession(websocket),
        )
        offer_task = asyncio.create_task(
            provider.async_handle_async_webrtc_offer(
                FakeCamera(source("gate_main")), "offer", "release-error", lambda _: None
            )
        )
        await asyncio.sleep(0)
        state = provider._sessions["release-error"]
        cleanup_task = asyncio.create_task(
            provider._cleanup_session("release-error", state)
        )
        with self.assertRaises(RuntimeError):
            await cleanup_task

        offer_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await offer_task

        self.assertEqual([state.lease], coordinator.released_leases)

    async def test_release_error_keeps_session_for_later_cleanup_retry(self):
        class FailOnceCoordinator(FakeCoordinator):
            def __init__(self):
                super().__init__(9)
                self.fail_release = True

            async def async_release_viewer(self, lease):
                self.released_leases.append(lease)
                if self.fail_release:
                    self.fail_release = False
                    raise RuntimeError("release failed")
                self._active_leases.discard(lease)

        websocket = FakeWebSocket()
        coordinator = FailOnceCoordinator()
        registry = FakeRegistry()
        registry.monitors["gate_main"] = coordinator
        provider = DoorfastWebRTCProvider(
            FakeHass(), "entry-1", registry, "http://127.0.0.1:1984",
            FakeSession(websocket),
        )
        await self.open_offer(provider, websocket, "gate_main", "retry-cleanup")
        state = provider._sessions["retry-cleanup"]

        with self.assertRaises(RuntimeError):
            await provider._cleanup_session("retry-cleanup", state)
        self.assertIn("retry-cleanup", provider._sessions)
        self.assertFalse(state.lease_released)

        await provider._cleanup_session("retry-cleanup", state)
        self.assertNotIn("retry-cleanup", provider._sessions)
        self.assertEqual([state.lease, state.lease], coordinator.released_leases)
        self.assertEqual(0, len(coordinator._active_leases))

    async def test_retry_stops_when_monitor_lease_becomes_inactive(self):
        first_ws = FakeWebSocket()
        second_ws = FakeWebSocket()
        provider, registry = self.make_provider(FakeSession(first_ws, second_ws))
        task = asyncio.create_task(
            provider.async_handle_async_webrtc_offer(
                FakeCamera(source("gate_main")), "offer", "expired", lambda _: None
            )
        )

        with patch.object(webrtc_module, "_NEGOTIATION_RETRY_DELAY", 0):
            await asyncio.sleep(0)
            await first_ws.incoming.put({"type": "error", "value": "not ready"})
            await asyncio.sleep(0)
            registry.monitors["gate_main"].invalidate_leases()
            with self.assertRaises(RuntimeError):
                await asyncio.wait_for(task, 1)

        self.assertEqual(1, len(provider._session.urls))
        self.assertEqual(1, registry.monitors["gate_main"].released)

    async def test_close_session_cancels_retrying_offer(self):
        first_ws = FakeWebSocket()
        second_ws = FakeWebSocket()
        provider, registry = self.make_provider(FakeSession(first_ws, second_ws))
        task = asyncio.create_task(
            provider.async_handle_async_webrtc_offer(
                FakeCamera(source("gate_main")), "offer", "closing", lambda _: None
            )
        )
        await asyncio.sleep(0)
        await first_ws.incoming.put({"type": "error", "value": "not ready"})
        await asyncio.sleep(0)
        provider.async_close_session("closing")

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        self.assertEqual(1, len(provider._session.urls))
        self.assertEqual(1, registry.monitors["gate_main"].released)

    async def test_reconcile_closes_only_stale_station_generation(self):
        main_ws = FakeWebSocket()
        side_ws = FakeWebSocket()
        provider, registry = self.make_provider(FakeSession(main_ws, side_ws))
        await self.open_offer(provider, main_ws, "gate_main", "main")
        await self.open_offer(provider, side_ws, "gate_side", "side")

        registry.monitors["gate_main"].generation = 10
        await provider.async_reconcile_monitor()

        self.assertTrue(main_ws.closed)
        self.assertFalse(side_ws.closed)
        self.assertNotIn("main", provider._sessions)
        self.assertIn("side", provider._sessions)
        await provider.async_close_entry()


if __name__ == "__main__":
    unittest.main()
