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
from .const import DOMAIN, PLATFORMS, CONF_SERVER_ADDRESS, CONF_POLL_INTERVAL, LATEST_EVENT, RING_STATUS, DEFAULT_AUDIO_PORT, DEFAULT_CALL_DURATION, DEFAULT_VIDEO_PORT
from .generation import is_ringing
from .events import EventGate, process_event
from .routing import select_client
from .websocket import PcmWebSocketManager

SERVICE_NAMES = ("unlock", "call_elevator", "answer", "hangup")

def service_client(hass, call):
    try:
        return select_client(hass.data.get(DOMAIN, {}), call.data.get("config_entry_id"))
    except ValueError as error:
        raise HomeAssistantError(str(error)) from error

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    pcm_ws = hass.data.setdefault(f"{DOMAIN}_pcm_ws", PcmWebSocketManager(hass))
    pcm_ws.register()
    client = DoorfastClient(hass, entry.data[CONF_SERVER_ADDRESS]); hass.data.setdefault(DOMAIN, {})[entry.entry_id] = client
    event_gate = EventGate()
    views = hass.data.setdefault(f"{DOMAIN}_event_views", {})
    view = views.get(entry.entry_id)
    if view is None:
        view = DoorfastEventView(hass, entry.entry_id, event_gate)
        views[entry.entry_id] = view
        hass.http.register_view(view)
    else:
        view.event_gate = event_gate
    async def dispatch_status() -> None:
        async_dispatcher_send(hass, f"{DOMAIN}_{entry.entry_id}_STATUS", client.status)
        async_dispatcher_send(hass, f"{DOMAIN}_{entry.entry_id}_{RING_STATUS}", is_ringing(client.status))
        await pcm_ws.reconcile(entry.entry_id, client.status)
    async def poll(_now=None):
        try:
            await client.refresh()
            await dispatch_status()
        except Exception:
            client.online = False
    await poll()
    entry.async_on_unload(async_track_time_interval(hass, poll, timedelta(seconds=entry.data.get(CONF_POLL_INTERVAL, 5))))
    async def unlock(call): await service_client(hass, call).unlock(call.data.get("generation"))
    async def call_elevator(call): await service_client(hass, call).call_elevator(call.data.get("direction", "up"))
    async def answer(call):
        await service_client(hass, call).answer(call.data.get("generation"), call.data.get("primary_media_port", DEFAULT_VIDEO_PORT), call.data.get("secondary_media_port", DEFAULT_AUDIO_PORT), call.data.get("duration_seconds", DEFAULT_CALL_DURATION))
    async def hangup(call):
        entry_id = call.data.get("config_entry_id")
        if isinstance(entry_id, str):
            await pcm_ws.release_entry(entry_id)
        await service_client(hass, call).hangup(call.data.get("generation"), call.data.get("reason", "ha"))
    for name, handler in (("unlock", unlock), ("call_elevator", call_elevator), ("answer", answer), ("hangup", hangup)):
        if not hass.services.has_service(DOMAIN, name): hass.services.async_register(DOMAIN, name, handler)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS); return True

async def async_unload_entry(hass, entry):
    pcm_ws = hass.data.get(f"{DOMAIN}_pcm_ws")
    if pcm_ws is not None:
        await pcm_ws.release_entry(entry.entry_id)
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            for name in SERVICE_NAMES:
                hass.services.async_remove(DOMAIN, name)
    return unloaded

class DoorfastEventView(HomeAssistantView):
    requires_auth = True
    def __init__(self, hass, entry_id, event_gate: EventGate):
        self.hass, self.entry_id, self.event_gate = hass, entry_id, event_gate
        self.url = f"/api/doorfast/{entry_id}"; self.name = f"api:doorfast:{entry_id}"

    async def post(self, request: Request) -> Response:
        try:
            payload = await request.json()
        except Exception:
            return json_response({"error": "invalid JSON"}, status=400)
        client = self.hass.data.get(DOMAIN, {}).get(self.entry_id)
        if client is None:
            return json_response({"error": "unknown Doorfast entry"}, status=404)
        def dispatch(kind, data):
            channels = {
                "status": f"{DOMAIN}_{self.entry_id}_STATUS",
                "latest_event": f"{DOMAIN}_{self.entry_id}_{LATEST_EVENT}",
                "ring_status": f"{DOMAIN}_{self.entry_id}_{RING_STATUS}",
            }
            async_dispatcher_send(self.hass, channels[kind], data)
        status, result = await process_event(payload, client, self.event_gate, dispatch, is_ringing)
        if payload.get("event") in {"hangup", "call_ended"}:
            await self.hass.data.get(f"{DOMAIN}_pcm_ws", PcmWebSocketManager(self.hass)).release_entry(self.entry_id)
        return json_response(result, status=status)
