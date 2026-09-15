from __future__ import annotations
import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from .client import DoorfastClient
from .config_helpers import is_doorfast_status, normalize_bridge_url
from .const import CONF_POLL_INTERVAL, CONF_SERVER_ADDRESS, DOMAIN

class DoorfastConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            address = normalize_bridge_url(user_input[CONF_SERVER_ADDRESS])
            try:
                status = await DoorfastClient(self.hass, address).refresh()
                if not is_doorfast_status(status):
                    raise ValueError("response is not a running Doorfast status")
            except (aiohttp.ClientError, TimeoutError, ValueError):
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=address,
                    data={**user_input, CONF_SERVER_ADDRESS: address},
                )
        return self.async_show_form(step_id="user", errors=errors, data_schema=vol.Schema({
            vol.Required(CONF_SERVER_ADDRESS): TextSelector(
                TextSelectorConfig(type=TextSelectorType.URL)
            ),
            vol.Optional(CONF_POLL_INTERVAL, default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=300)),
        }))
