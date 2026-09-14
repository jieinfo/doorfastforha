"""Tests for command payloads emitted by the Doorfast client."""

import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock


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


class FakeResponse:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def read(self):
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
        "call": {"generation": 7},
        "video": {"ready": True, "generation": 7, "frame_no": 12},
    }
    client.online = True
    client._video_generation = None
    client._video_etag = None
    client._video_frame = None
    return client


class AnswerPayloadTest(unittest.IsolatedAsyncioTestCase):
    async def test_uses_current_generation_and_verified_media_defaults(self):
        client = DoorfastClient.__new__(DoorfastClient)
        client.status = {"call": {"generation": 7}}
        client._request = AsyncMock(return_value={"queued": True})

        result = await client.answer()

        self.assertEqual({"queued": True}, result)
        client._request.assert_awaited_once_with(
            "POST",
            "/api/v1/answer",
            {
                "generation": 7,
                "primary_media_port": 8303,
                "secondary_media_port": 8302,
                "duration_seconds": 120,
            },
        )


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


if __name__ == "__main__":
    unittest.main()
