"""Tests for the independent Doorfast v1 station audio reader."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import struct
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
client_types = load_module(
    "custom_components.doorfast.client_types", COMPONENT / "client_types.py"
)
audio_module = load_module("custom_components.doorfast.audio", COMPONENT / "audio.py")
client_module = load_module("custom_components.doorfast.client", COMPONENT / "client.py")
DoorfastClient = client_module.DoorfastClient
AudioIdentity = client_types.AudioIdentity
AudioStreamError = client_types.AudioStreamError
decode_pcm_wav = audio_module.decode_pcm_wav


def synthetic_wav(samples=800):
    pcm = bytes(samples * 2)
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(pcm), b"WAVE",
        b"fmt ", 16, 1, 1, 8000, 16000, 2, 16, b"data", len(pcm),
    ) + pcm


class FakeContent:
    def __init__(self, body, step=257):
        self.body = body
        self.step = step
        self.offset = 0
        self.read_sizes = []

    async def read(self, size=-1):
        self.read_sizes.append(size)
        if self.offset >= len(self.body):
            return b""
        end = min(self.offset + self.step, self.offset + size)
        part = self.body[self.offset:end]
        self.offset = end
        return part


class FakeResponse:
    def __init__(self, status, body=b"", headers=None, step=257):
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent(body, step=step)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.responses.pop(0)


def make_audio_client(body, identity, previous_revision, revision):
    headers = {
        "X-Doorfast-Runtime-Id": identity.runtime_id,
        "X-Doorfast-Station-Id": identity.station_id,
        "X-Doorfast-Generation": str(identity.generation),
        "X-Doorfast-Audio-Previous-Revision": str(previous_revision),
        "X-Doorfast-Audio-Revision": str(revision),
        "X-Doorfast-Dropped-Bytes": "0",
        "X-Doorfast-Discontinuity": "0",
        "X-Doorfast-Timestamp-Ms": "1000",
        "X-Doorfast-Latest-Timestamp-Ms": "1000",
        "X-Doorfast-Format": "s16le",
        "X-Doorfast-Sample-Rate": "8000",
        "X-Doorfast-Channels": "1",
    }
    response = FakeResponse(200, body, headers)
    client = DoorfastClient.__new__(DoorfastClient)
    client.base_url = "http://doorfast/cgi-bin/doorfast"
    client.session = FakeSession(response)
    client.status = {}
    client._audio_generation = None
    client._audio_revision = None
    client._audio_etag = None
    client._test_response = response
    return client


def response_client(identity, status, body=b"", headers=None, step=257):
    response = FakeResponse(status, body, headers, step=step)
    client = DoorfastClient.__new__(DoorfastClient)
    client.base_url = "http://doorfast/cgi-bin/doorfast"
    client.session = FakeSession(response)
    client.status = {}
    client._audio_generation = None
    client._audio_revision = None
    client._audio_etag = None
    client._test_response = response
    return client


def identity_headers(identity):
    return {
        "X-Doorfast-Runtime-Id": identity.runtime_id,
        "X-Doorfast-Station-Id": identity.station_id,
        "X-Doorfast-Generation": str(identity.generation),
    }


class AudioClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_v1_chunk_does_not_consume_legacy_cursor(self):
        identity = AudioIdentity("0123456789abcdef", "gate_main", 7)
        client = make_audio_client(synthetic_wav(), identity, 30, 40)
        reply = await client.read_audio_chunk(identity, after=30)
        self.assertEqual("chunk", reply.state)
        self.assertEqual(bytes(1600), reply.chunk.pcm)
        self.assertIsNone(client._audio_revision)
        request = client.session.requests[0][1]
        self.assertEqual(
            {
                "runtime": identity.runtime_id,
                "station_id": identity.station_id,
                "generation": 7,
                "after": 30,
            },
            request["params"],
        )
        self.assertTrue(all(size <= 1024 for size in client._test_response.content.read_sizes))

    def test_identity_requires_talking_matching_v1_status(self):
        status = {
            "runtime_id": "0123456789abcdef",
            "call": {
                "session": "talking",
                "station_id": "gate_main",
                "generation": 7,
            },
            "audio": {"pcm_stream_version": 1},
        }
        self.assertEqual(
            AudioIdentity("0123456789abcdef", "gate_main", 7),
            AudioIdentity.from_status(status, "gate_main", expected_generation=7),
        )
        cases = (
            {**status, "runtime_id": "0123456789abcdeF"},
            {**status, "call": {**status["call"], "session": "idle"}},
            {**status, "call": {**status["call"], "station_id": "gate_side"}},
            {**status, "call": {**status["call"], "generation": 0}},
            {**status, "audio": {"pcm_stream_version": True}},
        )
        for invalid in cases:
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(AudioStreamError, "audio_identity_mismatch"):
                    AudioIdentity.from_status(invalid, "gate_main")
        with self.assertRaisesRegex(AudioStreamError, "audio_identity_mismatch"):
            AudioIdentity.from_status(status, "Gate-main")
        with self.assertRaisesRegex(AudioStreamError, "audio_identity_mismatch"):
            AudioIdentity.from_status(status, "gate_main", expected_generation=8)

    async def test_304_and_404_require_matching_identity_headers(self):
        identity = AudioIdentity("0123456789abcdef", "gate_main", 7)
        client = response_client(identity, 304, headers=identity_headers(identity))
        self.assertEqual("empty", (await client.read_audio_chunk(identity)).state)
        client = response_client(
            identity,
            404,
            b'{"code":"audio_unavailable"}',
            identity_headers(identity),
        )
        self.assertEqual("empty", (await client.read_audio_chunk(identity)).state)
        client = response_client(identity, 404, b'{"code":"audio_unavailable"}')
        with self.assertRaisesRegex(AudioStreamError, "audio_identity_mismatch"):
            await client.read_audio_chunk(identity)

    async def test_error_states_require_known_codes(self):
        identity = AudioIdentity("0123456789abcdef", "gate_main", 7)
        for status, body, expected in (
            (409, b'{"code":"audio_cursor_expired"}', "resync"),
            (503, b'{"code":"audio_temporarily_unavailable"}', "retry"),
        ):
            with self.subTest(status=status):
                client = response_client(identity, status, body)
                self.assertEqual(expected, (await client.read_audio_chunk(identity)).state)
        client = response_client(identity, 409, b'{"code":"unknown"}')
        with self.assertRaisesRegex(AudioStreamError, "audio_invalid_response"):
            await client.read_audio_chunk(identity)

    async def test_rejects_bad_revision_metadata_and_oversized_stream(self):
        identity = AudioIdentity("0123456789abcdef", "gate_main", 7)
        headers = identity_headers(identity)
        headers.update({
            "X-Doorfast-Audio-Previous-Revision": "30",
            "X-Doorfast-Audio-Revision": "40",
            "X-Doorfast-Dropped-Bytes": "0",
            "X-Doorfast-Discontinuity": "0",
            "X-Doorfast-Timestamp-Ms": "1000",
            "X-Doorfast-Latest-Timestamp-Ms": "1000",
            "X-Doorfast-Format": "s16le",
            "X-Doorfast-Sample-Rate": "8000",
            "X-Doorfast-Channels": "1",
        })
        for updates in (
            {"X-Doorfast-Audio-Revision": "30"},
            {"X-Doorfast-Timestamp-Ms": "1001"},
            {"X-Doorfast-Sample-Rate": "16000"},
        ):
            malformed = {**headers, **updates}
            client = response_client(identity, 200, synthetic_wav(), malformed)
            with self.subTest(updates=updates):
                with self.assertRaisesRegex(AudioStreamError, "audio_invalid_response"):
                    await client.read_audio_chunk(identity, after=30)
        client = response_client(identity, 200, b"x" * 4097, headers)
        with self.assertRaisesRegex(AudioStreamError, "audio_invalid_response"):
            await client.read_audio_chunk(identity)

    def test_strict_wav_parser_rejects_malformed_and_accepts_padding(self):
        self.assertEqual(bytes(1600), decode_pcm_wav(synthetic_wav()))
        malformed = (
            synthetic_wav()[:-1],
            synthetic_wav() + b"tail",
            synthetic_wav(samples=0),
            synthetic_wav(samples=1601),
        )
        for body in malformed:
            with self.subTest(length=len(body)):
                with self.assertRaisesRegex(AudioStreamError, "audio_invalid_response"):
                    decode_pcm_wav(body)
