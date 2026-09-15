from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from .const import DOMAIN, LATEST_EVENT, MANUFACTURER, SW_VERSION

async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([DoorfastStatus(hass, entry)])

class DoorfastStatus(SensorEntity):
    _attr_translation_key = LATEST_EVENT
    _attr_should_poll = False
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.client = hass.data[DOMAIN][entry.entry_id]
        self._state, self._attrs = "unknown", {}
    @property
    def unique_id(self): return f"{DOMAIN}_{self.entry.entry_id}_{LATEST_EVENT}"
    @property
    def device_info(self): return {"identifiers": {(DOMAIN,self.entry.entry_id)},"name":"Doorfast Controller","manufacturer":MANUFACTURER,"sw_version":SW_VERSION}
    @property
    def native_value(self): return self._state
    @property
    def extra_state_attributes(self): return self._attrs
    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(self.hass, f"{DOMAIN}_{self.entry.entry_id}_STATUS", self._handle_status))
        self._handle_status(self.client.status)
    @callback
    def _handle_status(self, data):
        self._attrs = {**data, "latest_audio_url": self.client.latest_audio_url}
        self._state = data.get("call", {}).get("session", data.get("event", "unknown"))
        self.async_write_ha_state()
