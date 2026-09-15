"""Tests for exact binary PCM HTTP primitives on DoorfastClient."""

import importlib.util
from pathlib import Path
import sys
import types
import unittest

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
helpers = sys.modules.setdefault("homeassistant.helpers", types.ModuleType("homeassistant.helpers"))
aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
aiohttp_client.async_get_clientsession = lambda hass: None
helpers.aiohttp_client = aiohttp_client
sys.modules["homeassistant.helpers.aiohttp_client"] = aiohttp_client
package = types.ModuleType("custom_components.doorfast")
package.__path__ = [str(COMPONENT)]
sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
sys.modules["custom_components.doorfast"] = package
load_module("custom_components.doorfast.const", COMPONENT / "const.py")
load_module("custom_components.doorfast.generation", COMPONENT / "generation.py")
client_types = load_module("custom_components.doorfast.client_types", COMPONENT / "client_types.py")
client_module = load_module("custom_components.doorfast.client", COMPONENT / "client.py")
DoorfastClient = client_module.DoorfastClient


class Response:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, content_type=None):
        return self.payload


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)


class PcmHttpPrimitiveTest(unittest.IsolatedAsyncioTestCase):
    def client(self, *responses):
        client = DoorfastClient.__new__(DoorfastClient)
        client.base_url = "http://doorfast/cgi-bin/doorfast"
        client.session = Session(*responses)
        return client

    async def test_emits_exact_open_submit_and_end_requests(self):
        token = "a" * 32
        client = self.client(
            Response(200, {"status": "success"}),
            Response(503, {"error": "send_unavailable", "accepted_frames": 1}),
            Response(409, {"error": "session_expired"}),
        )

        opened = await client.pcm_session_open("0123456789abcdef", 7)
        submitted = await client.pcm_submit("0123456789abcdef", 7, 9, token, b"x" * 320)
        ended = await client.pcm_session_end("0123456789abcdef", 7, token)

        self.assertEqual((200, {"status": "success"}), (opened.status, opened.payload))
        self.assertEqual(503, submitted.status)
        self.assertEqual("send_unavailable", submitted.payload["error"])
        self.assertEqual(409, ended.status)
        open_request, submit_request, end_request = client.session.requests
        self.assertEqual(("POST", "http://doorfast/cgi-bin/doorfast/api/v1/audio/session"), open_request[:2])
        self.assertEqual({"runtime": "0123456789abcdef", "generation": "7"}, open_request[2]["params"])
        self.assertEqual(b"", open_request[2]["data"])
        self.assertEqual({}, open_request[2]["headers"])
        self.assertEqual("http://doorfast/cgi-bin/doorfast/api/v1/audio/submit.pcm", submit_request[1])
        self.assertEqual({"runtime": "0123456789abcdef", "generation": "7", "sequence": "9"}, submit_request[2]["params"])
        self.assertEqual({"Content-Type": "application/octet-stream", "X-Doorfast-Audio-Session": token}, submit_request[2]["headers"])
        self.assertEqual(b"x" * 320, submit_request[2]["data"])
        self.assertEqual("http://doorfast/cgi-bin/doorfast/api/v1/audio/session/end", end_request[1])
        self.assertEqual({"X-Doorfast-Audio-Session": token}, end_request[2]["headers"])
        self.assertEqual(b"", end_request[2]["data"])

    async def test_parses_conflict_json_before_returning_status(self):
        client = self.client(Response(409, {"error": "sequence_duplicate", "next_sequence": 10}))
        reply = await client.pcm_submit("0123456789abcdef", 7, 9, "a" * 32, b"x" * 320)
        self.assertEqual(409, reply.status)
        self.assertEqual(10, reply.payload["next_sequence"])

    async def test_rejects_non_object_json_even_on_error_status(self):
        client = self.client(Response(503, []))
        with self.assertRaises(ValueError):
            await client.pcm_session_open("0123456789abcdef", 7)


if __name__ == "__main__":
    unittest.main()
