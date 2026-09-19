from __future__ import annotations
from typing import Any
import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from .const import DEFAULT_AUDIO_PORT, DEFAULT_CALL_DURATION, DEFAULT_VIDEO_PORT
from .generation import resolve_generation
from .client_types import (
    DoorfastStation,
    DoorfastStationSnapshot,
    PcmHttpReply,
    require_station_id,
)

class DoorfastClient:
    """Client for the Doorfast JSON bridge mapped to the local ubus API."""
    def __init__(self, hass, base_url: str):
        self.hass, self.base_url = hass, base_url.rstrip("/")
        self.session = async_get_clientsession(hass)
        self.status: dict[str, Any] = {}
        self.online = False
        self._video_generation: int | None = None
        self._video_etag: str | None = None
        self._video_frame: bytes | None = None
        self._audio_generation: int | None = None
        self._audio_revision: int | None = None
        self._audio_etag: str | None = None
        self._refresh_sequence = 0
    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict[str, Any]:
        async with self.session.request(method, f"{self.base_url}{path}", json=payload, timeout=aiohttp.ClientTimeout(total=10)) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
            if not isinstance(data, dict): raise ValueError("Doorfast bridge returned a non-object response")
            return data
    async def _pcm_request(self, path: str, params: dict[str, str], headers: dict[str, str], body: bytes) -> PcmHttpReply:
        async with self.session.request(
            "POST",
            f"{self.base_url}{path}",
            params=params,
            headers=headers,
            data=body,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            payload = await response.json(content_type=None)
            if not isinstance(payload, dict):
                raise ValueError("Doorfast PCM bridge returned a non-object response")
            return PcmHttpReply(response.status, payload)

    async def pcm_session_open(self, runtime_id: str, generation: int) -> PcmHttpReply:
        return await self._pcm_request(
            "/api/v1/audio/session",
            {"runtime": runtime_id, "generation": str(generation)},
            {},
            b"",
        )

    async def pcm_submit(self, runtime_id: str, generation: int, sequence: int, session_token: str, body: bytes) -> PcmHttpReply:
        return await self._pcm_request(
            "/api/v1/audio/submit.pcm",
            {
                "runtime": runtime_id,
                "generation": str(generation),
                "sequence": str(sequence),
            },
            {
                "Content-Type": "application/octet-stream",
                "X-Doorfast-Audio-Session": session_token,
            },
            body,
        )

    async def pcm_session_end(self, runtime_id: str, generation: int, session_token: str) -> PcmHttpReply:
        return await self._pcm_request(
            "/api/v1/audio/session/end",
            {"runtime": runtime_id, "generation": str(generation)},
            {"X-Doorfast-Audio-Session": session_token},
            b"",
        )

    async def refresh(self):
        self._refresh_sequence += 1
        refresh_sequence = self._refresh_sequence
        status = await self._request("GET", "/api/v1/status")
        if refresh_sequence != self._refresh_sequence:
            return status
        previous_runtime_id = self.status.get("runtime_id")
        runtime_changed = (
            isinstance(previous_runtime_id, str)
            and previous_runtime_id
            and status.get("runtime_id") != previous_runtime_id
        )
        self.status = status
        self.online = True
        if runtime_changed or self._current_video_generation() != self._video_generation:
            self._clear_video_cache()
        if runtime_changed or self._current_audio_generation() != self._audio_generation:
            self._clear_audio_cursor()
        return self.status

    def _current_runtime_id(self) -> str | None:
        runtime_id = self.status.get("runtime_id")
        if (
            not isinstance(runtime_id, str)
            or len(runtime_id) != 16
            or any(character not in "0123456789abcdef" for character in runtime_id)
        ):
            return None
        return runtime_id

    def _control_runtime_id(self) -> str:
        runtime_id = self._current_runtime_id()
        if runtime_id is None:
            raise ValueError("Doorfast status has no valid runtime_id")
        return runtime_id

    async def unlock(self, generation=None):
        generation = resolve_generation(self.status, generation)
        return await self._request("POST", "/api/v1/unlock", {
            "runtime_id": self._control_runtime_id(),
            "generation": generation,
        })
    async def answer(self, generation=None, primary_media_port=DEFAULT_VIDEO_PORT, secondary_media_port=DEFAULT_AUDIO_PORT, duration_seconds=DEFAULT_CALL_DURATION):
        generation = resolve_generation(self.status, generation)
        return await self._request("POST", "/api/v1/answer", {"runtime_id": self._control_runtime_id(), "generation": generation, "primary_media_port": primary_media_port, "secondary_media_port": secondary_media_port, "duration_seconds": duration_seconds})
    async def hangup(self, generation=None, reason="ha"):
        generation = resolve_generation(self.status, generation)
        return await self._request("POST", "/api/v1/hangup", {"runtime_id": self._control_runtime_id(), "generation": generation, "reason": reason})
    async def call_elevator(self, direction="up"):
        if direction not in {"up", "down"}: raise ValueError("direction must be up or down")
        return await self._request("POST", "/api/v1/call_elevator", {"runtime_id": self._control_runtime_id(), "direction": direction})

    @staticmethod
    def _snapshot_runtime_id(value: Any) -> str:
        if (
            not isinstance(value, str)
            or len(value) != 16
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("station snapshot has no valid runtime_id")
        return value

    @staticmethod
    def _snapshot_revision(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("station snapshot revision must be non-negative")
        return value

    async def stations(self) -> DoorfastStationSnapshot:
        payload = await self._request("GET", "/api/v1/stations")
        if not isinstance(payload, dict):
            raise ValueError("station snapshot must be an object")
        runtime_id = self._snapshot_runtime_id(payload.get("runtime_id"))
        revision = self._snapshot_revision(payload.get("revision"))
        raw_stations = payload.get("stations")
        if not isinstance(raw_stations, list):
            raise ValueError("station snapshot stations must be an array")
        stations = tuple(DoorfastStation.from_payload(raw) for raw in raw_stations)
        if len({station.station_id for station in stations}) != len(stations):
            raise ValueError("station snapshot contains duplicate station ids")
        if len({station.stream_name for station in stations}) != len(stations):
            raise ValueError("station snapshot contains duplicate station stream names")
        return DoorfastStationSnapshot(runtime_id, revision, stations)

    @staticmethod
    def _monitor_generation(generation: Any) -> int:
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation <= 0
        ):
            raise ValueError("monitor generation must be a positive integer")
        return generation

    async def start_monitor(
        self, runtime_id: str, station_id: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/api/v1/monitor/start",
            {"runtime_id": runtime_id, "station_id": require_station_id(station_id)},
        )

    async def stop_monitor(
        self, runtime_id: str, station_id: str, generation: int
    ) -> dict[str, Any]:
        generation = self._monitor_generation(generation)
        return await self._request(
            "POST",
            "/api/v1/monitor/stop",
            {
                "runtime_id": runtime_id,
                "station_id": require_station_id(station_id),
                "generation": generation,
            },
        )

    async def set_monitor_viewer(
        self, runtime_id: str, station_id: str, generation: int, active: bool
    ) -> dict[str, Any]:
        generation = self._monitor_generation(generation)
        if not isinstance(active, bool):
            raise ValueError("monitor viewer active must be a boolean")
        return await self._request(
            "POST",
            "/api/v1/monitor/viewer",
            {
                "runtime_id": runtime_id,
                "station_id": require_station_id(station_id),
                "generation": generation,
                "active": active,
            },
        )

    async def monitor_status(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/monitor/status")
    def _current_video_generation(self) -> int | None:
        call = self.status.get("call")
        video = self.status.get("video")
        if not isinstance(call, dict) or not isinstance(video, dict):
            return None
        call_generation = call.get("generation")
        video_generation = video.get("generation")
        if (
            video.get("ready") is not True
            or isinstance(call_generation, bool)
            or isinstance(video_generation, bool)
            or not isinstance(call_generation, int)
            or not isinstance(video_generation, int)
            or call_generation <= 0
            or video_generation != call_generation
        ):
            return None
        return call_generation

    def _clear_video_cache(self) -> None:
        self._video_generation = None
        self._video_etag = None
        self._video_frame = None

    def _video_request_is_current(self, runtime_id: str, generation: int) -> bool:
        return (
            self._current_runtime_id() == runtime_id
            and self._current_video_generation() == generation
        )

    def _clear_video_cache_for_request(
        self, runtime_id: str, generation: int
    ) -> None:
        if self._video_request_is_current(runtime_id, generation):
            self._clear_video_cache()

    def _current_audio_generation(self) -> int | None:
        call = self.status.get("call")
        audio = self.status.get("audio")
        if not isinstance(call, dict) or not isinstance(audio, dict):
            return None
        call_generation = call.get("generation")
        audio_generation = audio.get("generation")
        revision = audio.get("snapshot_packet_count")
        if (
            audio.get("snapshot_ready") is not True
            or isinstance(call_generation, bool)
            or isinstance(audio_generation, bool)
            or isinstance(revision, bool)
            or not isinstance(call_generation, int)
            or not isinstance(audio_generation, int)
            or not isinstance(revision, int)
            or call_generation <= 0
            or revision <= 0
            or audio_generation != call_generation
        ):
            return None
        return call_generation

    def _clear_audio_cursor(self) -> None:
        self._audio_generation = None
        self._audio_revision = None
        self._audio_etag = None

    def _audio_request_is_current(
        self, runtime_id: str, generation: int, previous_revision: int | None
    ) -> bool:
        if (
            self._current_runtime_id() != runtime_id
            or self._current_audio_generation() != generation
        ):
            return False
        if previous_revision is None:
            return self._audio_generation is None and self._audio_revision is None
        return (
            self._audio_generation == generation
            and self._audio_revision == previous_revision
        )

    def _clear_audio_cursor_for_request(
        self, runtime_id: str, generation: int, previous_revision: int | None
    ) -> None:
        if self._audio_request_is_current(
            runtime_id, generation, previous_revision
        ):
            self._clear_audio_cursor()

    @staticmethod
    def _audio_header_int(headers, name: str) -> int | None:
        raw = headers.get(name)
        if (
            not isinstance(raw, str)
            or not raw.isascii()
            or not raw.isdigit()
        ):
            return None
        return int(raw)

    async def latest_video_frame(self) -> bytes | None:
        runtime_id = self._current_runtime_id()
        generation = self._current_video_generation()
        if runtime_id is None or generation is None:
            self._clear_video_cache()
            return None
        headers = {}
        if self._video_generation == generation and self._video_etag is not None:
            headers["If-None-Match"] = self._video_etag
        async with self.session.get(
            f"{self.base_url}/api/v1/video/latest.jpg",
            params={"generation": generation},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            if response.status == 304:
                return (
                    self._video_frame
                    if self._video_request_is_current(runtime_id, generation)
                    and self._video_generation == generation
                    else None
                )
            if response.status in {404, 409}:
                self._clear_video_cache_for_request(runtime_id, generation)
                return None
            if response.status == 503:
                return (
                    self._video_frame
                    if self._video_request_is_current(runtime_id, generation)
                    and self._video_generation == generation
                    else None
                )
            response.raise_for_status()
            response_generation = response.headers.get("X-Doorfast-Generation")
            if response_generation != str(generation):
                self._clear_video_cache_for_request(runtime_id, generation)
                return None
            frame = await response.read()
            if (
                not frame
                or not self._video_request_is_current(runtime_id, generation)
            ):
                self._clear_video_cache_for_request(runtime_id, generation)
                return None
            self._video_generation = generation
            self._video_etag = response.headers.get("ETag")
            self._video_frame = frame
            return frame

    async def latest_audio_chunk(self) -> bytes | None:
        runtime_id = self._current_runtime_id()
        generation = self._current_audio_generation()
        if runtime_id is None or generation is None:
            self._clear_audio_cursor()
            return None
        latest_revision = self.status["audio"]["snapshot_packet_count"]
        params = {"generation": generation}
        headers = {}
        expected_previous = None
        if self._audio_generation == generation and self._audio_revision is not None:
            expected_previous = self._audio_revision
            params["after"] = expected_previous
            if self._audio_etag is not None:
                headers["If-None-Match"] = self._audio_etag
        async with self.session.get(
            f"{self.base_url}/api/v1/audio/latest.wav",
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            if response.status == 304:
                return None
            if response.status in {404, 409}:
                self._clear_audio_cursor_for_request(
                    runtime_id, generation, expected_previous
                )
                return None
            if response.status == 503:
                return None
            response.raise_for_status()
            response_generation = self._audio_header_int(
                response.headers, "X-Doorfast-Generation"
            )
            previous_revision = self._audio_header_int(
                response.headers, "X-Doorfast-Audio-Previous-Revision"
            )
            revision = self._audio_header_int(
                response.headers, "X-Doorfast-Audio-Revision"
            )
            chunk = await response.read()
            if (
                response_generation != generation
                or previous_revision is None
                or revision is None
                or revision <= previous_revision
                or (
                    expected_previous is None
                    and revision < latest_revision
                )
                or (
                    expected_previous is not None
                    and previous_revision != expected_previous
                )
                or not chunk
                or not self._audio_request_is_current(
                    runtime_id, generation, expected_previous
                )
            ):
                self._clear_audio_cursor_for_request(
                    runtime_id, generation, expected_previous
                )
                return None
            self._audio_generation = generation
            self._audio_revision = revision
            expected_etag = f'"df-audio-{generation}-{revision}"'
            response_etag = response.headers.get("ETag")
            self._audio_etag = (
                response_etag if response_etag == expected_etag else None
            )
            return chunk

    @property
    def latest_audio_url(self) -> str:
        return f"{self.base_url}/api/v1/audio/latest.wav"
