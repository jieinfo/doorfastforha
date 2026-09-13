from __future__ import annotations
import asyncio
from datetime import datetime
from aiohttp.web import Request, Response, json_response
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.dispatcher import async_dispatcher_send
from .client import DoorfastClient
from .const import DOMAIN, PLATFORMS, CONF_SERVER_ADDRESS, CONF_POLL_INTERVAL, LATEST_EVENT, RING_STATUS

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    client = DoorfastClient(hass, entry.data[CONF_SERVER_ADDRESS]); hass.data.setdefault(DOMAIN, {})[entry.entry_id] = client
    hass.http.register_view(DoorfastEventView(hass, entry.entry_id))
    async def poll(_now=None):
        try:
            await client.refresh()
            async_dispatcher_send(hass, f"{DOMAIN}_{entry.entry_id}_STATUS", client.status)
        except Exception:
            client.online = False
    await poll()
    entry.async_on_unload(asyncio.create_task(_poll_loop(hass, entry, poll)))
    async def unlock(call): await client.unlock(call.data.get("generation"))
    async def call_elevator(call): await client.call_elevator(call.data.get("direction", "up"))
    async def hangup(call): await client.hangup(call.data.get("generation"), call.data.get("reason", "ha"))
    for name, handler in (("unlock", unlock), ("call_elevator", call_elevator), ("hangup", hangup)):
        if not hass.services.has_service(DOMAIN, name): hass.services.async_register(DOMAIN, name, handler)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS); return True

async def _poll_loop(hass, entry, poll):
    while True:
        await asyncio.sleep(entry.data.get(CONF_POLL_INTERVAL, 5)); await poll()

async def async_unload_entry(hass, entry):
    hass.data[DOMAIN].pop(entry.entry_id, None); return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

class DoorfastEventView(HomeAssistantView):
    requires_auth = True
    def __init__(self, hass, entry_id):
        self.hass, self.entry_id = hass, entry_id; self.url = f"/api/doorfast/{entry_id}"; self.name = f"api:doorfast:{entry_id}"
    async def post(self, request: Request) -> Response:
        try: payload = await request.json()
        except Exception: return json_response({"error": "invalid JSON"}, status=400)
        client = self.hass.data[DOMAIN][self.entry_id]; payload["time"] = datetime.now().isoformat()
        client.status.update(payload); async_dispatcher_send(self.hass, f"{DOMAIN}_{self.entry_id}_{LATEST_EVENT}", payload)
        if payload.get("event") in {"ring", "incoming_call", "call"}: async_dispatcher_send(self.hass, f"{DOMAIN}_{self.entry_id}_{RING_STATUS}", True)
        return json_response({"status": "success"})
