"""Route Home Assistant service calls to a Doorfast config entry."""

from __future__ import annotations

from typing import Any


def select_client(clients: dict[str, Any], entry_id: str | None) -> Any:
    """Select an explicit client, or the sole configured client."""
    if entry_id is not None:
        if entry_id not in clients:
            raise ValueError("Unknown Doorfast config entry")
        return clients[entry_id]
    if len(clients) == 1:
        return next(iter(clients.values()))
    if not clients:
        raise ValueError("No Doorfast config entry is loaded")
    raise ValueError("config_entry_id is required when multiple Doorfast entries are loaded")
