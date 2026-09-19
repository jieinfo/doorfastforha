"""Station-aware Home Assistant WebRTC provider backed by go2rtc."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote, urlsplit, urlunsplit

from homeassistant.components.camera import (
    Camera,
    CameraWebRTCProvider,
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCError,
)
from webrtc_models import RTCIceCandidateInit

from .config_helpers import normalize_go2rtc_api_url
from .const import DOMAIN

try:
    from homeassistant.exceptions import HomeAssistantError
except ImportError:  # pragma: no cover - dependency-free tests
    class HomeAssistantError(RuntimeError):
        """Fallback used when Home Assistant is not installed."""


_SOURCE_PREFIX = "doorfast://"
_SOURCE_SUFFIX = "/preview"
_OFFER_TIMEOUT = 10.0


@dataclass
class _Session:
    websocket: Any
    station_id: str
    coordinator: Any
    generation: int
    send_message: Callable[[Any], None]
    answer: asyncio.Future[None]
    reader: asyncio.Task[None] | None = None
    released: bool = False


class DoorfastWebRTCProvider(CameraWebRTCProvider):
    """Proxy station camera signaling to a configured go2rtc API."""

    def __init__(
        self,
        hass: Any,
        entry_id: str,
        registry: Any,
        go2rtc_api_url: str,
        session: Any | None = None,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._registry = registry
        self._go2rtc_api_url = normalize_go2rtc_api_url(go2rtc_api_url)
        if session is None:
            from homeassistant.helpers.aiohttp_client import async_get_clientsession

            session = async_get_clientsession(hass)
        self._session = session
        self._sessions: dict[str, _Session] = {}

    @property
    def domain(self) -> str:
        return DOMAIN

    def _station_id(self, stream_source: str) -> str | None:
        prefix = f"{_SOURCE_PREFIX}{self._entry_id}/station/"
        if not stream_source.startswith(prefix) or not stream_source.endswith(
            _SOURCE_SUFFIX
        ):
            return None
        station_id = stream_source[len(prefix) : -len(_SOURCE_SUFFIX)]
        if not station_id or "/" in station_id:
            return None
        try:
            self._registry.station(station_id)
            self._registry.monitor(station_id)
        except KeyError:
            return None
        return station_id

    def async_is_supported(self, stream_source: str) -> bool:
        return isinstance(stream_source, str) and self._station_id(stream_source) is not None

    def websocket_url(self, stream_name: str) -> str:
        parsed = urlsplit(self._go2rtc_api_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        path = f"{parsed.path.rstrip('/')}/api/ws"
        return urlunsplit(
            (scheme, parsed.netloc, path, f"src={quote(stream_name, safe='')}", "")
        )

    async def async_handle_async_webrtc_offer(
        self,
        camera: Camera,
        offer_sdp: str,
        session_id: str,
        send_message: Callable[[Any], None],
    ) -> None:
        source = await camera.stream_source()
        station_id = self._station_id(source) if isinstance(source, str) else None
        if station_id is None:
            raise HomeAssistantError("Doorfast camera source is not supported")
        if session_id in self._sessions:
            await self._cleanup_session(session_id)

        station = self._registry.station(station_id)
        coordinator = self._registry.monitor(station_id)
        generation = await coordinator.async_acquire_viewer()
        state: _Session | None = None
        try:
            await coordinator.async_wait_ready(generation, timeout=_OFFER_TIMEOUT)
            websocket = await self._session.ws_connect(
                self.websocket_url(station.stream_name)
            )
            loop = asyncio.get_running_loop()
            state = _Session(
                websocket=websocket,
                station_id=station_id,
                coordinator=coordinator,
                generation=generation,
                send_message=send_message,
                answer=loop.create_future(),
            )
            self._sessions[session_id] = state
            state.reader = asyncio.create_task(self._read_loop(session_id, state))
            await websocket.send_json({"type": "webrtc/offer", "value": offer_sdp})
            await asyncio.wait_for(asyncio.shield(state.answer), _OFFER_TIMEOUT)
        except asyncio.CancelledError:
            if state is not None and not state.released:
                await self._cleanup_session(session_id, state)
            elif state is None:
                await coordinator.async_release_viewer()
            raise
        except Exception as error:
            if state is not None and not state.released:
                await self._cleanup_session(session_id, state)
            elif state is None:
                await coordinator.async_release_viewer()
            if isinstance(error, HomeAssistantError):
                raise
            raise HomeAssistantError("Doorfast go2rtc negotiation failed") from error

    async def _read_loop(self, session_id: str, state: _Session) -> None:
        try:
            while True:
                message = await state.websocket.receive_json()
                if message is None:
                    break
                if not isinstance(message, dict):
                    raise HomeAssistantError("invalid go2rtc WebSocket message")
                message_type = message.get("type")
                value = message.get("value")
                if message_type == "webrtc/answer":
                    if not isinstance(value, str) or not value:
                        raise HomeAssistantError("invalid go2rtc WebRTC answer")
                    state.send_message(WebRTCAnswer(value))
                    if not state.answer.done():
                        state.answer.set_result(None)
                elif message_type == "webrtc/candidate":
                    if not isinstance(value, str):
                        raise HomeAssistantError("invalid go2rtc ICE candidate")
                    state.send_message(WebRTCCandidate(RTCIceCandidateInit(value)))
                elif message_type == "error":
                    error = HomeAssistantError("go2rtc WebRTC signaling failed")
                    state.send_message(WebRTCError("doorfast_webrtc_failed", str(value)))
                    if not state.answer.done():
                        state.answer.set_exception(error)
                    break
                else:
                    raise HomeAssistantError("unsupported go2rtc WebSocket message")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not state.answer.done():
                state.answer.set_exception(error)
        finally:
            await self._cleanup_session(session_id, state)

    async def async_on_webrtc_candidate(
        self, session_id: str, candidate: RTCIceCandidateInit
    ) -> None:
        state = self._sessions.get(session_id)
        if state is None or state.released:
            return
        value = getattr(candidate, "candidate", None)
        if not isinstance(value, str):
            raise HomeAssistantError("invalid HA ICE candidate")
        await state.websocket.send_json(
            {"type": "webrtc/candidate", "value": value}
        )

    def async_close_session(self, session_id: str) -> None:
        if session_id in self._sessions:
            self._hass.async_create_task(self._cleanup_session(session_id))

    async def _cleanup_session(
        self, session_id: str, expected: _Session | None = None
    ) -> None:
        state = self._sessions.get(session_id)
        if state is None or (expected is not None and state is not expected):
            return
        self._sessions.pop(session_id, None)
        state.released = True
        current = asyncio.current_task()
        if state.reader is not None and state.reader is not current:
            state.reader.cancel()
        try:
            await state.websocket.close()
        finally:
            if not state.answer.done():
                state.answer.set_exception(
                    HomeAssistantError("go2rtc WebRTC session closed")
                )
            await state.coordinator.async_release_viewer()

    async def async_close_entry(self) -> None:
        await asyncio.gather(
            *(self._cleanup_session(session_id) for session_id in list(self._sessions))
        )

    async def async_reconcile_monitor(self) -> None:
        stale = []
        for session_id, state in self._sessions.items():
            try:
                coordinator = self._registry.monitor(state.station_id)
            except KeyError:
                stale.append(session_id)
                continue
            if (
                coordinator is not state.coordinator
                or coordinator.generation != state.generation
                or not coordinator.ready
            ):
                stale.append(session_id)
        await asyncio.gather(*(self._cleanup_session(item) for item in stale))

    async def async_teardown(self) -> None:
        await self.async_close_entry()
