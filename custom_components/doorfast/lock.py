from homeassistant.components.lock import LockEntity
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers import entity_registry as er

from .access_status import access_attributes
from .const import CONTROLLER_NAME, DOMAIN, MANUFACTURER, SW_VERSION
from .station_entity import DoorfastStationEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([DoorfastLock(hass, entry)])
    registry = hass.data.get(f"{DOMAIN}_stations", {}).get(entry.entry_id)
    if registry is None:
        return
    entities = {}

    def add_station(station_id):
        entity = DoorfastStationLock(hass, entry, registry.station(station_id))
        entities[station_id] = entity
        async_add_entities([entity])

    def remove_station(station_id):
        entity = entities.pop(station_id, None)
        if entity is None:
            return
        entity._active = False
        entity.async_write_ha_state()
        entity_registry = er.async_get(hass)
        entity_id = entity_registry.async_get_entity_id("lock", DOMAIN, entity.unique_id)
        if entity_id is not None:
            entity_registry.async_remove(entity_id)

    def listener(event, station_id):
        if event == "added":
            add_station(station_id)
        elif event == "removed":
            remove_station(station_id)
        elif event == "updated" and station_id in entities:
            entities[station_id].station = registry.station(station_id)
            entities[station_id].async_write_ha_state()

    for station_id in registry.station_ids:
        add_station(station_id)
    entry.async_on_unload(registry.add_listener(listener))


class DoorfastLock(LockEntity):
    """Momentary Doorfast unlock control with protocol-result attributes."""

    _attr_translation_key = "unlock"
    _attr_assumed_state = True
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry
        self.client = hass.data[DOMAIN][entry.entry_id]
        self._attrs = access_attributes(self.client.status)

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self.entry.entry_id}_unlock"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self.entry.entry_id)},
            "name": CONTROLLER_NAME,
            "manufacturer": MANUFACTURER,
            "sw_version": SW_VERSION,
        }

    @property
    def is_locked(self):
        # Doorfast currently confirms the protocol reply, not the door position.
        # Treat unlock as a momentary pulse and never claim a persistent open state.
        return True

    @property
    def available(self):
        """Keep the legacy call-scoped control inert when no station is ringing."""
        call = self.client.status.get("call")
        return (
            self.client.online
            and isinstance(call, dict)
            and isinstance(call.get("station_id"), str)
        )

    @property
    def extra_state_attributes(self):
        return self._attrs

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_{self.entry.entry_id}_STATUS",
                self._handle_status,
            )
        )

    @callback
    def _handle_status(self, data):
        self._attrs = access_attributes(data)
        self.async_write_ha_state()

    async def async_lock(self, **kwargs):
        self.async_write_ha_state()

    async def async_unlock(self, **kwargs):
        await self.client.unlock()
        self.async_write_ha_state()


class DoorfastStationLock(LockEntity, DoorfastStationEntity):
    _attr_translation_key = "unlock"
    _attr_assumed_state = True
    _attr_should_poll = False

    def __init__(self, hass, entry, station):
        LockEntity.__init__(self)
        DoorfastStationEntity.__init__(self, entry.entry_id, station)
        self.hass = hass
        self.client = hass.data[DOMAIN][entry.entry_id]
        self._active = True

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self.entry_id}_station_{self.station.station_id}_unlock"

    @property
    def available(self):
        return self._active and self.station.enabled and self.client.online

    @property
    def is_locked(self):
        return True

    async def async_lock(self, **kwargs):
        self.async_write_ha_state()

    async def async_unlock(self, **kwargs):
        await self.client.unlock_station(self.station.station_id)
        self.async_write_ha_state()
