"""Authenticated Home Assistant WebSocket ownership for microphone capture."""
from __future__ import annotations

import base64
import binascii
import secrets
from dataclasses import dataclass
from typing import Any, Callable

from .const import DOMAIN
from .pcm import PcmProducer, PcmProducerError, PcmProducerState

_FRAME_BYTES = 320
_MAX_FRAMES = 5
_MAX_PCM_BYTES = _FRAME_BYTES * _MAX_FRAMES
_MAX_PCM_B64_CHARS = 4 * ((_MAX_PCM_BYTES + 2) // 3)

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


class PcmWebSocketManager:
    """Own one producer per config entry and expose only opaque capture IDs."""

    def __init__(self, hass: Any, producer_factory: Callable[[Any], PcmProducer] = PcmProducer):
        self.hass = hass
        self.producer_factory = producer_factory
        self._captures: dict[str, _Capture] = {}
        self._by_entry: dict[str, _Capture] = {}
        self._registered = False

    def register(self) -> None:
        if self._registered or async_register_command is None:
            return
        for handler in (_start, _submit, _stop):
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

    def _attach_cleanup(self, capture: _Capture) -> None:
        def callback() -> None:
            task = self.hass.async_create_task(self.release(capture.capture_id))
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)

        subscriptions = getattr(capture.connection, "subscriptions", None)
        if isinstance(subscriptions, dict):
            subscriptions[("doorfast_pcm", capture.capture_id)] = callback
            return

        async def async_callback() -> None:
            await self.release(capture.capture_id)
        for name in ("async_add_cleanup_callback", "async_on_remove"):
            method = getattr(capture.connection, name, None)
            if method is not None:
                method(async_callback)
                return

    async def start(self, connection: Any, msg: dict[str, Any]) -> None:
        if not self._is_admin(connection):
            self._send_error(connection, msg, "unauthorized", "Administrator permission required")
            return
        try:
            entry_id = msg.get("config_entry_id")
            client = self._client(entry_id)
            if entry_id in self._by_entry:
                raise PcmProducerError("producer_busy")
            producer = self.producer_factory(client)
            await producer.start()
            capture_id = secrets.token_urlsafe(24)
            capture = _Capture(entry_id, connection, producer, capture_id)
            self._captures[capture_id] = capture
            self._by_entry[entry_id] = capture
            self._attach_cleanup(capture)
            connection.send_result(msg["id"], {
                "capture_id": capture_id,
                "state": producer.state.value,
                "sequence": producer.sequence,
            })
        except (ValueError, PcmProducerError) as err:
            self._send_error(connection, msg, getattr(err, "code", "invalid_request"), str(err))
        except Exception:
            self._send_error(connection, msg, "unknown_error", "Unable to start audio capture")

    async def submit(self, connection: Any, msg: dict[str, Any]) -> None:
        capture = self._captures.get(msg.get("capture_id"))
        if (
            capture is None
            or capture.connection is not connection
            or msg.get("config_entry_id") != capture.entry_id
        ):
            self._send_error(connection, msg, "not_found", "Unknown capture")
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
        capture = self._captures.pop(capture_id, None)
        if capture is None:
            return
        self._by_entry.pop(capture.entry_id, None)
        try:
            await capture.producer.stop()
        except Exception:
            pass

    async def release_entry(self, entry_id: str) -> None:
        capture = self._by_entry.get(entry_id)
        if capture is not None:
            await self.release(capture.capture_id)

    async def reconcile(self, entry_id: str, status: dict[str, Any]) -> None:
        capture = self._by_entry.get(entry_id)
        if capture is None:
            return
        call = status.get("call") if isinstance(status, dict) else None
        generation = call.get("generation") if isinstance(call, dict) else None
        if generation != capture.producer.generation:
            await self.release(capture.capture_id)


@websocket_command(({vol.Required("type"): "doorfast/audio/start", vol.Required("config_entry_id"): str} if vol else {"type": "doorfast/audio/start"}))
@async_response
async def _start(hass: Any, connection: Any, msg: dict[str, Any]) -> None:
    manager = hass.data.get(f"{DOMAIN}_pcm_ws")
    if manager is None:
        connection.send_error(msg.get("id"), "not_loaded", "Doorfast audio is not loaded")
        return
    await manager.start(connection, msg)


@websocket_command(({vol.Required("type"): "doorfast/audio/submit", vol.Required("config_entry_id"): str, vol.Required("capture_id"): str, vol.Required("pcm"): str} if vol else {"type": "doorfast/audio/submit"}))
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
