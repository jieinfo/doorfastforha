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

# Both unittest discovery and qualified module selections share this fixture.
# Loading it twice would replace the synthetic package's exception classes.
sys.modules.setdefault("test_audio_contract", sys.modules[__name__])
sys.modules.setdefault("tests.test_audio_contract", sys.modules[__name__])


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
monitor_module = load("monitor")
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

    def call_coordinator(self):
        coordinator = monitor_module.MonitorCoordinator(self.client, RUNTIME, "gate_main")
        coordinator.set_call_audio_release(
            lambda runtime, station, generation: self.manager.release_call("one", runtime, station, generation))
        request = self.http.request
        self.control_requests = []

        def control_request(method, url, **kwargs):
            if url.endswith(("/api/v1/call", "/api/v1/hangup")):
                self.control_requests.append((url.rsplit("/", 1)[-1], kwargs["json"]))
                return Response(payload={"runtime_id": RUNTIME, "station_id": "gate_main",
                    "operation_id": 3, "generation": 9, "queued": True,
                    "state": "ending" if url.endswith("hangup") else "preparing"})
            return request(method, url, **kwargs)

        self.http.request = control_request
        return coordinator

    async def test_hangup_releases_audio_self_refreshed_before_coordinator_poll(self):
        self.client.status = {"runtime_id": RUNTIME, "call": {"session": "idle", "generation": 0}}
        coordinator = self.call_coordinator()
        await coordinator.async_call()
        self.http.current_status = outbound_status()
        connection = self.connections[0]
        listener_id = await self.listen(connection)
        await self.manager.start(connection, {"id": 3, "config_entry_id": "one", "listener_id": listener_id})
        self.assertEqual("active", connection.results[-1]["state"])
        self.assertEqual(0, coordinator._native_generation)
        self.assertEqual(7, self.client.status["call"]["generation"])
        await coordinator.async_hangup()
        self.assertEqual(("hangup", {"runtime_id": RUNTIME, "station_id": "gate_main",
            "operation_id": 3, "generation": 9}), self.control_requests[-1])
        self.assertEqual("ending", coordinator.call_state)
        self.assert_released()
        self.assertEqual(["session", "end"], [url.rsplit("/", 1)[-1] for url, _ in self.http.producer_requests])
        self.assertEqual("talking", self.client.status["call"]["session"])

    async def test_hangup_revokes_initial_refresh_and_open_even_if_same_identity_returns(self):
        for boundary in ("refresh", "open"):
            for explicit in (False, True):
                with self.subTest(boundary=boundary, explicit=explicit):
                    self.http.request = HttpBoundary.request.__get__(self.http)
                    self.http.producer_requests.clear()
                    self.client.status = self.http.current_status = outbound_status()
                    coordinator = self.call_coordinator()
                    await coordinator.async_apply_call_status(self.client.status)
                    entered, resume = asyncio.Event(), asyncio.Event()
                    request = self.http.request

                    class GatedResponse(Response):
                        async def __aenter__(response):
                            entered.set()
                            await resume.wait()
                            return response

                    def gated_request(method, url, **kwargs):
                        response = request(method, url, **kwargs)
                        if url.endswith("/status" if boundary == "refresh" else "/audio/session"):
                            return GatedResponse(payload=response.payload)
                        return response

                    self.http.request = gated_request
                    connection = Connection()
                    msg = {"id": 1, "config_entry_id": "one"}
                    if explicit:
                        msg.update(station_id="gate_main", generation=7)
                    opening = asyncio.create_task(self.manager.start(connection, msg))
                    try:
                        await entered.wait()
                        pending = next(iter(self.manager._pending_cleanups.values()))
                        if boundary == "refresh":
                            self.assertIsNone(pending.producer.call_identity)
                        await coordinator.async_hangup()
                        # This remains real cached TALKING. No fake terminal native
                        # state may be used to prevent this reservation's revival.
                        self.assertEqual("talking", self.client.status["call"]["session"])
                        if explicit or boundary == "open":
                            self.assertEqual({}, self.manager._starting)
                            self.assertEqual({}, self.manager._pending_cleanups)
                        else:
                            # An implicit request may select a different fresh
                            # owner. Its one exact marker is checked at publication.
                            self.assertEqual(1, len(self.manager._starting))
                            self.assertEqual(1, len(self.manager._pending_cleanups))
                        self.client.status = self.http.current_status = outbound_status()
                        resume.set()
                        await opening
                        self.assertEqual([], connection.results)
                        self.assertEqual(["not_found"], [error.code for error in connection.errors])
                        self.assert_released()
                        self.assertEqual(["session", "end"], [
                            url.rsplit("/", 1)[-1] for url, _ in self.http.producer_requests])
                    finally:
                        resume.set()
                        await opening
                        self.http.request = request
                        await self.manager.release_entry("one")

    async def test_exact_release_preserves_new_owner_chosen_after_cached_candidate(self):
        for changed in ("runtime", "station", "control"):
            with self.subTest(changed=changed):
                self.client.status = outbound_status()
                sample = status(REBOOT_RUNTIME if changed == "runtime" else RUNTIME)
                if changed == "station":
                    sample["call"]["station_id"] = "gate_side"
                if changed == "control":
                    sample["call"]["generation"] = sample["audio_tx"]["generation"] = 8
                self.http.current_status = sample
                entered, resume = asyncio.Event(), asyncio.Event()
                request = self.http.request

                class GatedOpen(Response):
                    async def __aenter__(response):
                        entered.set()
                        await resume.wait()
                        return response

                def gated_request(method, url, **kwargs):
                    response = request(method, url, **kwargs)
                    return GatedOpen(payload=response.payload) if url.endswith("/audio/session") else response

                self.http.request = gated_request
                connection = Connection()
                opening = asyncio.create_task(self.manager.start(connection, {"id": 1, "config_entry_id": "one"}))
                try:
                    await entered.wait()
                    pending = next(iter(self.manager._pending_cleanups.values()))
                    owner = (sample["runtime_id"], sample["call"]["station_id"], sample["call"]["generation"])
                    self.assertEqual(owner, pending.producer.call_identity)
                    await self.manager.release_call("one", RUNTIME, "gate_main", 7)
                    self.assertEqual(1, len(self.manager._starting))
                    self.assertEqual(1, len(self.manager._pending_cleanups))
                    resume.set()
                    await opening
                    self.assertEqual([], connection.errors)
                    capture = self.manager._by_entry["one"]
                    self.assertEqual(owner, capture.producer.call_identity)
                    self.assertEqual("active", connection.results[-1]["state"])
                    self.assertEqual(["session"], [url.rsplit("/", 1)[-1] for url, _ in self.http.producer_requests])
                finally:
                    resume.set()
                    await opening
                    self.http.request = request
                    await self.manager.release_entry("one")
                    self.http.producer_requests.clear()

    async def test_exact_release_preserves_unrelated_request_during_initial_refresh(self):
        for changed in ("runtime", "station", "control"):
            with self.subTest(changed=changed):
                sample = status(REBOOT_RUNTIME if changed == "runtime" else RUNTIME)
                if changed == "station":
                    sample["call"]["station_id"] = "gate_side"
                if changed == "control":
                    sample["call"]["generation"] = sample["audio_tx"]["generation"] = 8
                # Explicit request identity takes precedence over cached native
                # station/generation, even before its first refresh completes.
                self.client.status = status(sample["runtime_id"])
                self.http.current_status = sample
                entered, resume = asyncio.Event(), asyncio.Event()
                request = self.http.request

                class GatedRefresh(Response):
                    async def __aenter__(response):
                        entered.set()
                        await resume.wait()
                        return response

                def gated_request(method, url, **kwargs):
                    response = request(method, url, **kwargs)
                    return GatedRefresh(payload=response.payload) if url.endswith("/status") else response

                self.http.request = gated_request
                connection = Connection()
                opening = asyncio.create_task(self.manager.start(connection, {"id": 1, "config_entry_id": "one",
                    "station_id": sample["call"]["station_id"], "generation": sample["call"]["generation"]}))
                try:
                    await entered.wait()
                    pending = next(iter(self.manager._pending_cleanups.values()))
                    self.assertIsNone(pending.producer.call_identity)
                    await self.manager.release_call("one", RUNTIME, "gate_main", 7)
                    self.assertEqual(1, len(self.manager._starting))
                    self.assertEqual(1, len(self.manager._pending_cleanups))
                    resume.set()
                    await opening
                    self.assertEqual([], connection.errors)
                    self.assertEqual((sample["runtime_id"], sample["call"]["station_id"], sample["call"]["generation"]),
                        self.manager._by_entry["one"].producer.call_identity)
                    self.assertEqual(["session"], [url.rsplit("/", 1)[-1] for url, _ in self.http.producer_requests])
                finally:
                    resume.set()
                    await opening
                    self.http.request = request
                    await self.manager.release_entry("one")
                    self.http.producer_requests.clear()

    async def test_delayed_accepted_hangup_preserves_new_incoming_audio_owner(self):
        for changed in ("station", "control"):
            with self.subTest(changed=changed):
                self.http.request = HttpBoundary.request.__get__(self.http)
                self.client.status = self.http.current_status = outbound_status()
                coordinator = self.call_coordinator()
                await coordinator.async_apply_call_status(self.client.status)
                entered, resume = asyncio.Event(), asyncio.Event()
                request = self.http.request

                class GatedHangup(Response):
                    async def __aenter__(response):
                        entered.set()
                        await resume.wait()
                        return response

                def gated_request(method, url, **kwargs):
                    response = request(method, url, **kwargs)
                    return GatedHangup(payload=response.payload) if url.endswith("/hangup") else response

                self.http.request = gated_request
                hanging = asyncio.create_task(coordinator.async_hangup())
                try:
                    await entered.wait()
                    sample = status()
                    if changed == "station":
                        sample["call"]["station_id"] = "gate_side"
                    else:
                        sample["call"]["generation"] = sample["audio_tx"]["generation"] = 8
                    self.client.status = self.http.current_status = sample
                    connection = Connection()
                    await self.manager.listen_start(connection, {"id": 1, "config_entry_id": "one",
                        "station_id": sample["call"]["station_id"], "generation": sample["call"]["generation"]})
                    listener_id = connection.results[-1]["listener_id"]
                    await self.manager.start(connection, {"id": 2, "config_entry_id": "one", "listener_id": listener_id})
                    capture = self.manager._by_entry["one"]
                    hub = self.manager._hubs["one"]
                    resume.set()
                    await hanging
                    self.assertEqual("ending", coordinator.call_state)
                    self.assertEqual([], connection.errors)
                    self.assertIs(hub, self.manager._hubs["one"])
                    self.assertIn(listener_id, self.manager._listeners)
                    self.assertIs(capture, self.manager._by_entry["one"])
                    self.assertEqual("active", capture.producer.state.value)
                    self.assertEqual(["session"], [url.rsplit("/", 1)[-1] for url, _ in self.http.producer_requests])
                finally:
                    resume.set()
                    await hanging
                    self.http.request = request
                    await self.manager.release_entry("one")
                    self.http.producer_requests.clear()

    async def test_rejected_http_hangup_preserves_existing_listener_and_microphone(self):
        self.client.status = self.http.current_status = outbound_status()
        coordinator = self.call_coordinator()
        await coordinator.async_apply_call_status(self.client.status)
        connection = self.connections[0]
        listener_id = await self.listen(connection)
        await self.manager.start(connection, {"id": 2, "config_entry_id": "one", "listener_id": listener_id})
        request = self.http.request

        def rejecting_request(method, url, **kwargs):
            if url.endswith("/hangup"):
                return Response(code=409, payload={"error": {"code": "operation_mismatch", "message": "synthetic"}})
            return request(method, url, **kwargs)

        self.http.request = rejecting_request
        with self.assertRaises(client_module.DoorfastTransportError):
            await coordinator.async_hangup()
        self.assertEqual("talking", coordinator.call_state)
        self.assertIn(listener_id, self.manager._listeners)
        self.assertEqual("active", self.manager._by_entry["one"].producer.state.value)
        self.assertEqual(["session"], [url.rsplit("/", 1)[-1] for url, _ in self.http.producer_requests])

    async def test_cached_idle_start_without_generation_cannot_publish_after_exact_hangup(self):
        self.client.status = {"runtime_id": RUNTIME, "call": {"session": "idle", "generation": 0}}
        coordinator = self.call_coordinator()
        await coordinator.async_call()
        self.http.current_status = outbound_status()
        entered, resume = asyncio.Event(), asyncio.Event()
        request = self.http.request
        gated = False

        class GatedRefresh(Response):
            async def __aenter__(response):
                entered.set()
                await resume.wait()
                return response

        def gated_request(method, url, **kwargs):
            nonlocal gated
            response = request(method, url, **kwargs)
            if url.endswith("/status") and not gated:
                gated = True
                return GatedRefresh(payload=response.payload)
            return response

        self.http.request = gated_request
        connection = Connection()
        opening = asyncio.create_task(self.manager.start(connection, {
            "id": 1, "config_entry_id": "one", "station_id": "gate_main"}))
        try:
            await entered.wait()
            # An independent real listener learns TALKING before the coordinator.
            await self.listen(self.connections[0])
            self.assertEqual(0, coordinator._native_generation)
            await coordinator.async_hangup()
            # Repeating this exact release and restoring the same native sample
            # cannot revive the reservation once its fresh identity arrives.
            await self.manager.release_call("one", RUNTIME, "gate_main", 7)
            await self.client.refresh()
            resume.set()
            await opening
            self.assertEqual([], connection.results)
            self.assertEqual(["not_found"], [error.code for error in connection.errors])
            self.assert_released()
            self.assertEqual(["session", "end"], [url.rsplit("/", 1)[-1] for url, _ in self.http.producer_requests])
            self.assertEqual("talking", self.client.status["call"]["session"])
        finally:
            resume.set()
            await opening

    async def test_second_related_release_revokes_only_ambiguous_pending_request(self):
        self.client.status = {"runtime_id": RUNTIME, "call": {"session": "idle", "generation": 0}}
        self.http.current_status = outbound_status()
        entered, resume = asyncio.Event(), asyncio.Event()
        request = self.http.request
        gated = False

        class GatedRefresh(Response):
            async def __aenter__(response):
                entered.set()
                await resume.wait()
                return response

        def gated_request(method, url, **kwargs):
            nonlocal gated
            response = request(method, url, **kwargs)
            if url.endswith("/status") and not gated:
                gated = True
                return GatedRefresh(payload=response.payload)
            return response

        self.http.request = gated_request
        connection = Connection()
        opening = asyncio.create_task(self.manager.start(connection, {
            "id": 1, "config_entry_id": "one", "station_id": "gate_main"}))
        try:
            await entered.wait()
            sample = status()
            sample["call"].update(station_id="gate_side", generation=8)
            sample["audio_tx"]["generation"] = 8
            self.http.current_status = sample
            listener = self.connections[0]
            await self.manager.listen_start(listener, {"id": 2, "config_entry_id": "one",
                "station_id": "gate_side", "generation": 8})
            listener_id = listener.results[-1]["listener_id"]
            hub = self.manager._hubs["one"]
            await self.manager.release_call("one", RUNTIME, "gate_main", 7)
            await self.manager.release_call("one", RUNTIME, "gate_main", 7)
            await self.manager.release_call("one", REBOOT_RUNTIME, "gate_main", 8)
            await self.manager.release_call("one", RUNTIME, "gate_other", 8)
            self.assertEqual(1, len(self.manager._starting))
            self.assertEqual(1, len(self.manager._pending_cleanups))
            await self.manager.release_call("one", RUNTIME, "gate_main", 8)
            self.assertEqual({}, self.manager._starting)
            self.assertEqual({}, self.manager._pending_cleanups)
            self.assertIs(hub, self.manager._hubs["one"])
            self.assertIn(listener_id, self.manager._listeners)
            resume.set()
            await opening
            self.assertEqual([], connection.results)
            self.assertEqual(["not_found"], [error.code for error in connection.errors])
            self.assertIs(hub, self.manager._hubs["one"])
            self.assertEqual(["session", "end"], [url.rsplit("/", 1)[-1] for url, _ in self.http.producer_requests])
        finally:
            resume.set()
            await opening

    async def test_one_exact_pending_marker_preserves_a_different_fresh_owner(self):
        for changed in ("runtime", "station", "control"):
            with self.subTest(changed=changed):
                self.client.status = {"runtime_id": RUNTIME, "call": {"session": "idle", "generation": 0}}
                sample = status(REBOOT_RUNTIME if changed == "runtime" else RUNTIME)
                if changed == "station":
                    sample["call"]["station_id"] = "gate_side"
                if changed == "control":
                    sample["call"]["generation"] = sample["audio_tx"]["generation"] = 8
                self.http.current_status = sample
                entered, resume = asyncio.Event(), asyncio.Event()
                request = self.http.request

                class GatedRefresh(Response):
                    async def __aenter__(response):
                        entered.set()
                        await resume.wait()
                        return response

                def gated_request(method, url, **kwargs):
                    response = request(method, url, **kwargs)
                    return GatedRefresh(payload=response.payload) if url.endswith("/status") else response

                self.http.request = gated_request
                connection = Connection()
                opening = asyncio.create_task(self.manager.start(connection, {"id": 1, "config_entry_id": "one"}))
                try:
                    await entered.wait()
                    pending = next(iter(self.manager._pending_cleanups.values()))
                    await self.manager.release_call("one", RUNTIME, "gate_main", 7)
                    await self.manager.release_call("one", RUNTIME, "gate_main", 7)
                    self.assertEqual(ws.AudioIdentity(RUNTIME, "gate_main", 7), pending.revoked_identity)
                    self.assertEqual(1, len(self.manager._starting))
                    resume.set()
                    await opening
                    self.assertEqual([], connection.errors)
                    capture = self.manager._by_entry["one"]
                    self.assertEqual((sample["runtime_id"], sample["call"]["station_id"], sample["call"]["generation"]),
                        capture.producer.call_identity)
                    await self.manager.release_call("one", RUNTIME, "gate_main", 7)
                    self.assertIs(capture, self.manager._by_entry["one"])
                    self.assertEqual("active", capture.producer.state.value)
                    self.assertEqual({}, self.manager._pending_cleanups)
                    self.assertEqual(["session"], [url.rsplit("/", 1)[-1] for url, _ in self.http.producer_requests])
                finally:
                    resume.set()
                    await opening
                    self.http.request = request
                    await self.manager.release_entry("one")
                    self.http.producer_requests.clear()

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

    async def test_operation_conflict_during_real_open_cannot_publish_microphone(self):
        for conflict in ("malformed", "control", "media"):
            for reconcile in (False, True):
                with self.subTest(conflict=conflict, reconcile=reconcile):
                    initial = outbound_status()
                    self.http.current_status = initial
                    self.client.status = initial
                    entered, resume = asyncio.Event(), asyncio.Event()
                    request = self.http.request

                    class GatedOpen(Response):
                        async def __aenter__(response):
                            entered.set()
                            await resume.wait()
                            return response

                    def gated_request(method, url, **kwargs):
                        response = request(method, url, **kwargs)
                        if url.endswith("/audio/session"):
                            return GatedOpen(payload=response.payload)
                        return response

                    self.http.request = gated_request
                    connection = Connection()
                    opening = asyncio.create_task(self.manager.start(connection, {
                        "id": 1, "config_entry_id": "one", "station_id": "gate_main", "generation": 7}))
                    try:
                        await entered.wait()
                        sample = outbound_status()
                        if conflict == "malformed":
                            sample["station_call"] = {}
                        else:
                            sample["station_call"]["call_generation" if conflict == "control" else "media_generation"] += 1
                        self.http.current_status = sample
                        self.client.status = sample
                        if reconcile:
                            await self.manager.reconcile("one", sample)
                            self.assertEqual({}, self.manager._starting)
                            self.assertEqual({}, self.manager._pending_cleanups)
                            # A revoked start cannot revive if another poll
                            # restores admission before the old open completes.
                            self.client.status = initial
                            self.http.current_status = initial
                        resume.set()
                        await opening
                        self.assertEqual([], connection.results)
                        self.assertTrue(connection.errors)
                        self.assert_released()
                        self.assertEqual(["session", "end"], [
                            url.rsplit("/", 1)[-1] for url, _ in self.http.producer_requests])
                    finally:
                        resume.set()
                        await opening
                        self.http.request = request
                        await self.manager.release_entry("one")
                        self.http.producer_requests.clear()

    async def test_direct_microphone_reconcile_revokes_observed_operation_conflict(self):
        for conflict in ("malformed", "control", "media"):
            with self.subTest(conflict=conflict):
                initial = outbound_status()
                self.http.current_status = initial
                self.client.status = initial
                connection = Connection()
                await self.manager.start(connection, {
                    "id": 1, "config_entry_id": "one", "station_id": "gate_main", "generation": 7})
                self.assertEqual("active", connection.results[-1]["state"])
                capture = self.manager._by_entry["one"]
                sample = outbound_status()
                if conflict == "malformed":
                    sample["station_call"] = {}
                else:
                    sample["station_call"]["call_generation" if conflict == "control" else "media_generation"] += 1
                try:
                    self.client.status = sample
                    await self.manager.reconcile("one", sample)
                    self.assert_released()
                    self.assertEqual("idle", capture.producer.state.value)
                    self.assertEqual(["session", "end"], [
                        url.rsplit("/", 1)[-1] for url, _ in self.http.producer_requests])
                finally:
                    await self.manager.release_entry("one")
                    self.http.producer_requests.clear()

    def test_audio_identity_keeps_control_and_media_namespaces_distinct(self):
        identity_type = sys.modules[f"{PACKAGE}.client_types"].AudioIdentity
        self.assertEqual(identity_type(RUNTIME, "gate_main", 7),
                         identity_type.from_status(outbound_status(), "gate_main", 7))
        for field, value in (("call_generation", 9), ("media_generation", 7),
                             ("operation_id", False), ("stage", "queued")):
            sample = outbound_status()
            sample["station_call"][field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(AudioStreamError, "audio_identity_mismatch"):
                    identity_type.from_status(sample, "gate_main", 7)
        incoming = outbound_status()
        incoming["station_call"].update(stage="ended", call_generation=6,
            media_generation=8, timer="none", remaining_ms=0)
        incoming.pop("media")
        self.assertEqual(identity_type(RUNTIME, "gate_main", 7),
                         identity_type.from_status(incoming, "gate_main", 7))

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
