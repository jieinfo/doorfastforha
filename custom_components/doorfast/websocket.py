"""Authenticated WebSocket ownership for shared listening and microphone PCM."""
from __future__ import annotations

import asyncio
import base64
import binascii
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable
from weakref import WeakValueDictionary

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
    client: Any = None
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
    reservation_key: tuple[int, str, str] | None = None
    reservation: object | None = None
    client: Any = None


@dataclass
class _PendingStart:
    entry_id: str
    listener_id: str | None
    revoke: Callable[[], None]
    station_id: str | None = None
    identity: AudioIdentity | None = None
    producer: PcmProducer | None = None
    request_runtime_id: str | None = None
    request_generation: int | None = None
    revoked_identity: AudioIdentity | None = None


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
        # Hubs (including in-flight reads on retired hubs) retain their lock.
        # The registry must not keep removed config entries alive indefinitely.
        self._fetch_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._epochs: dict[str, int] = {}
        self._pending_cleanups: dict[str, _PendingStart] = {}
        self._listener_reservations: dict[tuple[int, str, str], object] = {}
        self._subscription_cleanup_depth: dict[int, int] = {}
        self._admission: dict[str, bool] = {
            entry_id: True
            for entry_id in hass.data.get(DOMAIN, {})
            if isinstance(entry_id, str)
        }
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
        if client is None or self._admission.get(entry_id) is not True:
            raise AudioStreamError("not_loaded")
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
            connection_key = id(connection)

            def invoke() -> Any:
                self._subscription_cleanup_depth[connection_key] = (
                    self._subscription_cleanup_depth.get(connection_key, 0) + 1
                )
                try:
                    return callback()
                finally:
                    depth = self._subscription_cleanup_depth.get(connection_key, 1) - 1
                    if depth:
                        self._subscription_cleanup_depth[connection_key] = depth
                    else:
                        self._subscription_cleanup_depth.pop(connection_key, None)

            subscriptions[subscription_key] = invoke
            removed = False

            def remove() -> None:
                nonlocal removed
                if removed:
                    return
                removed = True
                if self._subscription_cleanup_depth.get(connection_key, 0):
                    # Home Assistant iterates this dictionary as a live view.
                    # Defer physical removal; ownership is already invalidated.
                    asyncio.get_running_loop().call_soon(
                        subscriptions.pop, subscription_key, None
                    )
                else:
                    subscriptions.pop(subscription_key, None)

            return remove
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
                        callback: Callable[[], None], listener_id: str | None = None,
                        *, station_id: str | None = None, identity: AudioIdentity | None = None,
                        producer: PcmProducer | None = None, request_runtime_id: str | None = None,
                        request_generation: int | None = None) -> tuple[Callable[[], None], _PendingStart]:
        key = secrets.token_urlsafe(24)
        def revoke() -> None:
            self._pending_cleanups.pop(key, None)
            cleanup()
            callback()
        cleanup = self._attach_cleanup(connection, key, revoke)
        pending = _PendingStart(entry_id, listener_id, revoke, station_id, identity, producer,
                                request_runtime_id, request_generation)
        self._pending_cleanups[key] = pending
        def detach() -> None:
            self._pending_cleanups.pop(key, None)
            cleanup()
        return detach, pending

    def _revoke_pending(self, entry_id: str, listener_id: str | None = None) -> None:
        for pending in list(self._pending_cleanups.values()):
            if pending.entry_id == entry_id and (listener_id is None or pending.listener_id == listener_id):
                pending.revoke()

    @staticmethod
    def _listener_identity(status: Any, station_id: str) -> AudioIdentity | None:
        try:
            return AudioIdentity.from_status(status, station_id)
        except AudioStreamError:
            return None

    @staticmethod
    def _operation_allows_audio(status: Any) -> bool:
        """Keep observed outbound conflicts closed across producer awaits.

        Legacy microphone status needs neither a station nor a listener stream
        capability. Its existing native owner guards remain independent.
        """
        if not isinstance(status, dict) or "station_call" not in status:
            return True
        from .session_diagnostics import station_call_phase

        call = status.get("call")
        station_id = call.get("station_id") if isinstance(call, dict) else None
        return station_call_phase(status, station_id) == "in_call"

    def _owned_listener(self, connection: Any, msg: dict[str, Any]) -> _Listener:
        listener_id = msg.get("listener_id")
        listener = self._listeners.get(listener_id) if isinstance(listener_id, str) else None
        if (listener is None or listener.connection is not connection
                or listener.entry_id != msg.get("config_entry_id")):
            raise AudioStreamError("not_found")
        if (
            self._admission.get(listener.entry_id) is not True
            or self.hass.data.get(DOMAIN, {}).get(listener.entry_id) is not listener.client
        ):
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
        self._release_listener_reservation(listener)
        capture = self._by_entry.get(listener.entry_id)
        if capture is not None and capture.listener_id == listener_id:
            return self._invalidate_capture(capture.capture_id)
        return []

    def _release_listener_reservation(self, listener: _Listener) -> None:
        if listener.reservation_key is not None and (
            self._listener_reservations.get(listener.reservation_key) is listener.reservation
        ):
            self._listener_reservations.pop(listener.reservation_key, None)

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
        reservation_key: tuple[int, str, str] | None = None
        reservation: object | None = None
        published = False
        try:
            entry_id = msg.get("config_entry_id")
            client = self._client(entry_id)
            station_id = msg.get("station_id")
            generation = msg.get("generation")
            if not isinstance(station_id, str) or not station_id or (
                generation is not None and not _safe_int(generation, 1)
            ):
                raise ValueError("Invalid listener identity")
            reservation_key = (id(connection), entry_id, station_id)
            if reservation_key in self._listener_reservations:
                raise AudioStreamError("audio_listener_busy")
            reservation = object()
            self._listener_reservations[reservation_key] = reservation
            epoch = self._epochs.get(entry_id, 0)

            def cancel_pending() -> None:
                pending[0] = False
                if reservation_key is not None and (
                    self._listener_reservations.get(reservation_key) is reservation
                ):
                    self._listener_reservations.pop(reservation_key, None)

            cleanup, _ = self._attach_pending(entry_id, connection, cancel_pending,
                station_id=station_id, identity=self._listener_identity(client.status, station_id))
            status = await client.refresh()
            if (not pending[0] or self._epochs.get(entry_id, 0) != epoch
                or self._listener_reservations.get(reservation_key) is not reservation
                or self.hass.data.get(DOMAIN, {}).get(entry_id) is not client
                or self._admission.get(entry_id) is not True):
                raise AudioStreamError("not_found")
            audio = status.get("audio") if isinstance(status, dict) else None
            if not isinstance(audio, dict) or type(audio.get("pcm_stream_version")) is not int or audio.get("pcm_stream_version") != 1:
                raise AudioStreamError("unsupported_audio_stream")
            identity = AudioIdentity.from_status(status, station_id, expected_generation=generation)
            # Older concurrent refreshes return their own response but cannot
            # overwrite the client's latest authoritative status cache.
            if identity != self._listener_identity(client.status, station_id):
                raise AudioStreamError("audio_identity_mismatch")
            if not _safe_int(identity.generation, 1):
                raise ValueError("Invalid generation")
            hub = self._hubs.get(entry_id)
            producers = []
            if hub is not None and (hub.closed or hub.identity != identity):
                producers = self._invalidate_hub(entry_id, hub)
                hub = None
            if hub is None:
                hub = AudioListenerHub(
                    client,
                    identity,
                    asyncio.get_running_loop().time,
                    fetch_lock=self._fetch_locks.setdefault(entry_id, asyncio.Lock()),
                )
                self._hubs[entry_id] = hub
            listener_id = secrets.token_urlsafe(24)
            hub.add(listener_id)
            listener = _Listener(
                entry_id, connection, listener_id, identity, hub,
                reservation_key=reservation_key, reservation=reservation, client=client,
            )
            self._listeners[listener_id] = listener
            listener.cleanup = self._attach_cleanup(connection, listener_id,
                lambda: self._schedule_stop(self._invalidate_listener(listener_id)))
            self._renew_lease(listener)
            cleanup()
            published = True
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
            if not published and reservation_key is not None and (
                self._listener_reservations.get(reservation_key) is reservation
            ):
                self._listener_reservations.pop(reservation_key, None)

    async def listen_read(self, connection: Any, msg: dict[str, Any]) -> None:
        if not self._is_admin(connection):
            self._send_error(connection, msg, "unauthorized", "Administrator permission required")
            return
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
            if self._owned_listener(connection, msg) is not listener:
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
            if listener is not None and err.code == "not_found":
                await self._stop_producers(self._invalidate_listener(listener.listener_id))
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
        if not self._is_admin(connection):
            self._send_error(connection, msg, "unauthorized", "Administrator permission required")
            return
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
            # Only listener binding constrains the producer's expected runtime.
            # A direct request may select a new runtime in its first refresh;
            # cached status is not a binding or an audio admission substitute.
            cleanup, pending = self._attach_pending(entry_id, connection, cancel_start,
                listener.listener_id if listener else None, station_id=station_id,
                identity=listener.identity if listener else None, producer=producer,
                request_runtime_id=listener.identity.runtime_id if listener else None,
                request_generation=generation)
            try:
                await producer.start()
                owner = getattr(producer, "call_identity", None)
                revoked = pending.revoked_identity
                if (self._starting.get(entry_id) is not reservation or
                    self.hass.data.get(DOMAIN, {}).get(entry_id) is not client or
                    self._admission.get(entry_id) is not True or
                    (revoked is not None and (owner is None or
                        owner == (revoked.runtime_id, revoked.station_id, revoked.generation))) or
                    not self._operation_allows_audio(getattr(client, "status", None)) or
                    (listener is not None and (self._listeners.get(listener.listener_id) is not listener
                        or listener.hub.closed or listener.connection is not connection))):
                    raise PcmProducerError("not_found")
                capture_id = secrets.token_urlsafe(24)
                capture = _Capture(entry_id, connection, producer, capture_id, station_id,
                    listener.listener_id if listener else None,
                    listener.identity.runtime_id if listener else None, client=client)
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
        if (self._admission.get(capture.entry_id) is not True
            or self.hass.data.get(DOMAIN, {}).get(capture.entry_id) is not capture.client):
            await self.release(capture.capture_id)
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
            if (self._captures.get(capture.capture_id) is not capture
                or self.hass.data.get(DOMAIN, {}).get(capture.entry_id) is not capture.client
                or self._admission.get(capture.entry_id) is not True):
                raise PcmProducerError("not_found")
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

    def admit_entry(self, entry_id: str) -> None:
        """Allow new audio ownership after a complete config-entry setup."""
        if entry_id in self.hass.data.get(DOMAIN, {}):
            self._admission[entry_id] = True

    def _invalidate_entry(self, entry_id: str) -> list[PcmProducer]:
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
        return producers

    async def release_entry(self, entry_id: str) -> None:
        """Release current ownership for hangup/reconcile without closing admission."""
        await self._stop_producers(self._invalidate_entry(entry_id))

    async def release_call(self, entry_id: str, runtime_id: str,
                           station_id: str, generation: int) -> None:
        """Revoke only the exact native owner after accepted station hangup."""
        identity = AudioIdentity(runtime_id, station_id, generation)
        producers = []
        for pending in tuple(self._pending_cleanups.values()):
            if pending.entry_id != entry_id:
                continue
            owner = getattr(pending.producer, "call_identity", None)
            # Once fresh admission chooses an owner it supersedes the request
            # candidate, which may describe an older incoming runtime.
            if owner is not None:
                if owner == (runtime_id, station_id, generation):
                    pending.revoke()
            elif pending.identity == identity:
                pending.revoke()
            elif (pending.producer is not None
                    and pending.request_runtime_id in (None, runtime_id)
                    and pending.station_id in (None, station_id)
                    and pending.request_generation in (None, generation)):
                # An unbound first refresh can learn this owner after release.
                # Keep one exact marker per pending request, never a global set.
                # Two distinct relevant releases make only this request ambiguous.
                if pending.revoked_identity not in (None, identity):
                    pending.revoke()
                else:
                    pending.revoked_identity = identity
        hub = self._hubs.get(entry_id)
        if hub is not None and hub.identity == identity:
            producers.extend(self._invalidate_hub(entry_id, hub))
        capture = self._by_entry.get(entry_id)
        if (capture is not None and capture.producer.call_identity ==
                (runtime_id, station_id, generation)):
            producers.extend(self._invalidate_capture(capture.capture_id))
        await self._stop_producers(producers)

    async def close_entry(self, entry_id: str) -> None:
        """Close admission before teardown and invalidate all current ownership."""
        self._admission[entry_id] = False
        await self._stop_producers(self._invalidate_entry(entry_id))

    async def reconcile(self, entry_id: str, status: dict[str, Any]) -> None:
        producers = []
        for pending in list(self._pending_cleanups.values()):
            if pending.entry_id != entry_id:
                continue
            if pending.producer is not None:
                identity = getattr(pending.producer, "call_identity", None)
                call = status.get("call") if isinstance(status, dict) else None
                if (identity is None or not isinstance(call, dict)
                    or call.get("session") != "talking"
                    or status.get("runtime_id") != identity[0]
                    or type(call.get("generation")) is not int or call.get("generation") != identity[2]
                    or not self._operation_allows_audio(status)
                    or (identity[1] is not None and call.get("station_id") != identity[1])):
                    pending.revoke()
            elif pending.station_id is not None:
                fresh = self._listener_identity(status, pending.station_id)
                # Unknown cached identity is unbound; conservatively revoke it
                # on a status observation, while preserving same-call polls.
                if pending.identity is None or fresh != pending.identity:
                    pending.revoke()
        hub = self._hubs.get(entry_id)
        if hub is not None:
            try:
                fresh = AudioIdentity.from_status(status, hub.identity.station_id)
            except AudioStreamError:
                fresh = None
            if fresh != hub.identity:
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
            or not self._operation_allows_audio(status)
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
