import inspect

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers import entity_registry as er
from .const import CONTROLLER_NAME, DOMAIN, MANUFACTURER, STATIONS_KEY, SW_VERSION
from .station_entity import DoorfastStationEntity
async def async_setup_entry(hass, entry, async_add_entities):
 c=hass.data[DOMAIN][entry.entry_id]; async_add_entities([ElevatorButton(c,entry.entry_id,"up"),ElevatorButton(c,entry.entry_id,"down"),AnswerButton(c,entry.entry_id),HangupButton(hass,c,entry.entry_id)])
 registry = hass.data.get(STATIONS_KEY, {}).get(entry.entry_id)
 if registry is None: return
 entities = {}
 def add_station(station_id):
  entities[station_id] = (StationCallButton(c, entry.entry_id, registry.station(station_id), registry.monitor(station_id)), StationHangupButton(c, entry.entry_id, registry.station(station_id), registry.monitor(station_id)))
  def update_states(_monitor):
   for entity in entities.get(station_id, ()):
    result = entity.async_write_ha_state()
    if inspect.isawaitable(result):
     hass.async_create_task(result)
  unsubscribe = registry.monitor(station_id).add_listener(update_states)
  for entity in entities[station_id]: entity._unsubscribe_monitor = unsubscribe
  async_add_entities(list(entities[station_id]))
 def remove_station(station_id):
  pair = entities.pop(station_id, ())
  if not pair:
   return
  unsubscribe = getattr(pair[0], "_unsubscribe_monitor", None)
  if unsubscribe:
   unsubscribe()
  entity_registry = er.async_get(hass)
  for entity in pair:
   entity._unsubscribe_monitor = None
   entity.mark_removed()
   entity_id = entity_registry.async_get_entity_id("button", DOMAIN, entity.unique_id)
   if entity_id is not None: entity_registry.async_remove(entity_id)
   hass.async_create_task(entity.async_remove(force_remove=True))
 def listener(event, station_id):
  if event == "added": add_station(station_id)
  elif event == "updated":
   for entity in entities.get(station_id, ()):
    entity.station = registry.station(station_id); entity.async_write_ha_state()
  elif event == "removed": remove_station(station_id)
 for station_id in registry.station_ids: add_station(station_id)
 entry.async_on_unload(registry.add_listener(listener))
class Base(ButtonEntity):
 _attr_has_entity_name = True
 def __init__(self,c,entry_id): self.client=c; self.entry_id=entry_id
 @property
 def device_info(self): return {"identifiers": {(DOMAIN,self.entry_id)},"name":CONTROLLER_NAME,"manufacturer":MANUFACTURER,"sw_version":SW_VERSION}
class ElevatorButton(Base):
 def __init__(self,c,entry_id,d): super().__init__(c,entry_id); self.direction=d; self._attr_translation_key=f"elevator_{d}"
 @property
 def unique_id(self): return f"{DOMAIN}_{self.entry_id}_elevator_{self.direction}"
 async def async_press(self): await self.client.call_elevator(self.direction)
class HangupButton(Base):
 def __init__(self,hass,c,entry_id): super().__init__(c,entry_id); self.hass=hass
 _attr_translation_key="hangup"
 @property
 def unique_id(self): return f"{DOMAIN}_{self.entry_id}_hangup"
 async def async_press(self):
  manager=self.hass.data.get(f"{DOMAIN}_pcm_ws")
  if manager is not None: await manager.release_entry(self.entry_id)
  await self.client.hangup()
class AnswerButton(Base):
 _attr_translation_key="answer"
 @property
 def unique_id(self): return f"{DOMAIN}_{self.entry_id}_answer"
 async def async_press(self): await self.client.answer()

class StationButton(ButtonEntity, DoorfastStationEntity):
 def __init__(self,c,entry_id,station,monitor):
  ButtonEntity.__init__(self); DoorfastStationEntity.__init__(self,entry_id,station)
  self.client, self.monitor, self._active = c, monitor, True
  self._unsubscribe_monitor = None
 @property
 def available(self): return self._active and self.station.enabled and self.client.online
 @property
 def extra_state_attributes(self): return {"call_state": self.monitor.call_state, "call_error": self.monitor.call_error}
 def mark_removed(self): self._active=False; self.async_write_ha_state()

class StationCallButton(StationButton):
 _attr_translation_key = "call"
 @property
 def unique_id(self): return f"{DOMAIN}_{self.entry_id}_station_{self.station.station_id}_call"
 async def async_press(self): await self.monitor.async_call()

class StationHangupButton(StationButton):
 _attr_translation_key = "hangup"
 @property
 def unique_id(self): return f"{DOMAIN}_{self.entry_id}_station_{self.station.station_id}_hangup"
 async def async_press(self): await self.monitor.async_hangup()
