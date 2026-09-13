from homeassistant.components.camera import Camera
from .const import DOMAIN, MANUFACTURER, SW_VERSION

async def async_setup_entry(hass, entry, async_add_entities):
    client = hass.data[DOMAIN][entry.entry_id]
    url = client.status.get("video_url") or client.status.get("call", {}).get("video_url")
    if url: async_add_entities([DoorfastCamera(url)])

class DoorfastCamera(Camera):
    _attr_translation_key = "video"
    def __init__(self, url): super().__init__(); self.url = url
    @property
    def unique_id(self): return f"{DOMAIN}_video"
    @property
    def device_info(self): return {"identifiers": {(DOMAIN,"controller")},"name":"Doorfast Controller","manufacturer":MANUFACTURER,"sw_version":SW_VERSION}
    async def stream_source(self): return self.url
