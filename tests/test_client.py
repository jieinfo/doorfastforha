"""Tests for command payloads emitted by the Doorfast client."""

import asyncio
import importlib.util
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
    async def test_binds_all_controls_to_current_runtime(self):
        client = DoorfastClient.__new__(DoorfastClient)
        client.status = {
            "runtime_id": "0123456789abcdef",
            "call": {"generation": 7},
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
                    {"runtime_id": "0123456789abcdef", "generation": 7},
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
                    await client.unlock()
                with self.assertRaises(ValueError):
                    await client.call_elevator()

        client._request.assert_not_awaited()


class MonitorPayloadTest(unittest.IsolatedAsyncioTestCase):
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
