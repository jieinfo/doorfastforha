from __future__ import annotations
from typing import Any
import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from .const import DEFAULT_AUDIO_PORT, DEFAULT_CALL_DURATION, DEFAULT_VIDEO_PORT
from .generation import resolve_generation

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
    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict[str, Any]:
        async with self.session.request(method, f"{self.base_url}{path}", json=payload, timeout=aiohttp.ClientTimeout(total=10)) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
            if not isinstance(data, dict): raise ValueError("Doorfast bridge returned a non-object response")
            return data
    async def refresh(self):
        self.status = await self._request("GET", "/api/v1/status")
        self.online = True
        if self._current_video_generation() != self._video_generation:
            self._clear_video_cache()
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
    @property
    def latest_audio_url(self) -> str:
        return f"{self.base_url}/api/v1/audio/latest.wav"
