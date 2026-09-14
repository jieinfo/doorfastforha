from homeassistant.components.lock import LockEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .access_status import access_attributes
from .const import DOMAIN, MANUFACTURER, SW_VERSION


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([DoorfastLock(hass, entry)])


class DoorfastLock(LockEntity):
    """Momentary Doorfast unlock control with protocol-result attributes."""

    _attr_translation_key = "unlock"
    _attr_assumed_state = True

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
            "name": "Doorfast Controller",
            "manufacturer": MANUFACTURER,
            "sw_version": SW_VERSION,
        }

    @property
    def is_locked(self):
        # Doorfast currently confirms the protocol reply, not the door position.
        # Treat unlock as a momentary pulse and never claim a persistent open state.
        return True

    @property
    def extra_state_attributes(self):
        return self._attrs

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_{self.entry.entry_id}_STATUS",
                self.update,
            )
        )

    def update(self, data):
        self._attrs = access_attributes(data)
        self.async_write_ha_state()

    async def async_lock(self, **kwargs):
        self.async_write_ha_state()

    async def async_unlock(self, **kwargs):
        await self.client.unlock()
        self.async_write_ha_state()
