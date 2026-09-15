from __future__ import annotations
from typing import Any
import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from .const import DEFAULT_AUDIO_PORT, DEFAULT_CALL_DURATION, DEFAULT_VIDEO_PORT
from .generation import resolve_generation
from .client_types import PcmHttpReply

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
            return self.status
        self.status = status
        self.online = True
        if self._current_video_generation() != self._video_generation:
            self._clear_video_cache()
        if self._current_audio_generation() != self._audio_generation:
            self._clear_audio_cursor()
        return self.status
    async def unlock(self, generation=None):
        generation = resolve_generation(self.status, generation)
        return await self._request("POST", "/api/v1/unlock", {"generation": generation})
    async def answer(self, generation=None, primary_media_port=DEFAULT_VIDEO_PORT, secondary_media_port=DEFAULT_AUDIO_PORT, duration_seconds=DEFAULT_CALL_DURATION):
        generation = resolve_generation(self.status, generation)
        return await self._request("POST", "/api/v1/answer", {"generation": generation, "primary_media_port": primary_media_port, "secondary_media_port": secondary_media_port, "duration_seconds": duration_seconds})
    async def hangup(self, generation=None, reason="ha"):
        generation = resolve_generation(self.status, generation)
        return await self._request("POST", "/api/v1/hangup", {"generation": generation, "reason": reason})
    async def call_elevator(self, direction="up"):
        if direction not in {"up", "down"}: raise ValueError("direction must be up or down")
        return await self._request("POST", "/api/v1/call_elevator", {"direction": direction})
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
        self, generation: int, previous_revision: int | None
    ) -> bool:
        if self._current_audio_generation() != generation:
            return False
        if previous_revision is None:
            return self._audio_generation is None and self._audio_revision is None
        return (
            self._audio_generation == generation
            and self._audio_revision == previous_revision
        )

    def _clear_audio_cursor_for_request(
        self, generation: int, previous_revision: int | None
    ) -> None:
        if self._audio_request_is_current(generation, previous_revision):
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
        generation = self._current_video_generation()
        if generation is None:
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
                return self._video_frame if self._video_generation == generation else None
            if response.status in {404, 409}:
                self._clear_video_cache()
                return None
            if response.status == 503:
                return self._video_frame if self._video_generation == generation else None
            response.raise_for_status()
            response_generation = response.headers.get("X-Doorfast-Generation")
            if response_generation != str(generation):
                self._clear_video_cache()
                return None
            frame = await response.read()
            if not frame:
                self._clear_video_cache()
                return None
            self._video_generation = generation
            self._video_etag = response.headers.get("ETag")
            self._video_frame = frame
            return frame

    async def latest_audio_chunk(self) -> bytes | None:
        generation = self._current_audio_generation()
        if generation is None:
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
                self._clear_audio_cursor_for_request(generation, expected_previous)
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
                    generation, expected_previous
                )
            ):
                self._clear_audio_cursor_for_request(generation, expected_previous)
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
