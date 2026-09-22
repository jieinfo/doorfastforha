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
webrtc_module = load_component("webrtc")
DoorfastWebRTCProvider = webrtc_module.DoorfastWebRTCProvider


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
        return self.websockets.pop(0)


class FakeCoordinator:
    def __init__(self, generation):
        self.generation = generation
        self.ready = True
        self.acquired = 0
        self.released = 0

    async def async_acquire_viewer(self):
        self.acquired += 1
        return self.generation

    async def async_wait_ready(self, generation, timeout=10):
        assert generation == self.generation
        assert timeout == 25.0

    async def async_release_viewer(self):
        self.released += 1


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

        with patch.object(webrtc_module, "_NEGOTIATION_ATTEMPTS", 6), patch.object(
            webrtc_module, "_NEGOTIATION_RETRY_DELAY", 0
        ):
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

    async def test_final_negotiation_failure_notifies_home_assistant(self):
        websockets = [FakeWebSocket() for _ in range(3)]
        provider, registry = self.make_provider(FakeSession(*websockets))
        messages = []
        task = asyncio.create_task(
            provider.async_handle_async_webrtc_offer(
                FakeCamera(source("gate_main")), "offer", "failed", messages.append
            )
        )

        with patch.object(webrtc_module, "_NEGOTIATION_ATTEMPTS", 3):
            for websocket in websockets:
                await asyncio.sleep(0)
                await websocket.incoming.put(
                    {"type": "error", "value": "not ready"}
                )

            with self.assertRaises(Exception):
                await asyncio.wait_for(task, 3)
        self.assertEqual(1, len(messages))
        self.assertIsInstance(messages[-1], WebRTCError)
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
