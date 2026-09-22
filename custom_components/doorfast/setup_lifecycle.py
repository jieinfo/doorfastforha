"""Helpers for making config-entry setup failure-safe."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from .const import DOMAIN, PLATFORMS, STATIONS_KEY


async def sync_monitor_state(
    client: Any,
    monitor: Any,
    provider: Any,
    *,
    include_event: bool = False,
    station_id: str | None = None,
    relay_event: dict[str, Any] | None = None,
) -> None:
    """Apply authoritative monitor state before reconciling WebRTC sessions."""
    station_ids = getattr(monitor, "station_ids", None)
    monitor_for_station = getattr(monitor, "monitor", None)
    if station_ids is not None and callable(monitor_for_station):
        expected_runtime = client.status.get("runtime_id")
        before_poll = {
            current_station_id: (
                getattr(monitor_for_station(current_station_id), "generation", None),
                getattr(
                    monitor_for_station(current_station_id), "status_revision", 0
                ),
            )
            for current_station_id in station_ids
        }
        status = await client.monitor_status()
        if not isinstance(status, dict):
            raise ValueError("monitor status must be an object")
        reported_runtime = status.get("runtime_id", expected_runtime)
        sessions = status.get("sessions")
        if (
            not isinstance(expected_runtime, str)
            or reported_runtime != expected_runtime
            or not isinstance(sessions, list)
        ):
            await provider.async_reconcile_monitor()
            return
        by_station: dict[str, dict[str, Any]] = {}
        for session in sessions:
            if not isinstance(session, dict):
                continue
            session_station_id = session.get("station_id")
            if (
                not isinstance(session_station_id, str)
                or session_station_id not in station_ids
            ):
                continue
            item = dict(session)
            item["runtime_id"] = expected_runtime
            item["station_id"] = session_station_id
            by_station[session_station_id] = item
        target_station_ids = (
            (station_id,) if station_id in station_ids else station_ids
        )
        for current_station_id in target_station_ids:
            coordinator = monitor_for_station(current_station_id)
            item = by_station.get(current_station_id)
            if item is None:
                current_marker = (
                    getattr(coordinator, "generation", None),
                    getattr(coordinator, "status_revision", 0),
                )
                if current_marker != before_poll[current_station_id]:
                    continue
                revision = status.get("status_revision", 0)
                item = {
                    "runtime_id": expected_runtime,
                    "station_id": current_station_id,
                    "generation": 0,
                    "state": "idle",
                    "status_revision": (
                        revision
                        if isinstance(revision, int) and not isinstance(revision, bool)
                        else 0
                    ),
                }
            await coordinator.async_apply_status(item)
        if (
            station_id is not None
            and isinstance(relay_event, dict)
            and relay_event.get("station_id") == station_id
            and station_id in station_ids
        ):
            await monitor_for_station(station_id).async_apply_status(relay_event)
        await provider.async_reconcile_monitor()
        return

    """Compatibility path for direct monitor coordinator callers."""
    media = client.status.get("media")
    if isinstance(media, dict):
        await monitor.async_apply_status(media)
    if include_event:
        event = client.status.get("media_event")
        if isinstance(event, dict):
            await monitor.async_apply_status(event)
    await provider.async_reconcile_monitor()


async def rollback_entry_setup(
    hass: Any,
    entry: Any,
    *,
    pcm_ws: Any,
    unregister_frontend: Callable[[Any, str], Awaitable[None]],
    platforms_forward_attempted: bool,
    frontend_registered: bool,
    view_created: bool,
    service_names: Iterable[str],
    monitor: Any | None = None,
    station_registry: Any | None = None,
    provider: Any | None = None,
    unregister_webrtc: Callable[[], None] | None = None,
) -> None:
    """Undo resources acquired by a partially initialized config entry.

    Home Assistant does not guarantee that ``async_unload_entry`` is called
    when ``async_setup_entry`` raises.  Each cleanup step is therefore best
    effort and independent so a failed platform unload cannot strand the
    client, PCM capture owner, frontend reference, or service registrations.
    """

    if provider is not None:
        try:
            await provider.async_close_entry()
        except Exception:
            pass

    if unregister_webrtc is not None:
        try:
            unregister_webrtc()
        except Exception:
            pass

    if monitor is not None:
        try:
            await monitor.async_close()
        except Exception:
            pass

    if station_registry is not None:
        try:
            await station_registry.async_close()
        except Exception:
            pass

    for key in (
        f"{DOMAIN}_monitors",
        STATIONS_KEY,
        f"{DOMAIN}_webrtc_providers",
        f"{DOMAIN}_webrtc_unsubscribers",
    ):
        resources = hass.data.get(key)
        if isinstance(resources, dict):
            resources.pop(entry.entry_id, None)
            if not resources:
                hass.data.pop(key, None)

    if platforms_forward_attempted:
        try:
            await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        except Exception:
            pass

    if frontend_registered:
        try:
            await unregister_frontend(hass, entry.entry_id)
        except Exception:
            pass

    try:
        await pcm_ws.release_entry(entry.entry_id)
    except Exception:
        pass

    clients = hass.data.get(DOMAIN)
    if isinstance(clients, dict):
        clients.pop(entry.entry_id, None)
        if not clients:
            hass.data.pop(DOMAIN, None)
            services = getattr(hass, "services", None)
            if services is not None:
                for name in service_names:
                    try:
                        if services.has_service(DOMAIN, name):
                            services.async_remove(DOMAIN, name)
                    except Exception:
                        pass

    if view_created:
        views = hass.data.get(f"{DOMAIN}_event_views")
        if isinstance(views, dict):
            views.pop(entry.entry_id, None)
            if not views:
                hass.data.pop(f"{DOMAIN}_event_views", None)
