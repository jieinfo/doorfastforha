"""Pure helpers for Doorfast configuration."""

from __future__ import annotations

from typing import Any


BRIDGE_PATH = "/cgi-bin/doorfast"


def normalize_bridge_url(address: str) -> str:
    """Return the canonical Doorfast HTTP bridge base URL."""
    normalized = address.rstrip("/")
    if not normalized.endswith(BRIDGE_PATH):
        normalized += BRIDGE_PATH
    return normalized


def is_doorfast_status(payload: dict[str, Any]) -> bool:
    """Return whether a response identifies a running Doorfast bridge."""
    return payload.get("running") is True
