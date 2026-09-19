"""Shared base for entities belonging to a configured Doorfast station."""

from __future__ import annotations

from homeassistant.helpers.entity import Entity

from .client_types import DoorfastStation
from .const import DOMAIN, MANUFACTURER


class DoorfastStationEntity(Entity):
    """Expose stable child-device identity for one configured station."""

    def __init__(self, entry_id: str, station: DoorfastStation) -> None:
        self.entry_id = entry_id
        self.station = station

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self.entry_id, self.station.station_id)},
            "name": self.station.name,
            "manufacturer": MANUFACTURER,
            "via_device": (DOMAIN, self.entry_id),
        }

