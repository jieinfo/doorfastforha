from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from .access_status import UNLOCK_PHASES, station_access_attributes
from .const import CONTROLLER_NAME, DOMAIN, LATEST_EVENT, MANUFACTURER, STATIONS_KEY, SW_VERSION
from .station_entity import DoorfastStationEntity
from .station_status import PREVIEW_PHASES, preview_attributes
from .session_diagnostics import call_diagnostics

async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([DoorfastStatus(hass, entry)])
    registry = hass.data[STATIONS_KEY][entry.entry_id]
    sensors = {}

    def add_station(station_id):
        entities = [
            entity_type(registry.client, entry.entry_id, registry.station(station_id))
            for entity_type in (DoorfastPreviewStatus, DoorfastUnlockStatus)
        ]
        sensors[station_id] = entities
        async_add_entities(entities)

    def remove_station(station_id):
        entity_registry = er.async_get(hass)
        for entity in sensors.pop(station_id, []):
            entity.mark_removed()
            entity_id = entity_registry.async_get_entity_id("sensor", DOMAIN, entity.unique_id)
            if entity_id is not None:
                entity_registry.async_remove(entity_id)
            if entity.hass is not None and entity.entity_id is not None:
                hass.async_create_task(entity.async_remove(force_remove=True))

    def handle_station(event, station_id):
        if event == "added":
            add_station(station_id)
        elif event == "removed":
            remove_station(station_id)
        elif event == "updated":
            for entity in sensors.get(station_id, []):
                entity.update_station(registry.station(station_id))

    for station_id in registry.station_ids:
        add_station(station_id)
    entry.async_on_unload(registry.add_listener(handle_station))

class DoorfastStatus(SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = LATEST_EVENT
    _attr_should_poll = False
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.client = hass.data[DOMAIN][entry.entry_id]
        self._state, self._attrs = "unknown", {}
    @property
    def unique_id(self): return f"{DOMAIN}_{self.entry.entry_id}_{LATEST_EVENT}"
    @property
    def device_info(self): return {"identifiers": {(DOMAIN,self.entry.entry_id)},"name":CONTROLLER_NAME,"manufacturer":MANUFACTURER,"sw_version":SW_VERSION}
    @property
    def native_value(self): return self._state
    @property
    def available(self): return self.client.online
    @property
    def extra_state_attributes(self): return self._attrs
    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(self.hass, f"{DOMAIN}_{self.entry.entry_id}_STATUS", self._handle_status))
        self._handle_status(self.client.status)
    @callback
    def _handle_status(self, data):
        self._attrs = {**data, "latest_audio_url": self.client.latest_audio_url,
                       **call_diagnostics(data)}
        call = data.get("call")
        self._state = (call.get("session", data.get("event", "unknown"))
                       if isinstance(call, dict) else data.get("event", "unknown"))
        self.async_write_ha_state()


class DoorfastStationStatus(SensorEntity, DoorfastStationEntity):
    """Read-only station feedback; never starts/stops a media session."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_should_poll = False

    def __init__(self, client, entry_id, station):
        SensorEntity.__init__(self)
        DoorfastStationEntity.__init__(self, entry_id, station)
        self.client = client
        self._active = True

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self.entry_id}_station_{self.station.station_id}_{self._attr_translation_key}"

    @property
    def available(self):
        return self._active and self.station.enabled and self.client.online

    @property
    def native_value(self):
        return self.extra_state_attributes["phase"]

    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(
            self.hass, f"{DOMAIN}_{self.entry_id}_STATUS", self._handle_status,
        ))
        self._handle_status(self.client.status)

    @callback
    def _handle_status(self, _data):
        self.async_write_station_state()

    def update_station(self, station):
        self.station = station
        self.async_write_station_state()

    def mark_removed(self):
        self._active = False
        self.async_write_station_state()


class DoorfastPreviewStatus(DoorfastStationStatus):
    _attr_translation_key = "preview_status"
    _attr_options = list(PREVIEW_PHASES)

    @property
    def extra_state_attributes(self):
        return preview_attributes(
            self.client.status if self.available else {}, self.station.station_id,
        )


class DoorfastUnlockStatus(DoorfastStationStatus):
    _attr_translation_key = "unlock_status"
    _attr_options = list(UNLOCK_PHASES)

    @property
    def extra_state_attributes(self):
        return station_access_attributes(
            self.client.status if self.available else {}, self.station.station_id,
        )
