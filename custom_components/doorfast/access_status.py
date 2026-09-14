"""Translate Doorfast access-control status for Home Assistant."""

from __future__ import annotations

from typing import Any


ACCESS_FIELDS = (
    "configured",
    "state",
    "generation",
    "raw_status",
    "physical_result_confirmed",
)


def access_attributes(status: dict[str, Any]) -> dict[str, Any]:
    """Return the supported, non-sensitive access-control status fields."""
    access = status.get("access")
    if not isinstance(access, dict):
        return {}
    return {field: access[field] for field in ACCESS_FIELDS if field in access}
