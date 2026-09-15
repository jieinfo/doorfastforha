"""Helpers for making config-entry setup failure-safe."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from .const import DOMAIN, PLATFORMS


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
) -> None:
    """Undo resources acquired by a partially initialized config entry.

    Home Assistant does not guarantee that ``async_unload_entry`` is called
    when ``async_setup_entry`` raises.  Each cleanup step is therefore best
    effort and independent so a failed platform unload cannot strand the
    client, PCM capture owner, frontend reference, or service registrations.
    """

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
