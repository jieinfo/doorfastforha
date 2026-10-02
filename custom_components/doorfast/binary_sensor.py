"""Doorfast controller and station binary sensors."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, MANUFACTURER, RING_STATUS, STATIONS_KEY, SW_VERSION
from .generation import is_ringing
from .station_entity import DoorfastStationEntity


async def async_setup_entry(hass, entry, async_add_entities):
    registry = hass.data[STATIONS_KEY][entry.entry_id]
    reachability = {}

    def add_station(station_id):
        entity = DoorfastStationReachability(
            registry.client, entry.entry_id, registry.station(station_id)
        )
        reachability[station_id] = entity
        async_add_entities([entity])

    def remove_station(station_id):
        entity = reachability.pop(station_id, None)
        if entity is None:
            return
        entity.mark_removed()
        entity_registry = er.async_get(hass)
        entity_id = entity_registry.async_get_entity_id(
            "binary_sensor", DOMAIN, entity.unique_id
        )
        if entity_id is not None:
            entity_registry.async_remove(entity_id)
        hass.async_create_task(entity.async_remove(force_remove=True))

    def handle_station(event, station_id):
        if event == "added":
            add_station(station_id)
        elif event == "updated":
            entity = reachability.get(station_id)
            if entity is not None:
                entity.update_station(registry.station(station_id))
        elif event == "removed":
            remove_station(station_id)

    async_add_entities([IncomingCall(hass, entry)])
    for station_id in registry.station_ids:
        add_station(station_id)
    entry.async_on_unload(registry.add_listener(handle_station))


class IncomingCall(BinarySensorEntity):
    _attr_translation_key = RING_STATUS
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_should_poll = False

    def __init__(self, hass, entry):
        self.hass = hass
        self.e = entry
        self._on = is_ringing(hass.data[DOMAIN][entry.entry_id].status)

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self.e.entry_id}_{RING_STATUS}"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self.e.entry_id)},
            "name": "Doorfast Controller",
            "manufacturer": MANUFACTURER,
            "sw_version": SW_VERSION,
        }

    @property
    def is_on(self):
        return self._on

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_{self.e.entry_id}_{RING_STATUS}",
                self.trigger,
            )
        )

    @callback
    def trigger(self, state=True):
        self._on = bool(state)
        self.async_write_ha_state()


class DoorfastStationReachability(BinarySensorEntity, DoorfastStationEntity):
    """Whether a configured station currently has a usable route."""

    _attr_translation_key = "station_reachable"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_should_poll = False

    def __init__(self, client, entry_id, station):
        BinarySensorEntity.__init__(self)
        DoorfastStationEntity.__init__(self, entry_id, station)
        self.client = client
        self._active = True

    @property
    def unique_id(self):
        return (
            f"{DOMAIN}_{self.entry_id}_station_"
            f"{self.station.station_id}_reachable"
        )

    @property
    def available(self):
        return self._active and self.client.online

    @property
    def is_on(self):
        return self.station.reachable

    def update_station(self, station):
        self.station = station
        self.async_write_ha_state()

    def mark_removed(self):
        self._active = False
        self.async_write_ha_state()
