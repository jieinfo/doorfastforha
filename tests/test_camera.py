"""Tests for dynamic station-scoped Doorfast cameras."""

from __future__ import annotations

import asyncio
import importlib.util
from enum import IntFlag
from pathlib import Path
import sys
import types
import unittest


class CameraEntityFeature(IntFlag):
    STREAM = 2


class Camera:
    def __init__(self):
        self.hass = None
        self.entity_id = None
        self.removed = []

    @property
    def supported_features(self):
        return self._attr_supported_features

    async def async_remove(self, force_remove=False):
        self.removed.append(force_remove)

    def async_write_ha_state(self):
        pass

    def async_on_remove(self, callback):
        self._remove_callback = callback


camera_module = types.ModuleType("homeassistant.components.camera")
camera_module.Camera = Camera
camera_module.CameraEntityFeature = CameraEntityFeature
sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
sys.modules.setdefault("homeassistant.components", types.ModuleType("homeassistant.components"))
sys.modules["homeassistant.components.camera"] = camera_module

entity_module = types.ModuleType("homeassistant.helpers.entity")
entity_module.Entity = object
sys.modules.setdefault("homeassistant.helpers", types.ModuleType("homeassistant.helpers"))
sys.modules["homeassistant.helpers.entity"] = entity_module


class FakeEntityRegistry:
    def __init__(self):
        self.removed = []

    def async_get_entity_id(self, platform, domain, unique_id):
        return f"camera.{unique_id}"

    def async_remove(self, entity_id):
        self.removed.append(entity_id)


ENTITY_REGISTRY = FakeEntityRegistry()
entity_registry_module = types.ModuleType("homeassistant.helpers.entity_registry")
entity_registry_module.async_get = lambda _hass: ENTITY_REGISTRY
sys.modules["homeassistant.helpers.entity_registry"] = entity_registry_module

ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / "doorfast"
package = types.ModuleType("custom_components.doorfast")
package.__path__ = [str(COMPONENT)]
sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
sys.modules["custom_components.doorfast"] = package


def load_component(name):
    spec = importlib.util.spec_from_file_location(
        f"custom_components.doorfast.{name}", COMPONENT / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


client_types = load_component("client_types")
const_module = load_component("const")
load_component("monitor")
load_component("stations")
load_component("station_entity")
camera_integration = load_component("camera")

DoorfastStation = client_types.DoorfastStation
DoorfastStationCamera = camera_integration.DoorfastStationCamera


def station(station_id, *, enabled=True):
    return DoorfastStation(
        station_id=station_id,
        name=station_id.replace("_", " ").title(),
        logical_address="32:02:01:00:02:01",
        enabled=enabled,
        stream_name=f"doorfast_{station_id}",
        route_source="discovered",
        route_fresh=True,
        monitorable=True,
        last_seen_ms=123,
    )


class FakeClient:
    online = True
    status = {"media": {"installed": True, "available": True}}


class FakeMonitor:
    snapshot = {
        "runtime_id": "runtime-a",
        "station_id": "gate_main",
        "state": "publishing",
        "generation": 9,
        "ready": True,
        "viewer_count": 1,
        "status_revision": 4,
    }


class FakeRegistry:
    def __init__(self):
        self.client = FakeClient()
        self.items = {
            "gate_main": station("gate_main"),
            "gate_side": station("gate_side"),
        }
        self.monitors = {key: FakeMonitor() for key in self.items}
        self.listener = None

    @property
    def station_ids(self):
        return tuple(self.items)

    def station(self, station_id):
        return self.items[station_id]

    def monitor(self, station_id):
        return self.monitors[station_id]

    def add_listener(self, listener):
        self.listener = listener
        return lambda: setattr(self, "listener", None)

    def remove(self, station_id):
        self.listener("removed", station_id)
        self.items.pop(station_id)
        self.monitors.pop(station_id)


class FakeHass:
    def __init__(self, registry):
        self.data = {const_module.STATIONS_KEY: {"entry-1": registry}}
        self.tasks = []

    def async_create_task(self, awaitable):
        task = asyncio.create_task(awaitable)
        self.tasks.append(task)
        return task


class FakeEntry:
    entry_id = "entry-1"

    def __init__(self):
        self.unloads = []

    def async_on_unload(self, callback):
        self.unloads.append(callback)


class DoorfastCameraTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ENTITY_REGISTRY.removed.clear()

    async def test_exposes_station_source_identity_and_no_jpeg(self):
        camera = DoorfastStationCamera(
            FakeClient(), "entry-1", station("gate_main"), FakeMonitor()
        )

        self.assertIsNone(await camera.async_camera_image())
        self.assertEqual(
            "doorfast://entry-1/station/gate_main/preview",
            await camera.stream_source(),
        )
        self.assertEqual(
            "doorfast_entry-1_station_gate_main_camera", camera.unique_id
        )
        self.assertTrue(camera.supported_features & CameraEntityFeature.STREAM)

    async def test_setup_adds_each_station_and_removes_one_dynamically(self):
        registry = FakeRegistry()
        hass = FakeHass(registry)
        entry = FakeEntry()
        cameras = []

        def add_entities(entities):
            for camera in entities:
                camera.hass = hass
                camera.entity_id = f"camera.{camera.unique_id}"
                cameras.append(camera)

        await camera_integration.async_setup_entry(hass, entry, add_entities)

        self.assertEqual(
            [
                "doorfast_entry-1_station_gate_main_camera",
                "doorfast_entry-1_station_gate_side_camera",
            ],
            [camera.unique_id for camera in cameras],
        )

        removed = cameras[0]
        registry.remove("gate_main")
        await asyncio.gather(*hass.tasks)

        self.assertFalse(removed.available)
        self.assertEqual([True], removed.removed)
        self.assertEqual(
            ["camera.doorfast_entry-1_station_gate_main_camera"],
            ENTITY_REGISTRY.removed,
        )
        self.assertEqual(1, len(entry.unloads))


if __name__ == "__main__":
    unittest.main()
