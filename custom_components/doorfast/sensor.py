from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from .const import DOMAIN, LATEST_EVENT, MANUFACTURER, SW_VERSION
async def async_setup_entry(hass,entry,add): add([DoorfastStatus(hass,entry)])
class DoorfastStatus(SensorEntity):
 _attr_translation_key=LATEST_EVENT
 def __init__(self,hass,e): self.hass=hass; self.e=e; self._state="unknown"; self._attrs={}
 @property
 def unique_id(self): return f"{DOMAIN}_{self.e.entry_id}_{LATEST_EVENT}"
 @property
 def device_info(self): return {"identifiers": {(DOMAIN,"controller")},"name":"Doorfast Controller","manufacturer":MANUFACTURER,"sw_version":SW_VERSION}
 @property
 def native_value(self): return self._state
 @property
 def extra_state_attributes(self): return self._attrs
 async def async_added_to_hass(self): self.async_on_remove(async_dispatcher_connect(self.hass,f"{DOMAIN}_{self.e.entry_id}_STATUS",self.update)); self.update(self.hass.data[DOMAIN][self.e.entry_id].status)
 def update(self,d): self._attrs=d; self._state=d.get("call",{}).get("state",d.get("event","unknown")); self.async_write_ha_state()

