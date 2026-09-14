from homeassistant.components.camera import Camera
from .const import DOMAIN, MANUFACTURER, SW_VERSION

async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([DoorfastCamera(hass.data[DOMAIN][entry.entry_id])])

class DoorfastCamera(Camera):
    _attr_translation_key = "video"
    _attr_content_type = "image/jpeg"
    def __init__(self, client):
        super().__init__(); self.client = client
    @property
    def unique_id(self): return f"{DOMAIN}_video"
    @property
    def device_info(self): return {"identifiers": {(DOMAIN,"controller")},"name":"Doorfast Controller","manufacturer":MANUFACTURER,"sw_version":SW_VERSION}
    async def async_camera_image(self, width=None, height=None):
        try: return await self.client.latest_video_frame()
        except Exception: return None
