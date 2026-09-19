from __future__ import annotations

import logging
from datetime import timedelta

from aiohttp.web import Request, Response, json_response
from homeassistant.components.camera import async_register_webrtc_provider
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .client import DoorfastClient
from .const import (
    CONF_POLL_INTERVAL,
    CONF_SERVER_ADDRESS,
    DEFAULT_AUDIO_PORT,
    DEFAULT_CALL_DURATION,
    DEFAULT_VIDEO_PORT,
    DOMAIN,
    LATEST_EVENT,
    MONITORS_KEY,
    MONITOR_STATUS,
    PLATFORMS,
    RING_STATUS,
    STATIONS_KEY,
    WEBRTC_PROVIDERS_KEY,
    WEBRTC_UNSUBS_KEY,
)
from .events import EventGate, process_event
from .frontend import async_register_frontend, async_unregister_frontend
from .generation import is_ringing
from .monitor import DoorfastMonitorCoordinator
from .routing import select_client
from .setup_lifecycle import rollback_entry_setup, sync_monitor_state
from .stations import StationRegistryCoordinator
from .webrtc import DoorfastWebRTCProvider
from .websocket import PcmWebSocketManager


SERVICE_NAMES = (
    "unlock",
    "call_elevator",
    "answer",
    "hangup",
    "start_monitor",
    "stop_monitor",
)
_LOGGER = logging.getLogger(__name__)


def _service_resource(hass, key, call):
    try:
        return select_client(
            hass.data.get(key, {}), call.data.get("config_entry_id")
        )
    except ValueError as error:
        raise HomeAssistantError(str(error)) from error


def service_client(hass, call):
    return _service_resource(hass, DOMAIN, call)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    pcm_ws = hass.data.setdefault(f"{DOMAIN}_pcm_ws", PcmWebSocketManager(hass))
    pcm_ws.register()

    clients = hass.data.setdefault(DOMAIN, {})
    client = DoorfastClient(hass, entry.data[CONF_SERVER_ADDRESS])
    clients[entry.entry_id] = client

    station_registries = hass.data.setdefault(STATIONS_KEY, {})
    station_registry = StationRegistryCoordinator(client, entry_id=entry.entry_id)
    station_registries[entry.entry_id] = station_registry

    monitors = hass.data.setdefault(MONITORS_KEY, {})
    monitor = DoorfastMonitorCoordinator(client)
    monitors[entry.entry_id] = monitor

    providers = hass.data.setdefault(WEBRTC_PROVIDERS_KEY, {})
    provider = DoorfastWebRTCProvider(hass, entry.entry_id, monitor)
    providers[entry.entry_id] = provider
    unregister_webrtc = None

    event_gate = EventGate()
    views = hass.data.setdefault(f"{DOMAIN}_event_views", {})
    existing_view = views.get(entry.entry_id)
    previous_event_gate = getattr(existing_view, "event_gate", None)
    previous_monitor = getattr(existing_view, "monitor", None)
    previous_provider = getattr(existing_view, "provider", None)
    view_created = False
    platforms_forward_attempted = False
    frontend_registration_attempted = False

    async def sync_current_monitor(*, include_event: bool = False) -> None:
        await sync_monitor_state(
            client, monitor, provider, include_event=include_event
        )
        async_dispatcher_send(
            hass,
            f"{DOMAIN}_{entry.entry_id}_{MONITOR_STATUS}",
            monitor.snapshot,
        )

    async def dispatch_status() -> None:
        async_dispatcher_send(
            hass, f"{DOMAIN}_{entry.entry_id}_STATUS", client.status
        )
        async_dispatcher_send(
            hass,
            f"{DOMAIN}_{entry.entry_id}_{RING_STATUS}",
            is_ringing(client.status),
        )
        await pcm_ws.reconcile(entry.entry_id, client.status)

    async def poll(_now=None):
        try:
            await client.refresh()
            await station_registry.async_refresh()
            await sync_current_monitor()
            await dispatch_status()
        except Exception:
            client.online = False

    async def unlock(call):
        await service_client(hass, call).unlock(call.data.get("generation"))

    async def call_elevator(call):
        await service_client(hass, call).call_elevator(
            call.data.get("direction", "up")
        )

    async def answer(call):
        await service_client(hass, call).answer(
            call.data.get("generation"),
            call.data.get("primary_media_port", DEFAULT_VIDEO_PORT),
            call.data.get("secondary_media_port", DEFAULT_AUDIO_PORT),
            call.data.get("duration_seconds", DEFAULT_CALL_DURATION),
        )

    async def hangup(call):
        entry_id = call.data.get("config_entry_id")
        if isinstance(entry_id, str):
            await pcm_ws.release_entry(entry_id)
        await service_client(hass, call).hangup(
            call.data.get("generation"), call.data.get("reason", "ha")
        )

    async def start_monitor(call):
        selected = _service_resource(hass, MONITORS_KEY, call)
        await selected.async_start()

    async def stop_monitor(call):
        selected = _service_resource(hass, MONITORS_KEY, call)
        await selected.async_stop()

    try:
        await poll()
        platforms_forward_attempted = True
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        unregister_webrtc = async_register_webrtc_provider(hass, provider)
        hass.data.setdefault(WEBRTC_UNSUBS_KEY, {})[
            entry.entry_id
        ] = unregister_webrtc

        frontend_registration_attempted = True
        await async_register_frontend(hass, entry.entry_id)

        view = existing_view
        if view is None:
            view = DoorfastEventView(
                hass, entry.entry_id, event_gate, monitor, provider
            )
            views[entry.entry_id] = view
            view_created = True
            hass.http.register_view(view)
        else:
            view.event_gate = event_gate
            view.monitor = monitor
            view.provider = provider

        handlers = (
            ("unlock", unlock),
            ("call_elevator", call_elevator),
            ("answer", answer),
            ("hangup", hangup),
            ("start_monitor", start_monitor),
            ("stop_monitor", stop_monitor),
        )
        for name, handler in handlers:
            if not hass.services.has_service(DOMAIN, name):
                hass.services.async_register(DOMAIN, name, handler)

        entry.async_on_unload(
            async_track_time_interval(
                hass,
                poll,
                timedelta(seconds=entry.data.get(CONF_POLL_INTERVAL, 5)),
            )
        )
        _LOGGER.info(
            "Doorfast configured: config entry ID=%s; relay endpoint=/api/doorfast/%s",
            entry.entry_id, entry.entry_id,
        )
        return True
    except Exception:
        if existing_view is not None:
            existing_view.event_gate = previous_event_gate
            existing_view.monitor = previous_monitor
            existing_view.provider = previous_provider
        await rollback_entry_setup(
            hass,
            entry,
            pcm_ws=pcm_ws,
            unregister_frontend=async_unregister_frontend,
            platforms_forward_attempted=platforms_forward_attempted,
            frontend_registered=frontend_registration_attempted,
            view_created=view_created,
            service_names=SERVICE_NAMES,
            monitor=monitor,
            station_registry=station_registry,
            provider=provider,
            unregister_webrtc=unregister_webrtc,
        )
        raise


