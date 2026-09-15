"""Serve and register the bundled Doorfast dashboard card."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from homeassistant.components.frontend import add_extra_js_url, remove_extra_js_url
from homeassistant.components.http import StaticPathConfig

from .const import SW_VERSION

_DATA_KEY = "doorfast_frontend"
_STATIC_URL = "/doorfast_static"
CARD_URL = f"{_STATIC_URL}/doorfast-ptt-card.mjs?v={SW_VERSION}"


def _state(hass: Any) -> dict[str, Any]:
    return hass.data.setdefault(
        _DATA_KEY,
        {"lock": asyncio.Lock(), "static_registered": False, "entries": set()},
    )


async def async_register_frontend(hass: Any, entry_id: str) -> None:
    """Register static files once and retain the module while entries are loaded."""
    state = _state(hass)
    async with state["lock"]:
        if not state["static_registered"]:
            await hass.http.async_register_static_paths(
                [StaticPathConfig(_STATIC_URL, str(Path(__file__).parent / "frontend"), True)]
            )
            state["static_registered"] = True
        if entry_id in state["entries"]:
            return
        if not state["entries"]:
            add_extra_js_url(hass, CARD_URL)
        state["entries"].add(entry_id)


async def async_unregister_frontend(hass: Any, entry_id: str) -> None:
    """Release a loaded config entry's reference to the dashboard module."""
    state = _state(hass)
    async with state["lock"]:
        if entry_id not in state["entries"]:
            return
        state["entries"].discard(entry_id)
        if not state["entries"]:
            remove_extra_js_url(hass, CARD_URL)
