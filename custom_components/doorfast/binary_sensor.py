from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from .const import DOMAIN, RING_STATUS, MANUFACTURER, SW_VERSION
from .generation import is_ringing
async def async_setup_entry(hass,entry,add): add([IncomingCall(hass,entry)])
class IncomingCall(BinarySensorEntity):
 _attr_translation_key=RING_STATUS; _attr_device_class=BinarySensorDeviceClass.OCCUPANCY
 _attr_should_poll=False
 def __init__(self,hass,e): self.hass=hass; self.e=e; self._on=is_ringing(hass.data[DOMAIN][e.entry_id].status)
 @property
 def unique_id(self): return f"{DOMAIN}_{self.e.entry_id}_{RING_STATUS}"
 @property
 def device_info(self): return {"identifiers": {(DOMAIN,self.e.entry_id)},"name":"Doorfast Controller","manufacturer":MANUFACTURER,"sw_version":SW_VERSION}
 @property
 def is_on(self): return self._on
 async def async_added_to_hass(self): self.async_on_remove(async_dispatcher_connect(self.hass,f"{DOMAIN}_{self.e.entry_id}_{RING_STATUS}",self.trigger))
 @callback
 def trigger(self,state=True): self._on=bool(state); self.async_write_ha_state()