async def async_unload_entry(hass, entry):
    pcm_ws = hass.data.get(f"{DOMAIN}_pcm_ws")
    if pcm_ws is not None:
        await pcm_ws.release_entry(entry.entry_id)

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded:
        return False

    await async_unregister_frontend(hass, entry.entry_id)

    providers = hass.data.get(WEBRTC_PROVIDERS_KEY, {})
    provider = providers.pop(entry.entry_id, None)
    if provider is not None:
        await provider.async_close_entry()
    if not providers:
        hass.data.pop(WEBRTC_PROVIDERS_KEY, None)

    unregisters = hass.data.get(WEBRTC_UNSUBS_KEY, {})
    unregister_webrtc = unregisters.pop(entry.entry_id, None)
    if unregister_webrtc is not None:
        unregister_webrtc()
    if not unregisters:
        hass.data.pop(WEBRTC_UNSUBS_KEY, None)

    station_registries = hass.data.get(STATIONS_KEY, {})
    station_registry = station_registries.pop(entry.entry_id, None)
    if station_registry is not None:
        await station_registry.async_close()
    if not station_registries:
        hass.data.pop(STATIONS_KEY, None)

    monitors = hass.data.get(MONITORS_KEY, {})
    monitor = monitors.pop(entry.entry_id, None)
    if monitor is not None:
        await monitor.async_close()
    if not monitors:
        hass.data.pop(MONITORS_KEY, None)

    clients = hass.data.get(DOMAIN, {})
    clients.pop(entry.entry_id, None)
    if not clients:
        hass.data.pop(DOMAIN, None)
        for name in SERVICE_NAMES:
            if hass.services.has_service(DOMAIN, name):
                hass.services.async_remove(DOMAIN, name)
    return True


class DoorfastEventView(HomeAssistantView):
    requires_auth = True

    def __init__(
        self, hass, entry_id, event_gate: EventGate, monitor, provider
    ):
        self.hass = hass
        self.entry_id = entry_id
        self.event_gate = event_gate
        self.monitor = monitor
        self.provider = provider
        self.url = f"/api/doorfast/{entry_id}"
        self.name = f"api:doorfast:{entry_id}"

    async def post(self, request: Request) -> Response:
        try:
            payload = await request.json()
        except Exception:
            return json_response({"error": "invalid JSON"}, status=400)
        client = self.hass.data.get(DOMAIN, {}).get(self.entry_id)
        if client is None:
            return json_response(
                {"error": "unknown Doorfast entry"}, status=404
            )

        def dispatch(kind, data):
            channels = {
                "status": f"{DOMAIN}_{self.entry_id}_STATUS",
                "latest_event": f"{DOMAIN}_{self.entry_id}_{LATEST_EVENT}",
                "ring_status": f"{DOMAIN}_{self.entry_id}_{RING_STATUS}",
            }
            async_dispatcher_send(self.hass, channels[kind], data)

        status, result = await process_event(
            payload, client, self.event_gate, dispatch, is_ringing
        )
        if status in {200, 202}:
            await sync_monitor_state(
                client,
                self.monitor,
                self.provider,
                include_event=status == 200,
            )
            async_dispatcher_send(
                self.hass,
                f"{DOMAIN}_{self.entry_id}_{MONITOR_STATUS}",
                self.monitor.snapshot,
            )
        if payload.get("event") in {"hangup", "call_ended"}:
            manager = self.hass.data.get(f"{DOMAIN}_pcm_ws")
            if manager is not None:
                await manager.release_entry(self.entry_id)
        return json_response(result, status=status)
