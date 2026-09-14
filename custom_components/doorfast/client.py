from __future__ import annotations
from typing import Any
import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession

class DoorfastClient:
    """Client for the Doorfast JSON bridge mapped to the local ubus API."""
    def __init__(self, hass, base_url: str):
        self.hass, self.base_url = hass, base_url.rstrip("/")
        self.session = async_get_clientsession(hass)
        self.status: dict[str, Any] = {}
        self.online = False
    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict[str, Any]:
        async with self.session.request(method, f"{self.base_url}{path}", json=payload, timeout=aiohttp.ClientTimeout(total=10)) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
            if not isinstance(data, dict): raise ValueError("Doorfast bridge returned a non-object response")
            return data
    async def refresh(self):
        self.status = await self._request("GET", "/api/v1/status"); self.online = True; return self.status
    async def unlock(self, generation=None): return await self._request("POST", "/api/v1/unlock", {} if generation is None else {"generation": generation})
    async def answer(self, generation, primary_media_port=0, secondary_media_port=0, duration_seconds=0):
        return await self._request("POST", "/api/v1/answer", {"generation": generation, "primary_media_port": primary_media_port, "secondary_media_port": secondary_media_port, "duration_seconds": duration_seconds})
    async def hangup(self, generation=None, reason="ha"): return await self._request("POST", "/api/v1/hangup", {"generation": generation, "reason": reason})
    async def call_elevator(self, direction="up"):
        if direction not in {"up", "down"}: raise ValueError("direction must be up or down")
        return await self._request("POST", "/api/v1/call_elevator", {"direction": direction})
