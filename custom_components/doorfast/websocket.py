"""Authenticated WebSocket ownership for shared listening and microphone PCM."""
from __future__ import annotations

import asyncio
import base64
import binascii
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable

from .const import DOMAIN
from .audio import AudioListenerHub
from .client_types import AudioIdentity, AudioStreamError
from .pcm import PcmProducer, PcmProducerError, PcmProducerState

_FRAME_BYTES = 320
_MAX_FRAMES = 5
_MAX_PCM_BYTES = _FRAME_BYTES * _MAX_FRAMES
_MAX_PCM_B64_CHARS = 4 * ((_MAX_PCM_BYTES + 2) // 3)
_JS_MAX = 9007199254740991


def _safe_int(value: Any, minimum: int = 0) -> bool:
    return type(value) is int and minimum <= value <= _JS_MAX

try:  # Imported only when running inside Home Assistant.
    import voluptuous as vol
    from homeassistant.components.websocket_api import (
        async_register_command,
        async_response,
        websocket_command,
    )
except ImportError:  # pragma: no cover - pure unit tests provide these symbols
    vol = None
    async_register_command = None
    def async_response(func):
        return func
    def websocket_command(schema):
        def decorate(func):
            func._ws_schema = schema
            return func
        return decorate


@dataclass
class _Capture:
    entry_id: str
    connection: Any
    producer: PcmProducer
    capture_id: str
    station_id: str | None = None
    listener_id: str | None = None
    runtime_id: str | None = None
    cleanup: Callable[[], None] = field(default=lambda: None)


@dataclass
class _Listener:
    entry_id: str
    connection: Any
    listener_id: str
    identity: AudioIdentity
    hub: AudioListenerHub
    lease_timer: Any = None
    reading: bool = False
    cleanup: Callable[[], None] = field(default=lambda: None)


class PcmWebSocketManager:
    """Bind listeners and one private producer to authenticated connections."""

    def __init__(self, hass: Any, producer_factory: Callable[[Any], PcmProducer] = PcmProducer):
        self.hass = hass
        self.producer_factory = producer_factory
        self._captures: dict[str, _Capture] = {}
        self._by_entry: dict[str, _Capture] = {}
        self._starting: dict[str, object] = {}
        self._listeners: dict[str, _Listener] = {}
        self._hubs: dict[str, AudioListenerHub] = {}
        self._epochs: dict[str, int] = {}
        self._pending_cleanups: dict[str, tuple[str, str | None, Callable[[], None]]] = {}
        self._registered = False

    def register(self) -> None:
        if self._registered or async_register_command is None:
            return
        for handler in (_start, _submit, _stop, _listen_start, _listen_read, _listen_stop):
            async_register_command(self.hass, handler)
        self._registered = True

    def _client(self, entry_id: object) -> Any:
        if not isinstance(entry_id, str) or not entry_id:
            raise ValueError("config_entry_id is required")
        client = self.hass.data.get(DOMAIN, {}).get(entry_id)
        if client is None:
            raise ValueError("unknown config entry")
        return client

    @staticmethod
    def _is_admin(connection: Any) -> bool:
        user = getattr(connection, "user", None)
        return bool(getattr(user, "is_admin", False))

    def _send_error(self, connection: Any, msg: dict[str, Any], code: str, message: str) -> None:
        connection.send_error(msg.get("id"), code, message)

    def _schedule_stop(self, producers: list[PcmProducer]) -> asyncio.Task | None:
        if not producers:
            return
        task = self.hass.async_create_task(self._stop_producers(producers))
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return task

    def _attach_cleanup(self, connection: Any, key: str, callback: Callable[[], Any]) -> Callable[[], None]:
        subscriptions = getattr(connection, "subscriptions", None)
        if isinstance(subscriptions, dict):
            subscription_key = ("doorfast_pcm", key)
            subscriptions[subscription_key] = callback
            return lambda: subscriptions.pop(subscription_key, None)
        async def async_callback() -> None:
            task = callback()
            if task is not None:
                await task
        for name in ("async_add_cleanup_callback", "async_on_remove"):
            method = getattr(connection, name, None)
            if method is not None:
                remover = method(async_callback)
                return remover if callable(remover) else lambda: None
        return lambda: None

    def _attach_pending(self, entry_id: str, connection: Any,
                        callback: Callable[[], None], listener_id: str | None = None) -> Callable[[], None]:
        key = secrets.token_urlsafe(24)
        def revoke() -> None:
            self._pending_cleanups.pop(key, None)
            cleanup()
            callback()
        cleanup = self._attach_cleanup(connection, key, revoke)
        self._pending_cleanups[key] = (entry_id, listener_id, revoke)
        def detach() -> None:
            self._pending_cleanups.pop(key, None)
            cleanup()
        return detach

    def _revoke_pending(self, entry_id: str, listener_id: str | None = None) -> None:
        for pending_entry, pending_listener, revoke in list(self._pending_cleanups.values()):
            if pending_entry == entry_id and (listener_id is None or pending_listener == listener_id):
                revoke()

    def _owned_listener(self, connection: Any, msg: dict[str, Any]) -> _Listener:
        listener_id = msg.get("listener_id")
        listener = self._listeners.get(listener_id) if isinstance(listener_id, str) else None
        if (listener is None or listener.connection is not connection
                or listener.entry_id != msg.get("config_entry_id")):
            raise AudioStreamError("not_found")
        return listener

    def _renew_lease(self, listener: _Listener) -> None:
        if listener.lease_timer is not None:
            listener.lease_timer.cancel()
        listener.lease_timer = asyncio.get_running_loop().call_later(
            5.0, self._expire_listener, listener.listener_id)

    def _expire_listener(self, listener_id: str) -> None:
        self._schedule_stop(self._invalidate_listener(listener_id))

    def _invalidate_capture(self, capture_id: str) -> list[PcmProducer]:
        capture = self._captures.pop(capture_id, None)
        if capture is None:
            return []
        if self._by_entry.get(capture.entry_id) is capture:
            self._by_entry.pop(capture.entry_id, None)
        capture.cleanup()
        return [capture.producer]

    def _invalidate_listener(self, listener_id: str) -> list[PcmProducer]:
        listener = self._listeners.pop(listener_id, None)
        if listener is None:
            return []
        listener.lease_timer.cancel()
        listener.cleanup()
        self._revoke_pending(listener.entry_id, listener_id)
        listener.hub.remove(listener_id)
        if listener.hub.closed and self._hubs.get(listener.entry_id) is listener.hub:
            self._hubs.pop(listener.entry_id, None)
        capture = self._by_entry.get(listener.entry_id)
        if capture is not None and capture.listener_id == listener_id:
            return self._invalidate_capture(capture.capture_id)
        return []

    def _invalidate_hub(self, entry_id: str, hub: AudioListenerHub) -> list[PcmProducer]:
        hub.close()
        producers = []
        for listener in list(self._listeners.values()):
            if listener.entry_id == entry_id and listener.hub is hub:
                producers.extend(self._invalidate_listener(listener.listener_id))
        if self._hubs.get(entry_id) is hub:
            self._hubs.pop(entry_id, None)
        return producers

    @staticmethod
    async def _stop_producers(producers: list[PcmProducer]) -> None:
        for producer in producers:
            try:
                await producer.stop()
            except Exception:
                pass

    async def listen_start(self, connection: Any, msg: dict[str, Any]) -> None:
        if not self._is_admin(connection):
            self._send_error(connection, msg, "unauthorized", "Administrator permission required")
            return
        pending = [True]
        cleanup = lambda: None
        try:
            entry_id = msg.get("config_entry_id")
            client = self._client(entry_id)
            station_id = msg.get("station_id")
            generation = msg.get("generation")
            if not isinstance(station_id, str) or not station_id or (
                generation is not None and not _safe_int(generation, 1)
            ):
                raise ValueError("Invalid listener identity")
            epoch = self._epochs.get(entry_id, 0)
            cleanup = self._attach_pending(entry_id, connection, lambda: pending.__setitem__(0, False))
            status = await client.refresh()
            if not pending[0] or self._epochs.get(entry_id, 0) != epoch:
                raise AudioStreamError("not_found")
            audio = status.get("audio") if isinstance(status, dict) else None
            if not isinstance(audio, dict) or type(audio.get("pcm_stream_version")) is not int or audio.get("pcm_stream_version") != 1:
                raise AudioStreamError("unsupported_audio_stream")
            identity = AudioIdentity.from_status(status, station_id, expected_generation=generation)
            if not _safe_int(identity.generation, 1):
                raise ValueError("Invalid generation")
            hub = self._hubs.get(entry_id)
            producers = []
            if hub is not None and (hub.closed or hub.identity != identity):
                producers = self._invalidate_hub(entry_id, hub)
                hub = None
            if hub is None:
                hub = AudioListenerHub(client, identity, asyncio.get_running_loop().time)
                self._hubs[entry_id] = hub
            listener_id = secrets.token_urlsafe(24)
            hub.add(listener_id)
            listener = _Listener(entry_id, connection, listener_id, identity, hub)
            self._listeners[listener_id] = listener
            listener.cleanup = self._attach_cleanup(connection, listener_id,
                lambda: self._schedule_stop(self._invalidate_listener(listener_id)))
            self._renew_lease(listener)
            cleanup()
            try:
                connection.send_result(msg["id"], {"listener_id": listener_id,
                    "station_id": identity.station_id, "generation": identity.generation,
                    "sample_rate": 8000, "channels": 1, "sample_format": "s16le"})
            except BaseException:
                producers.extend(self._invalidate_listener(listener_id))
                self._schedule_stop(producers)
                raise
            await self._stop_producers(producers)
        except (ValueError, AudioStreamError) as err:
            code = getattr(err, "code", "invalid_request")
            self._send_error(connection, msg, code, code)
        except Exception:
            self._send_error(connection, msg, "unknown_error", "Unable to start audio listener")
        finally:
            cleanup()

    async def listen_read(self, connection: Any, msg: dict[str, Any]) -> None:
        listener = None
        acquired = False
        try:
            listener = self._owned_listener(connection, msg)
            if listener.reading:
                raise AudioStreamError("audio_read_busy")
            listener.reading = True
            acquired = True
            self._renew_lease(listener)
            reply = await listener.hub.read(listener.listener_id)
            if self._listeners.get(listener.listener_id) is not listener:
                raise AudioStreamError("not_found")
            result = {"state": "empty"}
            if reply.state == "chunk":
                chunk = reply.chunk
                if (chunk.identity != listener.identity or not _safe_int(chunk.identity.generation, 1)
                    or not _safe_int(chunk.previous_revision) or not _safe_int(chunk.revision)
                    or not isinstance(chunk.pcm, bytes) or not 0 < len(chunk.pcm) <= 3200 or len(chunk.pcm) % 2
                    or type(chunk.discontinuity) is not bool):
                    raise AudioStreamError("audio_invalid_response")
                result = {"state": "chunk", "listener_id": listener.listener_id,
                    "generation": listener.identity.generation,
                    "previous_revision": chunk.previous_revision, "revision": chunk.revision,
                    "pcm": base64.b64encode(chunk.pcm).decode("ascii"),
                    "discontinuity": chunk.discontinuity}
            self._renew_lease(listener)
            connection.send_result(msg["id"], result)
        except AudioStreamError as err:
            if listener is not None and (listener.hub.closed or err.code == "audio_invalid_response"):
                producers = self._invalidate_hub(listener.entry_id, listener.hub)
                await self._stop_producers(producers)
            code = "not_found" if err.code in {"audio_listener_not_found", "audio_listener_closed"} else err.code
            self._send_error(connection, msg, code, code)
        except BaseException:
            if listener is not None:
                await self._stop_producers(self._invalidate_listener(listener.listener_id))
            raise
        finally:
            if listener is not None and acquired:
                listener.reading = False

    async def listen_stop(self, connection: Any, msg: dict[str, Any]) -> None:
        try:
            listener = self._owned_listener(connection, msg)
        except AudioStreamError as err:
            self._send_error(connection, msg, err.code, err.code)
            return
        await self._stop_producers(self._invalidate_listener(listener.listener_id))
        connection.send_result(msg["id"], {"listener_id": listener.listener_id, "state": "idle"})

    async def start(self, connection: Any, msg: dict[str, Any]) -> None:
        if not self._is_admin(connection):
            self._send_error(connection, msg, "unauthorized", "Administrator permission required")
            return
        reservation = object()
        cleanup = lambda: None
        producer = None
        published = False
        try:
            entry_id = msg.get("config_entry_id")
            client = self._client(entry_id)
            listener = self._owned_listener(connection, msg) if "listener_id" in msg else None
            if entry_id in self._by_entry or entry_id in self._starting:
                raise PcmProducerError("producer_busy")
            self._starting[entry_id] = reservation
            def cancel_start() -> None:
                if self._starting.get(entry_id) is reservation:
                    self._starting.pop(entry_id, None)
            cleanup = self._attach_pending(entry_id, connection, cancel_start,
                                           listener.listener_id if listener else None)
            station_id = msg.get("station_id")
            generation = msg.get("generation")
            if station_id is not None and (not isinstance(station_id, str) or not station_id):
                raise ValueError("station_id must be non-empty text")
            if generation is not None and (
                isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0
            ):
                raise ValueError("generation must be a positive integer")
            if listener is not None:
                if ((station_id is not None and station_id != listener.identity.station_id)
                    or (generation is not None and (not _safe_int(generation, 1) or generation != listener.identity.generation))
                    or listener.hub.closed):
                    raise PcmProducerError("identity_mismatch")
                station_id = listener.identity.station_id
                producer = self.producer_factory(client, station_id, listener.identity.generation,
                    expected_runtime_id=listener.identity.runtime_id)
            else:
                try:
                    producer = self.producer_factory(client, station_id, generation)
                except TypeError:
                    # Legacy factories remain supported only for unbound PTT.
                    producer = self.producer_factory(client)
            try:
                await producer.start()
                if (self._starting.get(entry_id) is not reservation or
                    (listener is not None and (self._listeners.get(listener.listener_id) is not listener
                        or listener.hub.closed or listener.connection is not connection))):
                    raise PcmProducerError("not_found")
                capture_id = secrets.token_urlsafe(24)
                capture = _Capture(entry_id, connection, producer, capture_id, station_id,
                    listener.listener_id if listener else None,
                    listener.identity.runtime_id if listener else None)
                self._captures[capture_id] = capture
                self._by_entry[entry_id] = capture
                capture.cleanup = self._attach_cleanup(connection, capture_id,
                    lambda: self._schedule_stop(self._invalidate_capture(capture_id)))
                published = True
                try:
                    connection.send_result(msg["id"], {
                        "capture_id": capture_id,
                        "state": producer.state.value,
                        "sequence": producer.sequence,
                    })
                except BaseException:
                    await self.release(capture_id)
                    raise
            finally:
                if self._starting.get(entry_id) is reservation:
                    self._starting.pop(entry_id, None)
        except (ValueError, PcmProducerError, AudioStreamError) as err:
            code = getattr(err, "code", "invalid_request")
            self._send_error(connection, msg, code, code)
        except Exception:
            self._send_error(connection, msg, "unknown_error", "Unable to start audio capture")
        finally:
            cleanup()
            if isinstance(entry_id, str) and self._starting.get(entry_id) is reservation:
                self._starting.pop(entry_id, None)
            # Open may finish after a stop/disconnect/reconcile revoked ownership.
            if producer is not None and not published:
                await self._stop_producers([producer])

    async def submit(self, connection: Any, msg: dict[str, Any]) -> None:
        capture = self._captures.get(msg.get("capture_id"))
        if (
            capture is None
            or capture.connection is not connection
            or msg.get("config_entry_id") != capture.entry_id
        ):
            self._send_error(connection, msg, "not_found", "Unknown capture")
            return
        if (
            ("station_id" in msg and msg.get("station_id") != capture.station_id)
            or ("generation" in msg and msg.get("generation") != capture.producer.generation)
        ):
            self._send_error(connection, msg, "identity_mismatch", "Capture identity does not match active call")
            return
        encoded = msg.get("pcm")
        if not isinstance(encoded, str):
            self._send_error(connection, msg, "invalid_format", "pcm must be base64 text")
            return
        if len(encoded) > _MAX_PCM_B64_CHARS:
            self._send_error(connection, msg, "invalid_format", "pcm exceeds five frames")
            return
        try:
            body = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            self._send_error(connection, msg, "invalid_format", "pcm is not valid base64")
            return
        if not body or len(body) % _FRAME_BYTES or not _FRAME_BYTES <= len(body) <= _MAX_PCM_BYTES:
            self._send_error(connection, msg, "invalid_format", "pcm must contain one to five 320-byte frames")
            return
        try:
            outcome = await capture.producer.submit([
                body[offset:offset + _FRAME_BYTES]
                for offset in range(0, len(body), _FRAME_BYTES)
            ])
            connection.send_result(msg["id"], {
                "capture_id": capture.capture_id,
                "accepted_frames": outcome.accepted_frames,
                "next_sequence": outcome.next_sequence,
                "complete": outcome.complete,
                "recovered": outcome.recovered,
            })
        except PcmProducerError as err:
            await self.release(capture.capture_id)
            self._send_error(connection, msg, err.code, err.code)
        except Exception:
            await self.release(capture.capture_id)
            self._send_error(connection, msg, "unknown_error", "Unable to submit audio")

    async def stop(self, connection: Any, msg: dict[str, Any]) -> None:
        capture = self._captures.get(msg.get("capture_id"))
        if (
            capture is None
            or capture.connection is not connection
            or msg.get("config_entry_id") != capture.entry_id
        ):
            self._send_error(connection, msg, "not_found", "Unknown capture")
            return
        await self.release(capture.capture_id)
        connection.send_result(msg["id"], {"capture_id": msg["capture_id"], "state": "idle"})

    async def release(self, capture_id: str) -> None:
        await self._stop_producers(self._invalidate_capture(capture_id))

    async def release_entry(self, entry_id: str) -> None:
        self._epochs[entry_id] = self._epochs.get(entry_id, 0) + 1
        self._starting.pop(entry_id, None)
        self._revoke_pending(entry_id)
        producers = []
        hub = self._hubs.get(entry_id)
        if hub is not None:
            producers.extend(self._invalidate_hub(entry_id, hub))
        capture = self._by_entry.get(entry_id)
        if capture is not None:
            producers.extend(self._invalidate_capture(capture.capture_id))
        await self._stop_producers(producers)

    async def reconcile(self, entry_id: str, status: dict[str, Any]) -> None:
        producers = []
        hub = self._hubs.get(entry_id)
        if hub is not None:
            try:
                fresh = AudioIdentity.from_status(status, hub.identity.station_id)
            except AudioStreamError:
                fresh = None
            if fresh != hub.identity:
                self._epochs[entry_id] = self._epochs.get(entry_id, 0) + 1
                self._starting.pop(entry_id, None)
                producers.extend(self._invalidate_hub(entry_id, hub))
        capture = self._by_entry.get(entry_id)
        if capture is None:
            await self._stop_producers(producers)
            return
        call = status.get("call") if isinstance(status, dict) else None
        generation = call.get("generation") if isinstance(call, dict) else None
        station_id = call.get("station_id") if isinstance(call, dict) else None
        session = call.get("session") if isinstance(call, dict) else None
        if (
            generation != capture.producer.generation
            or (capture.station_id is not None and station_id != capture.station_id)
            or session != "talking"
            or (capture.runtime_id is not None and status.get("runtime_id") != capture.runtime_id)
        ):
            producers.extend(self._invalidate_capture(capture.capture_id))
        await self._stop_producers(producers)


@websocket_command(({vol.Required("type"): "doorfast/audio/start", vol.Required("config_entry_id"): str, vol.Optional("station_id"): str, vol.Optional("generation"): int, vol.Optional("listener_id"): str} if vol else {"type": "doorfast/audio/start"}))
@async_response
async def _start(hass: Any, connection: Any, msg: dict[str, Any]) -> None:
    manager = hass.data.get(f"{DOMAIN}_pcm_ws")
    if manager is None:
        connection.send_error(msg.get("id"), "not_loaded", "Doorfast audio is not loaded")
        return
    await manager.start(connection, msg)


@websocket_command(({vol.Required("type"): "doorfast/audio/submit", vol.Required("config_entry_id"): str, vol.Required("capture_id"): str, vol.Required("pcm"): str, vol.Optional("station_id"): str, vol.Optional("generation"): int} if vol else {"type": "doorfast/audio/submit"}))
@async_response
async def _submit(hass: Any, connection: Any, msg: dict[str, Any]) -> None:
    manager = hass.data.get(f"{DOMAIN}_pcm_ws")
    if manager is None:
        connection.send_error(msg.get("id"), "not_loaded", "Doorfast audio is not loaded")
        return
    await manager.submit(connection, msg)


@websocket_command(({vol.Required("type"): "doorfast/audio/stop", vol.Required("config_entry_id"): str, vol.Required("capture_id"): str} if vol else {"type": "doorfast/audio/stop"}))
@async_response
async def _stop(hass: Any, connection: Any, msg: dict[str, Any]) -> None:
    manager = hass.data.get(f"{DOMAIN}_pcm_ws")
    if manager is None:
        connection.send_error(msg.get("id"), "not_loaded", "Doorfast audio is not loaded")
        return
    await manager.stop(connection, msg)


@websocket_command(({vol.Required("type"): "doorfast/audio/listen_start", vol.Required("config_entry_id"): str, vol.Required("station_id"): str, vol.Optional("generation"): int} if vol else {"type": "doorfast/audio/listen_start"}))
@async_response
async def _listen_start(hass: Any, connection: Any, msg: dict[str, Any]) -> None:
    manager = hass.data.get(f"{DOMAIN}_pcm_ws")
    if manager is None:
        connection.send_error(msg.get("id"), "not_loaded", "Doorfast audio is not loaded")
        return
    await manager.listen_start(connection, msg)


@websocket_command(({vol.Required("type"): "doorfast/audio/listen_read", vol.Required("config_entry_id"): str, vol.Required("listener_id"): str} if vol else {"type": "doorfast/audio/listen_read"}))
@async_response
async def _listen_read(hass: Any, connection: Any, msg: dict[str, Any]) -> None:
    manager = hass.data.get(f"{DOMAIN}_pcm_ws")
    if manager is None:
        connection.send_error(msg.get("id"), "not_loaded", "Doorfast audio is not loaded")
        return
    await manager.listen_read(connection, msg)


@websocket_command(({vol.Required("type"): "doorfast/audio/listen_stop", vol.Required("config_entry_id"): str, vol.Required("listener_id"): str} if vol else {"type": "doorfast/audio/listen_stop"}))
@async_response
async def _listen_stop(hass: Any, connection: Any, msg: dict[str, Any]) -> None:
    manager = hass.data.get(f"{DOMAIN}_pcm_ws")
    if manager is None:
        connection.send_error(msg.get("id"), "not_loaded", "Doorfast audio is not loaded")
        return
    await manager.listen_stop(connection, msg)
