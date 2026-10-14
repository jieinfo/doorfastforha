"""Synthetic HTTP streams exercise the real v1 parser/hub/WS/producer chain."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import sys
import types
import unittest
from unittest.mock import patch

# Reuse only the existing dependency stubs, not its decoder or client fixtures.
import test_audio_client  # noqa: F401


COMPONENT = Path(__file__).parents[1] / "custom_components" / "doorfast"
PACKAGE = "_doorfast_audio_contract"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(COMPONENT)]
sys.modules[PACKAGE] = package


def load(name):
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", COMPONENT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


for dependency in ("const", "generation", "client_types", "audio", "pcm"):
    load(dependency)
client_module = load("client")
ws = load("websocket")
AudioStreamError = sys.modules[f"{PACKAGE}.client_types"].AudioStreamError
RUNTIME = "0123456789abcdef"
REBOOT_RUNTIME = "fedcba9876543210"
PCM = struct.pack("<hhhh", -32768, -1, 0, 32767) * 200
PCM_DIGEST = hashlib.sha256(PCM).hexdigest()


def status(runtime=RUNTIME):
    return {
        "runtime_id": runtime,
        "call": {"session": "talking", "station_id": "gate_main", "generation": 7},
        "audio": {"pcm_stream_version": 1},
        "audio_tx": {"active": True, "generation": 7},
    }


def outbound_status():
    return {**status(),
        "station_call": {"operation_id": 3, "station_id": "gate_main", "stage": "talking",
                         "media_generation": 9, "call_generation": 7,
                         "timer": "session", "remaining_ms": 12000, "error": "none"},
        "media": {"available": True, "sessions": [
            {"station_id": "gate_main", "generation": 9, "purpose": "call",
             "state": "viewing", "ready": True, "source_live": True}]},
    }


def inadmissible_statuses():
    # Each mutation breaks a distinct authoritative fact, never DSP/PCM details.
    cases = []
    for field, value in (("call_generation", 8), ("media_generation", 10),
                         ("station_id", "gate_side"), ("operation_id", True),
                         ("remaining_ms", 1 << 64), ("error", "raw secret"),
                         ("stage", "calling"), ("stage", "preparing"), ("stage", "ending")):
        sample = outbound_status()
        sample["station_call"][field] = value
        cases.append((field + str(value), sample))
    for field, value in (("session", "idle"), ("generation", 0), ("station_id", "gate_side")):
        sample = outbound_status()
        sample["call"][field] = value
        cases.append((field + str(value), sample))
    for value in (None, {}, "queued"):
        sample = outbound_status()
        sample["station_call"] = value
        cases.append(("malformed" + str(value), sample))
    for field, value in (("generation", 10), ("purpose", "preview"), ("state", "idle")):
        sample = outbound_status()
        sample["media"]["sessions"][0][field] = value
        cases.append(("media" + field, sample))
    sample = outbound_status()
    sample["media"]["sessions"] *= 2
    cases.append(("ambiguous media", sample))
    sample = outbound_status()
    sample["media"]["available"] = False
    cases.append(("unavailable media", sample))
    sample = outbound_status()
    sample.pop("station_call")
    sample["call"].update(session="idle", generation=0)
    sample["queued"] = True
    cases.append(("legacy purpose-only ready queued idle", sample))
    return cases


def identity_headers(runtime=RUNTIME):
    return {
        "X-Doorfast-Runtime-Id": runtime,
        "X-Doorfast-Station-Id": "gate_main",
        "X-Doorfast-Generation": "7",
    }


class Content:
    """Finite streaming HTTP boundary; retain only consumption statistics."""

    def __init__(self, body):
        self.body = body
        self.consumed = 0
        self.requests = []

    async def read(self, size):
        self.requests.append(size)
        part = self.body[self.consumed:self.consumed + min(size, 257)]
        self.consumed += len(part)
        return part


class Response:
    def __init__(self, code=200, body=b"", headers=None, payload=None):
        self.status = code
        self.headers = headers or {}
        self.content = Content(body)
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        if self.status != 200:
            raise RuntimeError("synthetic HTTP failure")

    async def json(self, **kwargs):
        return self.payload


def chunk(previous, revision, runtime=RUNTIME):
    wav = struct.pack(
        "<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(PCM), b"WAVE", b"fmt ",
        16, 1, 1, 8000, 16000, 2, 16, b"data", len(PCM),
    ) + PCM
    headers = {
        **identity_headers(runtime),
        "X-Doorfast-Audio-Previous-Revision": str(previous),
        "X-Doorfast-Audio-Revision": str(revision),
        "X-Doorfast-Audio-Dropped-Bytes": "0",
        "X-Doorfast-Audio-Discontinuity": "0",
        "X-Doorfast-Audio-Timestamp-Ms": "1100",
        "X-Doorfast-Audio-Latest-Timestamp-Ms": "1200",
        "X-Doorfast-Audio-Format": "s16le",
        "X-Doorfast-Audio-Sample-Rate": "8000",
        "X-Doorfast-Audio-Channels": "1",
    }
    return Response(body=wav, headers=headers)


class HttpBoundary:
    def __init__(self):
        self.current_status = status()
        self.responses = []
        self.audio_requests = []
        self.producer_requests = []
        self.pcm_summaries = []
        self.sequence = 0

    def get(self, url, **kwargs):
        self.audio_requests.append((url, kwargs["params"], kwargs["headers"]))
        return self.responses.pop(0)

    def request(self, method, url, **kwargs):
        if url.endswith("/api/v1/status"):
            if method != "GET":
                raise AssertionError("status must be GET")
            return Response(payload=self.current_status)
        if method != "POST":
            raise AssertionError("producer must be POST")
        if not any(url.endswith(path) for path in (
            "/api/v1/audio/session", "/api/v1/audio/session/end", "/api/v1/audio/submit.pcm",
        )):
            raise AssertionError("unexpected producer route")
        params = kwargs["params"]
        ending = url.endswith("/session/end")
        submitting = url.endswith("/submit.pcm")
        self.producer_requests.append((url, dict(params)))
        accepted = len(kwargs["data"]) // 320 if submitting else 0
        if submitting:
            self.pcm_summaries.append((len(kwargs["data"]), hashlib.sha256(kwargs["data"]).hexdigest()))
            self.sequence += accepted
        return Response(payload={
            "status": "success", "runtime_id": params["runtime"],
            "generation": int(params["generation"]),
            "audio_session": "" if ending or submitting else "a" * 32,
            "accepted_frames": accepted, "next_sequence": self.sequence,
            "lease_ms": 0 if ending else 2000,
        })


class Connection:
    def __init__(self):
        self.user = types.SimpleNamespace(is_admin=True)
        self.subscriptions = {}
        self.results = []
        self.errors = []

    def send_result(self, ident, result):
        self.results.append(result)

    def send_error(self, ident, code, message):
        self.errors.append(AudioStreamError(code))


class Timer:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class AudioContractTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.http = HttpBoundary()
        self.client = client_module.DoorfastClient(None, "http://synthetic.invalid/cgi-bin/doorfast")
        self.client.session = self.http
        self.client.status = status()
        hass = types.SimpleNamespace(data={"doorfast": {"one": self.client}}, async_create_task=asyncio.create_task)
        self.manager = ws.PcmWebSocketManager(hass)
        self.connections = [Connection(), Connection()]
        self.now = 0.0
        self.timers = []

        def later(delay, callback, *args):
            self.assertEqual(5.0, delay)
            timer = Timer()
            self.timers.append(timer)
            return timer

        self.timer_patch = patch.object(asyncio.get_running_loop(), "call_later", later)
        self.timer_patch.start()

    async def asyncTearDown(self):
        await self.manager.release_entry("one")
        self.timer_patch.stop()

    async def listen(self, connection):
        await self.manager.listen_start(connection, {
            "id": 1, "config_entry_id": "one", "station_id": "gate_main", "generation": 7,
        })
        self.assertEqual([], connection.errors)
        self.manager._hubs["one"]._clock = lambda: self.now
        return connection.results[-1]["listener_id"]

    async def read(self, connection, listener_id):
        await self.manager.listen_read(connection, {
            "id": 2, "config_entry_id": "one", "listener_id": listener_id,
        })
        return connection.results[-1]

    def assert_released(self):
        self.assertEqual({}, self.manager._listeners)
        self.assertEqual({}, self.manager._captures)
        self.assertEqual({}, self.manager._hubs)
        self.assertEqual({}, self.manager._by_entry)
        self.assertEqual({}, self.manager._starting)
        self.assertEqual({}, self.manager._pending_cleanups)
        self.assertTrue(all(timer.cancelled for timer in self.timers))
        self.assertTrue(all(not connection.subscriptions for connection in self.connections))

    async def test_outbound_identity_conflicts_cannot_admit_listener_or_microphone(self):
        for name, sample in inadmissible_statuses():
            with self.subTest(case=name):
                self.http.current_status = sample
                self.client.status = sample
                listener, microphone = Connection(), Connection()
                try:
                    await self.manager.listen_start(listener, {
                        "id": 1, "config_entry_id": "one", "station_id": "gate_main", "generation": 7})
                    await self.manager.start(microphone, {
                        "id": 2, "config_entry_id": "one", "station_id": "gate_main", "generation": 7})
                    self.assertEqual([], listener.results)
                    self.assertEqual(["audio_identity_mismatch"], [error.code for error in listener.errors])
                    self.assertEqual([], microphone.results)
                    self.assertTrue(microphone.errors)
                    self.assertEqual([], self.http.producer_requests)
                    self.assert_released()
                finally:
                    await self.manager.release_entry("one")
                    self.http.producer_requests.clear()

    async def test_matched_outbound_and_new_incoming_ignore_terminal_history(self):
        samples = [outbound_status(), status()]
        for stage in ("ended", "failed"):
            for station in ("gate_main", "gate_side"):
                sample = outbound_status()
                sample["station_call"].update(stage=stage, station_id=station,
                    call_generation=6, media_generation=8, timer="none", remaining_ms=0)
                sample.pop("media")
                samples.append(sample)
        for sample in samples:
            with self.subTest(operation=sample.get("station_call")):
                self.http.current_status = sample
                self.client.status = sample
                connection = Connection()
                listener_id = await self.listen(connection)
                await self.manager.start(connection, {
                    "id": 3, "config_entry_id": "one", "listener_id": listener_id})
                self.assertEqual([], connection.errors)
                self.assertEqual("active", connection.results[-1]["state"])
                self.assertEqual("7", self.http.producer_requests[-1][1]["generation"])
                await self.manager.release_entry("one")
                self.assert_released()

    async def test_real_microphone_rejects_mapping_conflict_before_session_open(self):
        sample = outbound_status()
        sample["station_call"]["call_generation"] = 8
        self.http.current_status = sample
        self.client.status = sample
        connection = Connection()
        await self.manager.start(connection, {
            "id": 1, "config_entry_id": "one", "station_id": "gate_main", "generation": 7})
        self.assertEqual([], connection.results)
        self.assertEqual(["status_not_ready"], [error.code for error in connection.errors])
        self.assertEqual([], self.http.producer_requests)
        self.assert_released()

    async def test_http_stream_resync_and_reused_generation_release_the_whole_chain(self):
        """Ignoring a terminal identity failure would retain old hub/producer ownership."""
        manager = self.manager
        a, b = self.connections
        first_id, second_id = await self.listen(a), await self.listen(b)
        hub = manager._hubs["one"]
        responses = [
            chunk(30, 40), Response(304, headers=identity_headers()),
            Response(503, b'{"error":"temporary","code":"audio_temporarily_unavailable"}'),
            Response(409, b'{"error":"cursor expired","code":"audio_cursor_expired"}'),
            chunk(40, 50), Response(409, b'{"error":"audio identity mismatch","code":"audio_identity_mismatch"}'),
        ]
        self.http.responses = list(responses)
        first = await self.read(a, first_id)
        self.assertEqual("chunk", first["state"])
        second = await self.read(b, second_id)
        self.assertEqual((40, 40), (first["revision"], second["revision"]))
        self.assertEqual([PCM_DIGEST, PCM_DIGEST], [
            hashlib.sha256(base64.b64decode(reply["pcm"])).hexdigest() for reply in (first, second)
        ])
        self.assertEqual(1, len(self.http.audio_requests))
        await manager.start(a, {"id": 3, "config_entry_id": "one", "listener_id": first_id})
        self.assertEqual([], a.errors)
        capture_id = a.results[-1]["capture_id"]
        await manager.submit(a, {"id": 4, "config_entry_id": "one", "capture_id": capture_id,
            "pcm": base64.b64encode(PCM).decode("ascii")})
        self.assertEqual(5, a.results[-1]["accepted_frames"])
        self.assertEqual([(1600, PCM_DIGEST)], self.http.pcm_summaries)
        unchanged = await self.read(a, first_id)
        self.assertEqual("empty", unchanged["state"])
        self.now = 0.05
        self.assertEqual("empty", (await self.read(a, first_id))["state"])
        self.now = 0.1
        self.assertEqual("empty", (await self.read(a, first_id))["state"])
        resynced = await self.read(a, first_id)
        self.assertEqual((50, True), (resynced["revision"], resynced["discontinuity"]))
        self.assertEqual(50, (await self.read(b, second_id))["revision"])
        self.http.current_status = status(REBOOT_RUNTIME)
        await self.read(a, first_id)
        final_error = a.errors[-1]
        self.assertEqual("audio_identity_mismatch", final_error.code)
        self.assertEqual([None, 40, 40, 40, None, 50], [
            params.get("after") for _, params, _ in self.http.audio_requests
        ])
        for url, params, headers in self.http.audio_requests:
            self.assertEqual("http://synthetic.invalid/cgi-bin/doorfast/api/v1/audio/latest.wav", url)
            self.assertEqual((RUNTIME, "gate_main", 7), (params["runtime"], params["station_id"], params["generation"]))
            self.assertEqual({}, headers)
        self.assertEqual([], self.http.responses)
        self.assertEqual([1644, 0, 1644], [responses[index].content.consumed for index in (0, 1, 4)])
        self.assertTrue(all(response.content.consumed < 4096 for response in responses))
        self.assertTrue(all(size <= 1024 for response in responses for size in response.content.requests))
        self.assertTrue(hub.closed)
        self.assertEqual(0, len(hub._cache))
        producer_base = "http://synthetic.invalid/cgi-bin/doorfast/api/v1/audio/"
        self.assertEqual([
            (producer_base + "session", {"runtime": RUNTIME, "generation": "7"}),
            (producer_base + "submit.pcm", {"runtime": RUNTIME, "generation": "7", "sequence": "0"}),
            (producer_base + "session/end", {"runtime": RUNTIME, "generation": "7"}),
        ], self.http.producer_requests)
        self.assertNotIn("audio_session", json.dumps(a.results))
        self.assert_released()

    async def test_old_listener_cannot_open_real_producer_after_runtime_restart_with_generation_seven(self):
        """Removing PcmProducer's expected-runtime guard must open the wrong producer."""
        a = self.connections[0]
        listener_id = await self.listen(a)
        self.http.current_status = status(REBOOT_RUNTIME)
        await self.manager.start(a, {"id": 3, "config_entry_id": "one", "listener_id": listener_id})
        self.assertEqual(["runtime_mismatch"], [error.code for error in a.errors])
        self.assertEqual([], self.http.producer_requests)
        await self.manager.release_entry("one")
        self.assert_released()

    async def test_entry_http_bound_survives_stop_runtime_change_and_client_reload(self):
        """Old unabortable HTTP owns the slot until it settles, even after reload."""
        for transition in ("stop", "runtime", "reload"):
            with self.subTest(transition=transition):
                await self.manager.release_entry("one")
                old_connection, new_connection = Connection(), Connection()
                self.http.current_status = status()
                old_client = self.manager.hass.data["doorfast"]["one"]
                old_client.session = self.http
                old_id = await self.listen(old_connection)
                entered, resume = asyncio.Event(), asyncio.Event()
                active = 0
                maximum = 0
                class GatedResponse(Response):
                    async def __aenter__(response):
                        nonlocal active, maximum
                        active += 1
                        maximum = max(maximum, active)
                        entered.set()
                        await resume.wait()
                        return response
                    async def __aexit__(response, *args):
                        nonlocal active
                        active -= 1
                self.http.responses = [GatedResponse(304, headers=identity_headers())]
                old_read = asyncio.create_task(self.manager.listen_read(old_connection, {
                    "id": 2, "config_entry_id": "one", "listener_id": old_id,
                }))
                await entered.wait()
                if transition == "stop":
                    await self.manager.listen_stop(old_connection, {
                        "id": 3, "config_entry_id": "one", "listener_id": old_id,
                    })
                elif transition == "runtime":
                    self.http.current_status = status(REBOOT_RUNTIME)
                    await self.manager.reconcile("one", self.http.current_status)
                else:
                    await self.manager.close_entry("one")
                    replacement = client_module.DoorfastClient(None, "http://synthetic.invalid")
                    replacement.session = self.http
                    self.manager.hass.data["doorfast"]["one"] = replacement
                    self.manager.admit_entry("one")
                new_id = await self.listen(new_connection)
                self.http.responses.append(GatedResponse(304, headers=identity_headers(self.http.current_status["runtime_id"])))
                new_read = asyncio.create_task(self.manager.listen_read(new_connection, {
                    "id": 4, "config_entry_id": "one", "listener_id": new_id,
                }))
                await asyncio.sleep(0)
                try:
                    self.assertEqual(1, active)
                    self.assertEqual(1, len(self.http.responses), "replacement must wait before issuing HTTP")
                    new_read.cancel()
                    with self.assertRaises(asyncio.CancelledError): await new_read
                    third_connection = Connection()
                    third_id = await self.listen(third_connection)
                    third_read = asyncio.create_task(self.manager.listen_read(third_connection, {
                        "id": 5, "config_entry_id": "one", "listener_id": third_id,
                    }))
                    await asyncio.sleep(0)
                    self.assertEqual(1, active, "cancelled waiter must not unlock the old HTTP request")
                    self.assertEqual(1, len(self.http.responses))
                    # Another config entry is independent while entry one waits.
                    separate_http = HttpBoundary()
                    separate_http.responses = [Response(304, headers=identity_headers())]
                    separate = client_module.DoorfastClient(None, "http://separate.invalid")
                    separate.session = separate_http
                    self.manager.hass.data["doorfast"]["two"] = separate
                    self.manager.admit_entry("two")
                    other = Connection()
                    await self.manager.listen_start(other, {"id": 6, "config_entry_id": "two", "station_id": "gate_main"})
                    await self.manager.listen_read(other, {"id": 7, "config_entry_id": "two", "listener_id": other.results[-1]["listener_id"]})
                    self.assertEqual("empty", other.results[-1]["state"])
                finally:
                    resume.set()
                    await old_read
                    await asyncio.gather(new_read, return_exceptions=True)
                    if "third_read" in locals(): await third_read
                    await self.manager.release_entry("two")
                self.assertEqual(1, maximum)
                self.assertEqual(["not_found"], [error.code for error in old_connection.errors])
                self.assertEqual("empty", third_connection.results[-1]["state"])
                self.assertFalse(any(result.get("state") == "chunk" for result in old_connection.results))

    async def test_removed_client_cannot_submit_owned_microphone_frames(self):
        connection = self.connections[0]
        listener_id = await self.listen(connection)
        await self.manager.start(connection, {"id": 2, "config_entry_id": "one", "listener_id": listener_id})
        capture_id = connection.results[-1]["capture_id"]
        self.manager.hass.data["doorfast"].pop("one")
        await self.manager.submit(connection, {
            "id": 3, "config_entry_id": "one", "capture_id": capture_id,
            "pcm": base64.b64encode(PCM).decode("ascii"),
        })
        self.assertEqual([], self.http.pcm_summaries, "removed entry must never submit microphone data")
        self.assertEqual(["not_found"], [error.code for error in connection.errors])


if __name__ == "__main__":
    unittest.main()
