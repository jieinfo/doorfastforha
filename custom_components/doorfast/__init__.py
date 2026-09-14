from __future__ import annotations
import asyncio
from datetime import datetime
from aiohttp.web import Request, Response, json_response
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.components.http import HomeAssistantView
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from datetime import timedelta
from .client import DoorfastClient
from .const import DOMAIN, PLATFORMS, CONF_SERVER_ADDRESS, CONF_POLL_INTERVAL, LATEST_EVENT, RING_STATUS
from .generation import is_ringing
from .routing import select_client

SERVICE_NAMES = ("unlock", "call_elevator", "answer", "hangup")

def service_client(hass, call):
    try:
        return select_client(hass.data.get(DOMAIN, {}), call.data.get("config_entry_id"))
    except ValueError as error:
        raise HomeAssistantError(str(error)) from error

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    client = DoorfastClient(hass, entry.data[CONF_SERVER_ADDRESS]); hass.data.setdefault(DOMAIN, {})[entry.entry_id] = client
    hass.http.register_view(DoorfastEventView(hass, entry.entry_id))
    async def poll(_now=None):
        try:
            await client.refresh()
            async_dispatcher_send(hass, f"{DOMAIN}_{entry.entry_id}_STATUS", client.status)
            async_dispatcher_send(hass, f"{DOMAIN}_{entry.entry_id}_{RING_STATUS}", is_ringing(client.status))
        except Exception:
            client.online = False
    await poll()
    entry.async_on_unload(async_track_time_interval(hass, poll, timedelta(seconds=entry.data.get(CONF_POLL_INTERVAL, 5))))
    async def unlock(call): await service_client(hass, call).unlock(call.data.get("generation"))
    async def call_elevator(call): await service_client(hass, call).call_elevator(call.data.get("direction", "up"))
    async def answer(call):
        await service_client(hass, call).answer(call.data["generation"], call.data.get("primary_media_port", 0), call.data.get("secondary_media_port", 0), call.data.get("duration_seconds", 0))
    async def hangup(call): await service_client(hass, call).hangup(call.data.get("generation"), call.data.get("reason", "ha"))
    for name, handler in (("unlock", unlock), ("call_elevator", call_elevator), ("answer", answer), ("hangup", hangup)):
        if not hass.services.has_service(DOMAIN, name): hass.services.async_register(DOMAIN, name, handler)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS); return True

async def async_unload_entry(hass, entry):
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            for name in SERVICE_NAMES:
                hass.services.async_remove(DOMAIN, name)
    return unloaded

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
        elif payload.get("event") in {"hangup", "ended", "call_ended"}: async_dispatcher_send(self.hass, f"{DOMAIN}_{self.entry_id}_{RING_STATUS}", False)
        return json_response({"status": "success"})
