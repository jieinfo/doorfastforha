"""Shared base for entities belonging to a configured Doorfast station."""

from __future__ import annotations

from homeassistant.helpers.entity import Entity

from .client_types import DoorfastStation
from .const import DOMAIN, MANUFACTURER


class DoorfastStationEntity(Entity):
    """Expose stable child-device identity for one configured station."""

    _attr_has_entity_name = True

    def __init__(self, entry_id: str, station: DoorfastStation) -> None:
        self.entry_id = entry_id
        self.station = station

    def async_write_station_state(self) -> None:
        """Disabled/aborted entities remain tracked but are not attached to HA."""
        if getattr(self, "hass", None) is not None and getattr(self, "entity_id", None) is not None:
            self.async_write_ha_state()

    @property
    def device_info(self) -> dict:
        info = {
            "identifiers": {
                (DOMAIN, f"{self.entry_id}_station_{self.station.station_id}")
            },
            "name": self.station.name,
            "manufacturer": MANUFACTURER,
        }
        if (hass := getattr(self, "hass", None)) is not None:
            from homeassistant.helpers import device_registry as dr

            registry = dr.async_get(hass)
            identifier = (DOMAIN, self.entry_id)
            if (lookup := getattr(registry, "async_get_device_by_identifier", None)) is not None:
                controller = lookup(identifier, self.entry_id)
                if controller is not None:
                    info["via_device_id"] = controller.id
            else:
                # HA before 2026.8 only accepts the identifier-based link.
                info["via_device"] = identifier
        return info
