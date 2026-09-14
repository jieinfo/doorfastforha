from homeassistant.components.lock import LockEntity
from .const import DOMAIN, MANUFACTURER, SW_VERSION
async def async_setup_entry(hass, entry, async_add_entities): async_add_entities([DoorfastLock(hass.data[DOMAIN][entry.entry_id], entry.entry_id)])
class DoorfastLock(LockEntity):
    _attr_translation_key = "unlock"
    def __init__(self, client, entry_id): self.client=client; self.entry_id=entry_id; self._locked=True
    @property
    def unique_id(self): return f"{DOMAIN}_{self.entry_id}_unlock"
    @property
    def device_info(self): return {"identifiers": {(DOMAIN,self.entry_id)}, "name":"Doorfast Controller", "manufacturer":MANUFACTURER, "sw_version":SW_VERSION}
    @property
    def is_locked(self): return self._locked
    async def async_lock(self, **kwargs): self._locked=True; self.async_write_ha_state()
    async def async_unlock(self, **kwargs): await self.client.unlock(); self._locked=False; self.async_write_ha_state()
