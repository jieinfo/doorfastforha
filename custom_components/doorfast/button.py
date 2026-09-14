from homeassistant.components.button import ButtonEntity
from .const import DOMAIN, MANUFACTURER, SW_VERSION
async def async_setup_entry(hass, entry, async_add_entities):
 c=hass.data[DOMAIN][entry.entry_id]; async_add_entities([ElevatorButton(c,entry.entry_id,"up"),ElevatorButton(c,entry.entry_id,"down"),AnswerButton(c,entry.entry_id),HangupButton(c,entry.entry_id)])
class Base(ButtonEntity):
 def __init__(self,c,entry_id): self.client=c; self.entry_id=entry_id
 @property
 def device_info(self): return {"identifiers": {(DOMAIN,self.entry_id)},"name":"Doorfast Controller","manufacturer":MANUFACTURER,"sw_version":SW_VERSION}
class ElevatorButton(Base):
 def __init__(self,c,entry_id,d): super().__init__(c,entry_id); self.direction=d; self._attr_translation_key=f"elevator_{d}"
 @property
 def unique_id(self): return f"{DOMAIN}_{self.entry_id}_elevator_{self.direction}"
 async def async_press(self): await self.client.call_elevator(self.direction)
class HangupButton(Base):
 _attr_translation_key="hangup"
 @property
 def unique_id(self): return f"{DOMAIN}_{self.entry_id}_hangup"
 async def async_press(self): await self.client.hangup()
class AnswerButton(Base):
 _attr_translation_key="answer"
 @property
 def unique_id(self): return f"{DOMAIN}_{self.entry_id}_answer"
 async def async_press(self): await self.client.answer()
