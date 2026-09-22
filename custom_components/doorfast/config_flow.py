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
from .config_helpers import (
    is_doorfast_status,
    normalize_bridge_url,
    normalize_go2rtc_api_url,
)
from .const import (
    CONF_GO2RTC_API_URL,
    CONF_GO2RTC_PASSWORD,
    CONF_GO2RTC_USERNAME,
    CONF_POLL_INTERVAL,
    CONF_SERVER_ADDRESS,
    DEFAULT_GO2RTC_API_URL,
    DOMAIN,
)

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

    @staticmethod
    def async_get_options_flow(config_entry):
        return DoorfastOptionsFlow(config_entry)


class DoorfastOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            username = user_input.get(CONF_GO2RTC_USERNAME, "")
            password = user_input.get(CONF_GO2RTC_PASSWORD, "")
            if bool(username) != bool(password):
                errors["base"] = "go2rtc_credentials_required"
            else:
                try:
                    address = normalize_go2rtc_api_url(
                        user_input[CONF_GO2RTC_API_URL]
                    )
                except ValueError:
                    errors[CONF_GO2RTC_API_URL] = "invalid_url"
                else:
                    return self.async_create_entry(
                        title="",
                        data={
                            CONF_GO2RTC_API_URL: address,
                            CONF_GO2RTC_USERNAME: username,
                            CONF_GO2RTC_PASSWORD: password,
                        },
                    )
        current = self.config_entry.options.get(
            CONF_GO2RTC_API_URL,
            self.config_entry.data.get(
                CONF_GO2RTC_API_URL, DEFAULT_GO2RTC_API_URL
            ),
        )
        current_username = self.config_entry.options.get(
            CONF_GO2RTC_USERNAME,
            self.config_entry.data.get(CONF_GO2RTC_USERNAME, ""),
        )
        current_password = self.config_entry.options.get(
            CONF_GO2RTC_PASSWORD,
            self.config_entry.data.get(CONF_GO2RTC_PASSWORD, ""),
        )
        return self.async_show_form(
            step_id="init",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_GO2RTC_API_URL, default=current
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
                    vol.Optional(
                        CONF_GO2RTC_USERNAME, default=current_username
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
                    vol.Optional(
                        CONF_GO2RTC_PASSWORD, default=current_password
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                }
            ),
        )
