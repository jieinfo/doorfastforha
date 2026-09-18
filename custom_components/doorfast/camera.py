from homeassistant.components.camera import Camera, CameraEntityFeature

from .const import (
    DOMAIN,
    MANUFACTURER,
    MONITORS_KEY,
    MONITOR_STATUS,
    SW_VERSION,
)

async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        [
            DoorfastCamera(
                hass.data[DOMAIN][entry.entry_id],
                entry.entry_id,
                hass.data[MONITORS_KEY][entry.entry_id],
            )
        ]
    )

class DoorfastCamera(Camera):
    _attr_translation_key = "video"
    _attr_content_type = "image/jpeg"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, client, entry_id, monitor):
        super().__init__()
        self.client = client
        self.entry_id = entry_id
        self.monitor = monitor
        self._stream_available = self._media_available()

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self.entry_id}_video"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self.entry_id)},
            "name": "Doorfast Controller",
            "manufacturer": MANUFACTURER,
            "sw_version": SW_VERSION,
        }

    @property
    def extra_state_attributes(self):
        snapshot = self.monitor.snapshot
        return {
            "monitor_state": snapshot["state"],
            "monitor_generation": snapshot["generation"],
            "monitor_ready": snapshot["ready"],
        }

    def _media_available(self):
        media = self.client.status.get("media")
        return (
            isinstance(media, dict)
            and media.get("installed") is True
            and media.get("available") is True
        )

    async def async_added_to_hass(self):
        from homeassistant.helpers.dispatcher import async_dispatcher_connect

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_{self.entry_id}_{MONITOR_STATUS}",
                self._handle_monitor_status,
            )
        )

    def _handle_monitor_status(self, _snapshot):
        stream_available = self._media_available()
        if stream_available != self._stream_available:
            self._stream_available = stream_available
            self.hass.async_create_task(self.async_refresh_providers())
        self.async_write_ha_state()

    async def stream_source(self):
        if not self._media_available():
            return None
        return f"doorfast://{self.entry_id}/preview"

    async def async_camera_image(self, width=None, height=None):
        try:
            return await self.client.latest_video_frame()
        except Exception:
            return None
