"""Helpers for binding commands to the current Doorfast call."""

from __future__ import annotations

from typing import Any


def resolve_generation(status: dict[str, Any], generation: int | None) -> int:
    """Return an explicit or currently active positive call generation."""
    candidate: Any = generation
    if candidate is None:
        call = status.get("call")
        if isinstance(call, dict):
            candidate = call.get("generation")

    if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate <= 0:
        raise ValueError("Doorfast has no active call generation")
    return candidate


def is_ringing(status: dict[str, Any]) -> bool:
    """Return whether Doorfast reports an incoming call that is still ringing."""
    call = status.get("call")
    return isinstance(call, dict) and call.get("session") == "ringing"
