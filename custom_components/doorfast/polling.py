"""Resilient polling boundaries for the Doorfast integration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


async def async_poll(
    client: Any,
    refresh_client: Callable[[], Awaitable[None]],
    refresh_stations: Callable[[], Awaitable[None]],
    sync_monitor: Callable[[], Awaitable[None]],
    dispatch_status: Callable[[], Awaitable[None]],
    logger: Any = None,
) -> None:
    """Refresh the bridge without turning secondary sync failures into offline.

    The bridge status request is the connectivity probe. Station discovery,
    monitor reconciliation, and HA event dispatch are independent consumers;
    a transient failure in one must not make station controls unavailable.
    """

    try:
        await refresh_client()
    except Exception:
        client.online = False
        return

    for operation in (refresh_stations, sync_monitor, dispatch_status):
        try:
            await operation()
        except Exception:
            if logger is not None:
                logger.debug("Doorfast secondary poll operation failed", exc_info=True)
