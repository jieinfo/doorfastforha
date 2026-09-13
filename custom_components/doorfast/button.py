from homeassistant.components.button import ButtonEntity
from .const import DOMAIN, MANUFACTURER, SW_VERSION
async def async_setup_entry(hass, entry, async_add_entities):
 c=hass.data[DOMAIN][entry.entry_id]; async_add_entities([ElevatorButton(c,"up"),ElevatorButton(c,"down"),HangupButton(c)])
class Base(ButtonEntity):
 def __init__(self,c): self.client=c
 @property
 def device_info(self): return {"identifiers": {(DOMAIN,"controller")},"name":"Doorfast Controller","manufacturer":MANUFACTURER,"sw_version":SW_VERSION}
class ElevatorButton(Base):
 def __init__(self,c,d): super().__init__(c); self.direction=d; self._attr_translation_key=f"elevator_{d}"
 @property
 def unique_id(self): return f"{DOMAIN}_elevator_{self.direction}"
 async def async_press(self): await self.client.call_elevator(self.direction)
class HangupButton(Base):
 _attr_translation_key="hangup"
 @property
 def unique_id(self): return f"{DOMAIN}_hangup"
 async def async_press(self): await self.client.hangup()

