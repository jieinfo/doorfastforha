from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from .const import DOMAIN, RING_STATUS, MANUFACTURER, SW_VERSION
async def async_setup_entry(hass,entry,add): add([IncomingCall(hass,entry)])
class IncomingCall(BinarySensorEntity):
 _attr_translation_key=RING_STATUS; _attr_device_class=BinarySensorDeviceClass.OCCUPANCY
 def __init__(self,hass,e): self.hass=hass; self.e=e; self._on=False
 @property
 def unique_id(self): return f"{DOMAIN}_{self.e.entry_id}_{RING_STATUS}"
 @property
 def device_info(self): return {"identifiers": {(DOMAIN,"controller")},"name":"Doorfast Controller","manufacturer":MANUFACTURER,"sw_version":SW_VERSION}
 @property
 def is_on(self): return self._on
 async def async_added_to_hass(self): self.async_on_remove(async_dispatcher_connect(self.hass,f"{DOMAIN}_{self.e.entry_id}_{RING_STATUS}",self.trigger))
 def trigger(self,state=True): self._on=bool(state); self.async_write_ha_state()

