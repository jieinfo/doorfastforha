from __future__ import annotations
import voluptuous as vol
from homeassistant import config_entries
import homeassistant.helpers.config_validation as cv
from .const import CONF_POLL_INTERVAL, CONF_SERVER_ADDRESS, DOMAIN

class DoorfastConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    async def async_step_user(self, user_input=None):
        if user_input is not None:
            address = user_input[CONF_SERVER_ADDRESS].rstrip("/")
            if address.endswith("/cgi-bin/doorfast") is False:
                address += "/cgi-bin/doorfast"
            return self.async_create_entry(title=address, data={**user_input, CONF_SERVER_ADDRESS: address})
        return self.async_show_form(step_id="user", data_schema=vol.Schema({
            vol.Required(CONF_SERVER_ADDRESS): cv.url,
            vol.Optional(CONF_POLL_INTERVAL, default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=300)),
        }))
