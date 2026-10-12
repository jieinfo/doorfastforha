"""Translate Doorfast access-control status for Home Assistant."""

from __future__ import annotations

from typing import Any
import re


ACCESS_FIELDS = (
    "configured",
    "state",
    "generation",
    "raw_status",
    "physical_result_confirmed",
    "station_id",
    "manual_state",
    "manual_station_id",
)

ACCESS_STATES = (
    "idle", "waiting", "protocol_completed", "protocol_rejected",
    "expired", "cancelled", "send_failed",
)
MANUAL_STATES = ("idle", "waiting_media", "waiting_reply")


def _valid_field(field: str, value: Any) -> bool:
    if field in ("station_id", "manual_station_id"):
        return isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", value) is not None
    if field == "state":
        return isinstance(value, str) and value in ACCESS_STATES
    if field == "manual_state":
        return isinstance(value, str) and value in MANUAL_STATES
    if field in ("configured", "physical_result_confirmed"):
        return isinstance(value, bool)
    maximum = 255 if field == "raw_status" else (1 << 64) - 1
    return type(value) is int and 0 <= value <= maximum


def access_attributes(status: dict[str, Any]) -> dict[str, Any]:
    """Return the supported, non-sensitive access-control status fields."""
    access = status.get("access")
    if not isinstance(access, dict):
        return {}
    return {
        field: access[field] for field in ACCESS_FIELDS
        if field in access and _valid_field(field, access[field])
    }


UNLOCK_PHASES = (
    "idle", "waiting_media", "waiting_reply", "protocol_completed",
    "protocol_rejected", "expired", "cancelled", "send_failed",
)


def station_access_attributes(status: dict[str, Any], station_id: str) -> dict[str, Any]:
    """Scope progress and the latest protocol transaction to its actual target."""
    access = access_attributes(status)
    attributes: dict[str, Any] = {"phase": None}
    if "configured" in access:
        attributes["configured"] = access["configured"]
    manual = access.get("manual_state")
    if access.get("manual_station_id") == station_id and manual in (
        "waiting_media", "waiting_reply",
    ):
        attributes.update(phase=manual, manual_state=manual)
        # While obtaining media, access.result can still refer to an old
        # transaction, even for this station. Do not display it as this request.
        if manual == "waiting_media":
            return attributes
    target = access.get("station_id")
    if target == station_id:
        attributes.update(access)
        state = access.get("state")
        attributes["phase"] = "waiting_reply" if state == "waiting" else state
        if manual == "waiting_reply" and access.get("manual_station_id") == station_id:
            attributes["phase"] = "waiting_reply"
    elif attributes["phase"] is None:
        if target is not None or access.get("state") == "idle":
            attributes["phase"] = "idle"
    return attributes
