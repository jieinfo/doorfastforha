"""Tests for the HA-local go2rtc WebRTC signaling provider."""

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path


class WebRTCMessage:
    def __init__(self, value=None, **kwargs):
        self.value = value
        self.__dict__.update(kwargs)


class WebRTCAnswer(WebRTCMessage):
    pass


class WebRTCCandidate(WebRTCMessage):
    pass


class WebRTCError(WebRTCMessage):
    pass


class CameraWebRTCProvider:
    pass


class FakeCamera:
    def __init__(self, source="doorfast://entry-1/preview"):
        self.source = source

    async def stream_source(self):
        return self.source


camera_module = types.ModuleType("homeassistant.components.camera")
camera_module.Camera = FakeCamera
camera_module.CameraWebRTCProvider = CameraWebRTCProvider
camera_module.WebRTCAnswer = WebRTCAnswer
camera_module.WebRTCCandidate = WebRTCCandidate
camera_module.WebRTCError = WebRTCError
camera_module.WebRTCMessage = WebRTCMessage
camera_module.WebRTCSendMessage = object
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
const_spec = importlib.util.spec_from_file_location(
    "custom_components.doorfast.const", COMPONENT / "const.py"
)
const_module = importlib.util.module_from_spec(const_spec)
sys.modules[const_spec.name] = const_module
assert const_spec.loader is not None
const_spec.loader.exec_module(const_module)
spec = importlib.util.spec_from_file_location(
    "custom_components.doorfast.webrtc", COMPONENT / "webrtc.py"
)
webrtc_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = webrtc_module
assert spec.loader is not None
spec.loader.exec_module(webrtc_module)
DoorfastWebRTCProvider = webrtc_module.DoorfastWebRTCProvider


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
    def __init__(self, websocket):
        self.websocket = websocket
        self.url = None

    async def ws_connect(self, url, **kwargs):
        self.url = url
        return self.websocket


class FailingSession(FakeSession):
    async def ws_connect(self, url, **kwargs):
        self.url = url
        raise OSError("go2rtc unavailable")


class FakeCoordinator:
    def __init__(self):
        self.generation = 9
        self.state = "publishing"
        self.ready = True
        self.acquired = []
        self.released = 0

    async def async_acquire_viewer(self):
        self.acquired.append(self.generation)
        return self.generation

    async def async_wait_ready(self, generation, timeout=10):
        assert generation == self.generation
        assert timeout == 10

    async def async_release_viewer(self):
        self.released += 1


class FakeHass:
    def __init__(self):
        self.tasks = []

    def async_create_task(self, awaitable):
        task = asyncio.create_task(awaitable)
        self.tasks.append(task)
        return task


class ProviderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ws = FakeWebSocket()
        self.session = FakeSession(self.ws)
        self.hass = FakeHass()
        self.coordinator = FakeCoordinator()
        self.provider = DoorfastWebRTCProvider(
            self.hass, "entry-1", self.coordinator, self.session
        )

    async def test_offer_is_proxied_to_local_go2rtc_and_answer_forwarded(self):
        messages = []
        offer = "v=0\\r\\no=- offer"
        task = asyncio.create_task(
            self.provider.async_handle_async_webrtc_offer(
                FakeCamera(), offer, "session-1", messages.append
            )
        )
        await asyncio.sleep(0)
        self.assertEqual(
            "ws://127.0.0.1:1984/api/ws?src=doorfast_preview",
            self.session.url,
        )
        self.assertEqual(
            [{"type": "webrtc/offer", "value": offer}], self.ws.sent
        )
        await self.ws.incoming.put(
            {"type": "webrtc/answer", "value": "v=0\\r\\na=answer"}
        )
        await asyncio.wait_for(task, 1)
        self.assertIsInstance(messages[0], WebRTCAnswer)
        self.assertEqual("v=0\\r\\na=answer", messages[0].value)
        self.assertEqual([9], self.coordinator.acquired)

    async def test_candidate_forwarding_and_session_close_release_viewer(self):
        messages = []
        task = asyncio.create_task(
            self.provider.async_handle_async_webrtc_offer(
                FakeCamera(), "offer", "session-1", messages.append
            )
        )
        await asyncio.sleep(0)
        await self.ws.incoming.put(
            {"type": "webrtc/answer", "value": "answer"}
        )
        await asyncio.wait_for(task, 1)
        await self.provider.async_on_webrtc_candidate(
            "session-1", RTCIceCandidateInit("candidate:1")
        )
        self.assertEqual(
            [
                {"type": "webrtc/offer", "value": "offer"},
                {"type": "webrtc/candidate", "value": "candidate:1"},
            ],
            self.ws.sent,
        )
        self.provider.async_close_session("session-1")
        await asyncio.gather(*self.hass.tasks)
        self.assertTrue(self.ws.closed)
        self.assertEqual(1, self.coordinator.released)

    async def test_provider_accepts_only_opaque_doorfast_source(self):
        self.assertTrue(self.provider.async_is_supported("doorfast://entry-1/preview"))
        self.assertFalse(self.provider.async_is_supported("doorfast://entry-2/preview"))
        self.assertFalse(self.provider.async_is_supported("rtsp://secret@example"))

    async def test_failed_socket_connect_releases_acquired_viewer(self):
        provider = DoorfastWebRTCProvider(
            self.hass, "entry-1", self.coordinator, FailingSession(self.ws)
        )
        with self.assertRaises(RuntimeError):
            await provider.async_handle_async_webrtc_offer(
                FakeCamera(), "offer", "session-1", lambda _message: None
            )
        self.assertEqual(1, self.coordinator.released)

    async def test_go2rtc_error_releases_viewer_once(self):
        task = asyncio.create_task(
            self.provider.async_handle_async_webrtc_offer(
                FakeCamera(), "offer", "session-1", lambda _message: None
            )
        )
        await asyncio.sleep(0)
        await self.ws.incoming.put(
            {"type": "error", "value": "source unavailable"}
        )
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(task, 1)
        self.assertEqual(1, self.coordinator.released)

    async def test_generation_change_closes_stale_session(self):
        task = asyncio.create_task(
            self.provider.async_handle_async_webrtc_offer(
                FakeCamera(), "offer", "session-1", lambda _message: None
            )
        )
        await asyncio.sleep(0)
        await self.ws.incoming.put(
            {"type": "webrtc/answer", "value": "answer"}
        )
        await asyncio.wait_for(task, 1)

        self.coordinator.generation = 10
        await self.provider.async_reconcile_monitor()

        self.assertTrue(self.ws.closed)
        self.assertEqual(1, self.coordinator.released)


if __name__ == "__main__":
    unittest.main()
