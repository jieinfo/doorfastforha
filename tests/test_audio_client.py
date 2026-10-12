"""Tests for the independent Doorfast v1 station audio reader."""

from __future__ import annotations

import importlib.util
import json
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


def extended_wav(extra_size):
    """Insert a padded unknown chunk while retaining a real PCM fmt/data pair."""
    base = synthetic_wav()
    extra = b"JUNK" + struct.pack("<I", extra_size) + bytes(extra_size)
    if extra_size % 2:
        extra += b"\0"
    body = base[:12] + extra + base[12:]
    return body[:4] + struct.pack("<I", len(body) - 8) + body[8:]


class FakeContent:
    def __init__(self, body, step=257):
        self.body = body
        self.step = step
        self.offset = 0
        self.read_sizes = []
        self.returned_bytes = 0

    async def read(self, size=-1):
        self.read_sizes.append(size)
        if self.offset >= len(self.body):
            return b""
        end = min(len(self.body), self.offset + self.step, self.offset + size)
        part = self.body[self.offset:end]
        self.offset = end
        self.returned_bytes += len(part)
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
        "X-Doorfast-Audio-Dropped-Bytes": "0",
        "X-Doorfast-Audio-Discontinuity": "0",
        "X-Doorfast-Audio-Timestamp-Ms": "1000",
        "X-Doorfast-Audio-Latest-Timestamp-Ms": "1000",
        "X-Doorfast-Audio-Format": "s16le",
        "X-Doorfast-Audio-Sample-Rate": "8000",
        "X-Doorfast-Audio-Channels": "1",
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
            "X-Doorfast-Audio-Dropped-Bytes": "0",
            "X-Doorfast-Audio-Discontinuity": "0",
            "X-Doorfast-Audio-Timestamp-Ms": "1000",
            "X-Doorfast-Audio-Latest-Timestamp-Ms": "1000",
            "X-Doorfast-Audio-Format": "s16le",
            "X-Doorfast-Audio-Sample-Rate": "8000",
            "X-Doorfast-Audio-Channels": "1",
        })
        for updates in (
            {"X-Doorfast-Audio-Revision": "30"},
            {"X-Doorfast-Audio-Timestamp-Ms": "1001"},
            {"X-Doorfast-Audio-Sample-Rate": "16000"},
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

    async def test_consumed_body_never_exceeds_budget_even_with_small_length(self):
        identity = AudioIdentity("0123456789abcdef", "gate_main", 7)
        for status in (200, 400, 404, 409, 503):
            for length in (4096, 4097, 8192):
                with self.subTest(status=status, length=length):
                    client = response_client(
                        identity, status, b"x" * length,
                        {**identity_headers(identity), "Content-Length": "1"},
                    )
                    with self.assertRaisesRegex(AudioStreamError, "audio_invalid_response"):
                        await client.read_audio_chunk(identity)
                    self.assertLessEqual(client._test_response.content.returned_bytes, 4096)
                    self.assertEqual(4096, client._test_response.content.returned_bytes)

    async def test_valid_400_preserves_invalid_request_code(self):
        identity = AudioIdentity("0123456789abcdef", "gate_main", 7)
        client = response_client(identity, 400, b'{"code":"invalid_request"}')
        with self.assertRaises(AudioStreamError) as raised:
            await client.read_audio_chunk(identity)
        self.assertEqual("invalid_request", raised.exception.code)

    async def test_exact_budget_wav_rejected_without_eof_probe(self):
        identity = AudioIdentity("0123456789abcdef", "gate_main", 7)
        client = make_audio_client(extended_wav(2444), identity, 30, 40)
        self.assertEqual(4096, len(client._test_response.content.body))
        with self.assertRaisesRegex(AudioStreamError, "audio_invalid_response"):
            await client.read_audio_chunk(identity)
        self.assertEqual(4096, client._test_response.content.returned_bytes)
        self.assertEqual(16, len(client._test_response.content.read_sizes))

    async def test_status_and_code_must_match_and_malformed_json_is_rejected(self):
        identity = AudioIdentity("0123456789abcdef", "gate_main", 7)
        codes = ("invalid_request", "audio_identity_mismatch", "audio_cursor_expired",
                 "audio_unavailable", "audio_temporarily_unavailable", "unknown")
        allowed = {400: {"invalid_request"}, 409: {"audio_identity_mismatch", "audio_cursor_expired"},
                   404: {"audio_unavailable"}, 503: {"audio_temporarily_unavailable"}}
        for status in (400, 404, 409, 503, 500):
            for code in codes:
                if code in allowed.get(status, set()):
                    continue
                with self.subTest(status=status, code=code):
                    client = response_client(identity, status, json.dumps({"code": code}).encode(),
                                             identity_headers(identity))
                    with self.assertRaises(AudioStreamError) as raised:
                        await client.read_audio_chunk(identity)
                    self.assertEqual("audio_invalid_response", raised.exception.code)
            for body in (b"{", b"\xff", b"[]", b"null", b'{"code":true}', b'{}', b''):
                with self.subTest(status=status, malformed=body):
                    client = response_client(identity, status, body, identity_headers(identity))
                    with self.assertRaisesRegex(AudioStreamError, "audio_invalid_response"):
                        await client.read_audio_chunk(identity)

    def test_every_identity_guard_and_uint64_boundary(self):
        status = {"runtime_id": "0123456789abcdef", "call": {
            "session": "talking", "station_id": "gate_main", "generation": 7},
            "audio": {"pcm_stream_version": 1, "snapshot_ready": False}}
        invalid_statuses = [None, [], {}, {**status, "call": None},
                            {**status, "call": []}, {**status, "audio": None},
                            {**status, "audio": []}]
        for runtime in (None, 7, "", "0123456789abcde", "0123456789abcdef0",
                        "0123456789abcdeF", "g123456789abcdef", "０123456789abcdef"):
            invalid_statuses.append({**status, "runtime_id": runtime})
        for field, values in (("session", (None, "idle", "ringing", True)),
                              ("station_id", (None, "gate_side", "Gate_main")),
                              ("generation", (None, True, False, "7", 7.0, 0, -1, 1 << 64))):
            for value in values:
                invalid_statuses.append({**status, "call": {**status["call"], field: value}})
        for version in (None, True, False, "1", 1.0, 0, 2):
            invalid_statuses.append({**status, "audio": {"pcm_stream_version": version}})
        for invalid in invalid_statuses:
            with self.subTest(status=invalid):
                with self.assertRaises(AudioStreamError) as raised:
                    AudioIdentity.from_status(invalid, "gate_main")
                self.assertEqual("audio_identity_mismatch", raised.exception.code)
        for station in (None, 7, "", "A", "_gate", "1gate", "gate-main", "gate.main",
                        "gate main", "gâte", "a" * 33):
            with self.subTest(station=station):
                with self.assertRaisesRegex(AudioStreamError, "audio_identity_mismatch"):
                    AudioIdentity.from_status(status, station)
        for generation in (True, False, 0, -1, "7", 7.0, 1 << 64, 8):
            with self.subTest(expected=generation):
                with self.assertRaisesRegex(AudioStreamError, "audio_identity_mismatch"):
                    AudioIdentity.from_status(status, "gate_main", generation)
        generation_one = {**status, "call": {**status["call"], "generation": 1}}
        with self.assertRaisesRegex(AudioStreamError, "audio_identity_mismatch"):
            AudioIdentity.from_status(generation_one, "gate_main", True)
        for station in ("a", "a0_", "a" * 32):
            for generation in (1, (1 << 64) - 1):
                valid = {**status, "call": {**status["call"], "station_id": station,
                                           "generation": generation}}
                self.assertEqual(AudioIdentity(status["runtime_id"], station, generation),
                                 AudioIdentity.from_status(valid, station, generation))

    async def test_missing_and_wrong_identity_headers_are_fatal(self):
        identity = AudioIdentity("0123456789abcdef", "gate_main", 7)
        for status in (200, 304, 404):
            for header, wrong in (("X-Doorfast-Runtime-Id", "fedcba9876543210"),
                                  ("X-Doorfast-Station-Id", "gate_side"),
                                  ("X-Doorfast-Generation", "8")):
                for value in (None, wrong):
                    with self.subTest(status=status, header=header, value=value):
                        client = make_audio_client(synthetic_wav(), identity, 30, 40)
                        response = client._test_response
                        response.status = status
                        if status == 404:
                            response.content = FakeContent(b'{"code":"audio_unavailable"}')
                        if value is None:
                            response.headers.pop(header)
                        else:
                            response.headers[header] = value
                        with self.assertRaisesRegex(AudioStreamError, "audio_identity_mismatch"):
                            await client.read_audio_chunk(identity)
        client = response_client(identity, 409, b'{"code":"audio_identity_mismatch"}')
        with self.assertRaisesRegex(AudioStreamError, "audio_identity_mismatch"):
            await client.read_audio_chunk(identity)

    async def test_all_uint64_metadata_missing_and_malformed(self):
        identity = AudioIdentity("0123456789abcdef", "gate_main", 7)
        for header in ("X-Doorfast-Audio-Previous-Revision", "X-Doorfast-Audio-Revision",
                       "X-Doorfast-Audio-Dropped-Bytes", "X-Doorfast-Audio-Timestamp-Ms",
                       "X-Doorfast-Audio-Latest-Timestamp-Ms"):
            for invalid in (None, "", True, 1, "-1", "+1", "1.0", " 1", "１", str(1 << 64)):
                with self.subTest(header=header, invalid=invalid):
                    client = make_audio_client(synthetic_wav(), identity, 30, 40)
                    if invalid is None:
                        client._test_response.headers.pop(header)
                    else:
                        client._test_response.headers[header] = invalid
                    with self.assertRaisesRegex(AudioStreamError, "audio_invalid_response"):
                        await client.read_audio_chunk(identity, after=30)
        for previous, revision in ((40, 30), (40, 40), (29, 40), (31, 40)):
            client = make_audio_client(synthetic_wav(), identity, previous, revision)
            with self.subTest(previous=previous, revision=revision):
                with self.assertRaisesRegex(AudioStreamError, "audio_invalid_response"):
                    await client.read_audio_chunk(identity, after=30)

    async def test_all_format_metadata_missing_and_malformed(self):
        identity = AudioIdentity("0123456789abcdef", "gate_main", 7)
        for header, values in (
            ("X-Doorfast-Audio-Discontinuity", (None, True, 0, "2", "true", "")),
            ("X-Doorfast-Audio-Format", (None, "alaw", "S16LE")),
            ("X-Doorfast-Audio-Sample-Rate", (None, "16000", 8000)),
            ("X-Doorfast-Audio-Channels", (None, "2", 1)),
        ):
            for invalid in values:
                with self.subTest(header=header, invalid=invalid):
                    client = make_audio_client(synthetic_wav(), identity, 30, 40)
                    if invalid is None:
                        client._test_response.headers.pop(header)
                    else:
                        client._test_response.headers[header] = invalid
                    with self.assertRaisesRegex(AudioStreamError, "audio_invalid_response"):
                        await client.read_audio_chunk(identity)

    async def test_pcm_max_and_metadata_semantics_and_optional_after(self):
        identity = AudioIdentity("0123456789abcdef", "gate_main", 7)
        maximum = (1 << 64) - 1
        client = make_audio_client(synthetic_wav(1600), identity, maximum - 1, maximum)
        headers = client._test_response.headers
        headers.update({"X-Doorfast-Audio-Dropped-Bytes": str(maximum),
                        "X-Doorfast-Audio-Discontinuity": "1",
                        "X-Doorfast-Audio-Timestamp-Ms": "1000",
                        "X-Doorfast-Audio-Latest-Timestamp-Ms": str(maximum)})
        reply = await client.read_audio_chunk(identity)
        self.assertEqual(bytes(3200), reply.chunk.pcm)
        self.assertEqual(maximum, reply.chunk.dropped_samples)
        self.assertEqual(1000, reply.chunk.timestamp_ms)
        self.assertEqual(maximum, reply.chunk.latest_timestamp_ms)
        self.assertTrue(reply.chunk.discontinuity)
        self.assertNotIn("after", client.session.requests[0][1]["params"])
        self.assertEqual({"total": 2}, client.session.requests[0][1]["timeout"])
        client = make_audio_client(synthetic_wav(), identity, 0, 1)
        reply = await client.read_audio_chunk(identity, after=0)
        self.assertEqual(0, reply.chunk.previous_revision)
        self.assertEqual(0, reply.chunk.dropped_samples)
        self.assertFalse(reply.chunk.discontinuity)
        self.assertEqual(0, client.session.requests[0][1]["params"]["after"])

    async def test_invalid_read_inputs_rejected_before_http(self):
        identity = AudioIdentity("0123456789abcdef", "gate_main", 7)
        for after in (True, False, -1, 1 << 64, "30", 30.0):
            client = response_client(identity, 200)
            with self.subTest(after=after):
                with self.assertRaisesRegex(AudioStreamError, "invalid_request"):
                    await client.read_audio_chunk(identity, after)
                self.assertEqual([], client.session.requests)
        for invalid in (AudioIdentity("bad", "gate_main", 7),
                        AudioIdentity(identity.runtime_id, "bad-name", 7),
                        AudioIdentity(identity.runtime_id, "gate_main", True),
                        AudioIdentity(identity.runtime_id, "gate_main", 0),
                        AudioIdentity(identity.runtime_id, "gate_main", 1 << 64)):
            client = response_client(identity, 200)
            with self.subTest(identity=invalid):
                with self.assertRaisesRegex(AudioStreamError, "audio_identity_mismatch"):
                    await client.read_audio_chunk(invalid)
                self.assertEqual([], client.session.requests)

    def test_wav_format_guards(self):
        # Removing each corresponding fmt guard must allow this malformed input.
        for offset, encoding, invalid in ((20, "<H", 3), (22, "<H", 2),
                                           (24, "<I", 16000), (28, "<I", 32000),
                                           (32, "<H", 4), (34, "<H", 8)):
            body = bytearray(synthetic_wav())
            struct.pack_into(encoding, body, offset, invalid)
            with self.subTest(offset=offset):
                with self.assertRaisesRegex(AudioStreamError, "audio_invalid_response"):
                    decode_pcm_wav(body)

    def test_real_padding_and_riff_chunk_boundaries(self):
        self.assertEqual(bytes(1600), decode_pcm_wav(extended_wav(1)))
        self.assertEqual(bytes(1600), decode_pcm_wav(extended_wav(3)))
        self.assertEqual(bytes(3200), decode_pcm_wav(synthetic_wav(1600)))
        base = synthetic_wav()
        def riff(chunks):
            return b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WAVE" + chunks
        fmt, data = base[12:36], base[36:]
        malformed = (None, b"", b"RIFX" + base[4:], base[:8] + b"NOPE" + base[12:],
                     riff(fmt + b"data" + struct.pack("<I", 1) + b"x\0"),
                     riff(fmt + b"data" + struct.pack("<I", 1) + b"x"),
                     riff(fmt + b"JUNK" + struct.pack("<I", 1) + b"x" + data),
                     riff(fmt + fmt + data), riff(fmt + data + data),
                     riff(data), riff(fmt), riff(fmt + data + b"abcd"),
                     riff(b"fmt " + struct.pack("<I", 18) + fmt[8:] + b"\0\0" + data))
        for index, body in enumerate(malformed):
            with self.subTest(case=index):
                with self.assertRaisesRegex(AudioStreamError, "audio_invalid_response"):
                    decode_pcm_wav(body)

    async def test_extended_wav_below_budget_reaches_eof_and_decodes(self):
        identity = AudioIdentity("0123456789abcdef", "gate_main", 7)
        client = make_audio_client(extended_wav(2442), identity, 30, 40)
        reply = await client.read_audio_chunk(identity)
        self.assertEqual(bytes(1600), reply.chunk.pcm)
        self.assertEqual(4094, client._test_response.content.returned_bytes)
