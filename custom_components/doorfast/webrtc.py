"""Station-aware Home Assistant WebRTC provider backed by go2rtc."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote, urlsplit, urlunsplit

try:
    from aiohttp import BasicAuth
except ImportError:  # pragma: no cover - dependency-free unit tests
    class BasicAuth:
        """Minimal fallback for tests that do not install Home Assistant deps."""

        def __init__(self, login: str, password: str) -> None:
            self.login = login
            self.password = password
from homeassistant.components.camera import (
    Camera,
    CameraWebRTCProvider,
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCError,
)
from webrtc_models import RTCIceCandidateInit

from .config_helpers import normalize_go2rtc_api_url
from .const import DOMAIN, MONITOR_READY_TIMEOUT

try:
    from homeassistant.exceptions import HomeAssistantError
except ImportError:  # pragma: no cover - dependency-free tests
    class HomeAssistantError(RuntimeError):
        """Fallback used when Home Assistant is not installed."""


_SOURCE_PREFIX = "doorfast://"
_SOURCE_SUFFIX = "/preview"
_OFFER_TIMEOUT = 10.0
# Doorfast may report the monitor ready before its RTSP producer is visible
# to go2rtc. Keep retrying while the HA WebRTC request remains open. The
# frontend closes the session when the viewer leaves, which cancels this loop.
_NEGOTIATION_RETRY_DELAY = 1.0


def _consume_answer_exception(future: asyncio.Future[None]) -> None:
    if not future.cancelled():
        future.exception()


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
    negotiating: bool = True
    notify_error: bool = False


class _ProducerNotReadyError(RuntimeError):
    """The go2rtc stream is not published yet and can be retried."""


class DoorfastWebRTCProvider(CameraWebRTCProvider):
    """Proxy station camera signaling to a configured go2rtc API."""

    def __init__(
        self,
        hass: Any,
        entry_id: str,
        registry: Any,
        go2rtc_api_url: str,
        session: Any | None = None,
        go2rtc_username: str | None = None,
        go2rtc_password: str | None = None,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._registry = registry
        self._go2rtc_api_url = normalize_go2rtc_api_url(go2rtc_api_url)
        self._go2rtc_auth = (
            BasicAuth(go2rtc_username, go2rtc_password or "")
            if go2rtc_username
            else None
        )
        if session is None:
            from homeassistant.helpers.aiohttp_client import async_get_clientsession

            session = async_get_clientsession(hass)
        self._session = session
        self._sessions: dict[str, _Session] = {}
        self._negotiations: dict[str, asyncio.Task[None]] = {}

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
        current_task = asyncio.current_task()
        previous = self._negotiations.get(session_id)
        if previous is not None and previous is not current_task:
            previous.cancel()
            await asyncio.gather(previous, return_exceptions=True)
        if session_id in self._sessions:
            await self._cleanup_session(session_id)
        if current_task is not None:
            self._negotiations[session_id] = current_task

        station = self._registry.station(station_id)
        coordinator = self._registry.monitor(station_id)
        generation = await coordinator.async_acquire_viewer()
        viewer_released = False
        try:
            await coordinator.async_wait_ready(
                generation, timeout=MONITOR_READY_TIMEOUT
            )
            while True:
                state: _Session | None = None
                try:
                    websocket = await self._session.ws_connect(
                        self.websocket_url(station.stream_name),
                        **(
                            {"auth": self._go2rtc_auth}
                            if self._go2rtc_auth is not None
                            else {}
                        ),
                    )
                    loop = asyncio.get_running_loop()
                    state = _Session(
                        websocket=websocket,
                        station_id=station_id,
                        coordinator=coordinator,
                        generation=generation,
                        send_message=send_message,
                        answer=loop.create_future(),
                        notify_error=False,
                    )
                    state.answer.add_done_callback(_consume_answer_exception)
                    self._sessions[session_id] = state
                    state.reader = asyncio.create_task(self._read_loop(session_id, state))
                    await websocket.send_json(
                        {"type": "webrtc/offer", "value": offer_sdp}
                    )
                    await asyncio.wait_for(
                        asyncio.shield(state.answer), _OFFER_TIMEOUT
                    )
                    state.negotiating = False
                    return
                except asyncio.CancelledError:
                    if state is not None and not state.released:
                        await self._cleanup_session(session_id, state)
                    viewer_released = True
                    raise
                except HomeAssistantError:
                    if state is not None and not state.released:
                        await self._cleanup_session(
                            session_id, state, release_viewer=False
                        )
                    raise
                except Exception:
                    if state is not None and not state.released:
                        await self._cleanup_session(
                            session_id, state, release_viewer=False
                        )
                    # A producer can disappear between attempts while the
                    # station remains active. Keep the same Doorfast viewer
                    # lease and retry until HA closes this WebRTC session.
                    await asyncio.sleep(_NEGOTIATION_RETRY_DELAY)
        except asyncio.CancelledError:
            if not viewer_released:
                await coordinator.async_release_viewer()
            raise
        except Exception:
            if not viewer_released:
                # Covers failures before a WebSocket session is created.
                await coordinator.async_release_viewer()
            raise
        finally:
            if self._negotiations.get(session_id) is current_task:
                self._negotiations.pop(session_id, None)

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
                    state.negotiating = False
                    if not state.answer.done():
                        state.answer.set_result(None)
                elif message_type == "webrtc/candidate":
                    if not isinstance(value, str):
                        raise HomeAssistantError("invalid go2rtc ICE candidate")
                    state.send_message(WebRTCCandidate(RTCIceCandidateInit(value)))
                elif message_type == "error":
                    error = _ProducerNotReadyError(
                        "go2rtc WebRTC producer is not ready"
                    )
                    if not state.negotiating or state.notify_error:
                        state.send_message(
                            WebRTCError("doorfast_webrtc_failed", str(value))
                        )
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
            if not state.negotiating:
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
        negotiation = self._negotiations.get(session_id)
        if negotiation is not None:
            negotiation.cancel()
        if session_id in self._sessions:
            self._hass.async_create_task(self._cleanup_session(session_id))

    async def _cleanup_session(
        self,
        session_id: str,
        expected: _Session | None = None,
        release_viewer: bool = True,
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
                if state.negotiating:
                    state.answer.cancel()
                else:
                    state.answer.set_exception(
                        HomeAssistantError("go2rtc WebRTC session closed")
                    )
            if release_viewer:
                await state.coordinator.async_release_viewer()

    async def async_close_entry(self) -> None:
        negotiations = list(self._negotiations.values())
        for negotiation in negotiations:
            negotiation.cancel()
        await asyncio.gather(*negotiations, return_exceptions=True)
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
