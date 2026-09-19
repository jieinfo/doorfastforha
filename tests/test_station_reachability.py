"""Tests for dynamic Doorfast station reachability entities."""

from __future__ import annotations

import asyncio
import importlib.util
from enum import Enum
from pathlib import Path
import sys
import types
import unittest


class BinarySensorEntity:
    def __init__(self):
        self.hass = None
        self.entity_id = None
        self.removed = []
        self.writes = 0

    async def async_remove(self, force_remove=False):
        self.removed.append(force_remove)

    def async_write_ha_state(self):
        self.writes += 1

    def async_on_remove(self, callback):
        self._remove_callback = callback


class BinarySensorDeviceClass(Enum):
    OCCUPANCY = "occupancy"
    CONNECTIVITY = "connectivity"


binary_sensor_module = types.ModuleType("homeassistant.components.binary_sensor")
binary_sensor_module.BinarySensorEntity = BinarySensorEntity
binary_sensor_module.BinarySensorDeviceClass = BinarySensorDeviceClass
sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
sys.modules.setdefault("homeassistant.components", types.ModuleType("homeassistant.components"))
sys.modules["homeassistant.components.binary_sensor"] = binary_sensor_module

core_module = types.ModuleType("homeassistant.core")
core_module.callback = lambda function: function
sys.modules["homeassistant.core"] = core_module

dispatcher_module = types.ModuleType("homeassistant.helpers.dispatcher")
dispatcher_module.async_dispatcher_connect = lambda *_args: lambda: None
sys.modules.setdefault("homeassistant.helpers", types.ModuleType("homeassistant.helpers"))
sys.modules["homeassistant.helpers.dispatcher"] = dispatcher_module

entity_module = types.ModuleType("homeassistant.helpers.entity")
entity_module.Entity = object
sys.modules["homeassistant.helpers.entity"] = entity_module


class FakeEntityRegistry:
    def __init__(self):
        self.removed = []

    def async_get_entity_id(self, _domain, _platform, unique_id):
        return f"binary_sensor.{unique_id}"

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
load_component("generation")
load_component("station_entity")
binary_sensor = load_component("binary_sensor")

DoorfastStation = client_types.DoorfastStation
DoorfastStationReachability = binary_sensor.DoorfastStationReachability


def station(station_id="gate_main", *, name="Main Gate", route_fresh=True):
    return DoorfastStation(
        station_id=station_id,
        name=name,
        logical_address="32:02:01:00:02:01",
        enabled=True,
        stream_name=f"doorfast_{station_id}",
        route_source="discovered",
        route_fresh=route_fresh,
        monitorable=True,
        last_seen_ms=123,
    )


class FakeClient:
    online = True
    status = {"call": {"session": "idle"}}


class FakeRegistry:
    def __init__(self):
        self.client = FakeClient()
        self.items = {
            "gate_main": station(),
            "gate_side": station("gate_side", name="Side Gate"),
        }
        self.listener = None

    @property
    def station_ids(self):
        return tuple(self.items)

    def station(self, station_id):
        return self.items[station_id]

    def add_listener(self, listener):
        self.listener = listener
        return lambda: setattr(self, "listener", None)

    def update(self, item):
        self.items[item.station_id] = item
        self.listener("updated", item.station_id)

    def remove(self, station_id):
        self.listener("removed", station_id)
        self.items.pop(station_id)


class FakeHass:
    def __init__(self, registry):
        self.data = {
            const_module.DOMAIN: {"entry-1": registry.client},
            const_module.STATIONS_KEY: {"entry-1": registry},
        }
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


class StationReachabilityTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ENTITY_REGISTRY.removed.clear()

    async def test_route_freshness_and_stable_identity(self):
        entity = DoorfastStationReachability(
            FakeClient(), "entry-1", station(route_fresh=False)
        )

        self.assertEqual(
            "doorfast_entry-1_station_gate_main_reachable", entity.unique_id
        )
        self.assertFalse(entity.is_on)
        entity.update_station(station(name="Renamed Gate", route_fresh=True))
        self.assertTrue(entity.is_on)
        self.assertEqual(
            "doorfast_entry-1_station_gate_main_reachable", entity.unique_id
        )

    async def test_setup_updates_and_removes_only_matching_station(self):
        registry = FakeRegistry()
        hass = FakeHass(registry)
        entry = FakeEntry()
        entities = []

        def add(items):
            for item in items:
                item.hass = hass
                item.entity_id = f"binary_sensor.{item.unique_id}"
                entities.append(item)

        await binary_sensor.async_setup_entry(hass, entry, add)
        stations = [
            item for item in entities if isinstance(item, DoorfastStationReachability)
        ]
        self.assertEqual(2, len(stations))

        main = next(item for item in stations if item.station.station_id == "gate_main")
        side = next(item for item in stations if item.station.station_id == "gate_side")
        registry.update(station(route_fresh=False))
        self.assertFalse(main.is_on)
        self.assertTrue(side.is_on)

        registry.remove("gate_main")
        await asyncio.gather(*hass.tasks)
        self.assertFalse(main.available)
        self.assertEqual([True], main.removed)
        self.assertEqual([], side.removed)
        self.assertEqual(
            ["binary_sensor.doorfast_entry-1_station_gate_main_reachable"],
            ENTITY_REGISTRY.removed,
        )


if __name__ == "__main__":
    unittest.main()
