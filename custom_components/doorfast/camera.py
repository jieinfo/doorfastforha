"""Dynamic cameras for configured Doorfast stations."""

from __future__ import annotations

from homeassistant.components.camera import Camera
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, STATIONS_KEY
from .station_entity import DoorfastStationEntity


async def async_setup_entry(hass, entry, async_add_entities):
    registry = hass.data[STATIONS_KEY][entry.entry_id]
    cameras = {}

    def add_station(station_id):
        camera = DoorfastStationCamera(
            registry.client,
            entry.entry_id,
            registry.station(station_id),
            registry.monitor(station_id),
        )
        cameras[station_id] = camera
        async_add_entities([camera])

    def remove_station(station_id):
        camera = cameras.pop(station_id, None)
        if camera is None:
            return
        camera.mark_removed()
        entity_registry = er.async_get(hass)
        entity_id = entity_registry.async_get_entity_id(
            "camera", DOMAIN, camera.unique_id
        )
        if entity_id is not None:
            entity_registry.async_remove(entity_id)
        hass.async_create_task(camera.async_remove(force_remove=True))

    def handle_station(event, station_id):
        if event == "added":
            add_station(station_id)
        elif event == "updated":
            camera = cameras.get(station_id)
            if camera is not None:
                camera.station = registry.station(station_id)
                camera.async_write_ha_state()
        elif event == "removed":
            remove_station(station_id)

    for station_id in registry.station_ids:
        add_station(station_id)
    entry.async_on_unload(registry.add_listener(handle_station))


class DoorfastStationCamera(Camera, DoorfastStationEntity):
    """A station-scoped opaque WebRTC source."""

    _attr_translation_key = "video"
    _attr_content_type = "image/jpeg"
    _attr_supported_features = 0

    def __init__(self, client, entry_id, station, monitor):
        Camera.__init__(self)
        DoorfastStationEntity.__init__(self, entry_id, station)
        self.client = client
        self.monitor = monitor
        self._active = True

    @property
    def unique_id(self):
        return (
            f"{DOMAIN}_{self.entry_id}_station_"
            f"{self.station.station_id}_camera"
        )

    @property
    def available(self):
        return self._active and self.station.enabled and self.client.online

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
            and self.station.monitorable
        )

    def mark_removed(self):
        self._active = False
        self.async_write_ha_state()

    async def stream_source(self):
        if not self.available or not self._media_available():
            return None
        return (
            f"doorfast://{self.entry_id}/station/"
            f"{self.station.station_id}/preview"
        )

    async def async_camera_image(self, width=None, height=None):
        return None


DoorfastCamera = DoorfastStationCamera
