"""Station status entities use scoped bridge facts without altering media leases."""

from __future__ import annotations

import asyncio
from enum import Enum
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest

from tests.test_device_registration import DeviceRegistry


class SensorEntity:
    def __init__(self):
        self.hass = None
        self.entity_id = None
        self.removed = []
        self.writes = 0
        self.cleanup = []

    async def async_remove(self, force_remove=False):
        self.removed.append(force_remove)
        for unsubscribe in self.cleanup:
            unsubscribe()

    def async_write_ha_state(self):
        if self.hass is None or self.entity_id is None:
            raise RuntimeError("entity is not attached to HA")
        self.writes += 1

    def async_on_remove(self, callback):
        self.cleanup.append(callback)


class SensorDeviceClass(Enum):
    ENUM = "enum"


sensor_module = types.ModuleType("homeassistant.components.sensor")
sensor_module.SensorEntity = SensorEntity
sensor_module.SensorDeviceClass = SensorDeviceClass
sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
sys.modules.setdefault("homeassistant.components", types.ModuleType("homeassistant.components"))
sys.modules["homeassistant.components.sensor"] = sensor_module
core_module = types.ModuleType("homeassistant.core")
core_module.callback = lambda function: function
sys.modules["homeassistant.core"] = core_module
sys.modules.setdefault("homeassistant.helpers", types.ModuleType("homeassistant.helpers"))
device_registry_module = types.ModuleType("homeassistant.helpers.device_registry")
device_registry_module.async_get = lambda hass: hass.devices
sys.modules["homeassistant.helpers.device_registry"] = device_registry_module
sys.modules["homeassistant.helpers"].device_registry = device_registry_module
entity_module = types.ModuleType("homeassistant.helpers.entity")
entity_module.Entity = object
sys.modules["homeassistant.helpers.entity"] = entity_module
dispatcher_module = types.ModuleType("homeassistant.helpers.dispatcher")


def connect(hass, signal, listener):
    hass.listeners.setdefault(signal, []).append(listener)
    return lambda: hass.listeners[signal].remove(listener)


dispatcher_module.async_dispatcher_connect = connect
sys.modules["homeassistant.helpers.dispatcher"] = dispatcher_module
entity_registry_module = types.ModuleType("homeassistant.helpers.entity_registry")
entity_registry_module.async_get = lambda hass: hass
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
const = load_component("const")
load_component("station_entity")
sensor = load_component("sensor")
sensor.async_dispatcher_connect = connect
sensor.er = entity_registry_module


def station(station_id="gate_main", name="Main gate", enabled=True):
    return client_types.DoorfastStation(
        station_id=station_id, name=name, logical_address="32:02:01:00:01:00",
        enabled=enabled, stream_name=f"doorfast_{station_id}",
        route_source="configured", route_fresh=True, monitorable=True, last_seen_ms=0,
    )


class FakeRegistry:
    def __init__(self):
        self.client = types.SimpleNamespace(online=True, status={}, latest_audio_url=None)
        self.items = {"gate_main": station(), "gate_side": station("gate_side", "Side gate")}
        self.listener = None

    @property
    def station_ids(self):
        return tuple(self.items)

    def station(self, station_id):
        return self.items[station_id]

    def add_listener(self, listener):
        self.listener = listener
        return lambda: setattr(self, "listener", None)


class FakeHass:
    def __init__(self, registry):
        self.devices = DeviceRegistry()
        self.devices.async_get_or_create(
            config_entry_id="entry-1", identifiers={("doorfast", "entry-1")},
            name="Doorfast Bridge", manufacturer="Doorfast",
        )
        self.data = {
            const.DOMAIN: {"entry-1": registry.client},
            const.STATIONS_KEY: {"entry-1": registry},
        }
        self.listeners = {}
        self.tasks = []
        self.removed = []

    def async_create_task(self, awaitable):
        task = asyncio.create_task(awaitable)
        self.tasks.append(task)
        return task

    def async_get_entity_id(self, platform, domain, unique_id):
        return f"{platform}.{unique_id}"

    def async_remove(self, entity_id):
        self.removed.append(entity_id)


class FakeEntry:
    entry_id = "entry-1"

    def __init__(self):
        self.unloads = []

    def async_on_unload(self, listener):
        self.unloads.append(listener)


class StationStatusTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = FakeRegistry()
        self.hass = FakeHass(self.registry)
        self.entry = FakeEntry()
        self.entities = []

        def add(items):
            for item in items:
                item.hass = self.hass
                item.entity_id = f"sensor.{item.unique_id}"
                self.entities.append(item)

        await sensor.async_setup_entry(self.hass, self.entry, add)

    def find(self, station_id, kind):
        return next(item for item in self.entities
                    if getattr(item, "station", None) is not None
                    and item.station.station_id == station_id
                    and item._attr_translation_key == kind)

    async def test_setup_preserves_controller_and_adds_translated_station_status(self):
        self.assertEqual(5, len(self.entities))
        self.assertEqual("doorfast_entry-1_latest_event", self.entities[0].unique_id)
        for language in ("en", "zh-Hans"):
            translations = json.loads((COMPONENT / "translations" / f"{language}.json").read_text())
            for kind in ("preview_status", "unlock_status"):
                entity = self.find("gate_main", kind)
                self.assertEqual(f"doorfast_entry-1_station_gate_main_{kind}", entity.unique_id)
                self.assertEqual(SensorDeviceClass.ENUM, entity._attr_device_class)
                self.assertEqual(set(entity._attr_options), set(translations["entity"]["sensor"][kind]["state"]))
                self.assertEqual({("doorfast", "entry-1_station_gate_main")}, entity.device_info["identifiers"])

    async def test_preview_lifecycle_and_status_dispatch(self):
        self.assertEqual(5, len(self.entities))
        entity = self.find("gate_main", "preview_status")
        await entity.async_added_to_hass()
        self.assertIsNone(entity.native_value)  # No supported status, not idle/live.
        session = {"station_id": "gate_main", "generation": 4, "state": "requesting"}
        self.registry.client.status = {"media": {"sessions": [session]}}
        # Depending on entity startup order, select this sensor's callback.
        listener = next(cb for cb in self.hass.listeners["doorfast_entry-1_STATUS"] if cb.__self__ is entity)
        for state, ready, live, expected in (
            ("requesting", False, False, "requesting"),
            ("awaiting_video", False, False, "awaiting_video"),
            ("viewing", True, True, "live"),
            ("publishing", True, False, "reconnecting"),
            ("failed", True, True, "failed"),
        ):
            session.update(state=state, ready=ready, source_live=live)
            listener(self.registry.client.status)
            self.assertEqual(expected, entity.native_value)
        session.update(state="publishing", source_live="true")
        self.assertIsNone(entity.native_value)
        session.update(source_live=True, purpose="call")
        self.assertEqual("in_call", entity.native_value)
        self.registry.client.online = False
        listener(self.registry.client.status)
        self.assertFalse(entity.available)
        self.assertIsNone(entity.native_value)
        self.assertGreater(entity.writes, 0)

    async def test_unlock_progress_does_not_reuse_another_station_result(self):
        self.assertEqual(5, len(self.entities))
        main = self.find("gate_main", "unlock_status")
        side = self.find("gate_side", "unlock_status")
        access = {"configured": True, "state": "protocol_completed", "station_id": "gate_main",
                  "generation": 8, "raw_status": 1, "physical_result_confirmed": False,
                  "manual_state": "waiting_media", "manual_station_id": "gate_side",
                  "material": "must not leak"}
        self.registry.client.status = {"access": access}
        self.assertEqual("protocol_completed", main.native_value)
        self.assertEqual("waiting_media", side.native_value)
        self.assertNotIn("raw_status", side.extra_state_attributes)
        self.assertNotIn("generation", side.extra_state_attributes)
        self.assertNotIn("must not leak", str(main.extra_state_attributes))
        access.update(state="waiting", station_id="gate_side", manual_state="waiting_reply")
        self.assertEqual("idle", main.native_value)
        self.assertEqual("waiting_reply", side.native_value)
        access.update(state="protocol_rejected", raw_status=0, manual_state="idle")
        access.pop("manual_station_id")
        self.assertEqual("protocol_rejected", side.native_value)
        self.assertFalse(side.extra_state_attributes["physical_result_confirmed"])
        access.pop("station_id")  # Legacy unscoped result is never guessed.
        self.assertIsNone(side.native_value)
        self.assertNotIn("raw_status", side.extra_state_attributes)

    async def test_preempted_slot_is_removed_and_marker_does_not_override_new_preview(self):
        self.assertEqual(5, len(self.entities))
        entity = self.find("gate_main", "preview_status")
        media = {"preempted_station_id": "gate_main", "preempted_generation": 4,
                 "sessions": [{"station_id": "gate_side", "generation": 5,
                               "purpose": "call", "state": "viewing", "ready": True,
                               "source_live": True}]}
        self.registry.client.status = {"media": media}
        self.assertEqual("preempted", entity.native_value)
        self.assertEqual(4, entity.extra_state_attributes["preempted_generation"])
        media["sessions"].append({"station_id": "gate_main", "generation": 6,
                                  "purpose": "preview", "state": "viewing", "ready": True,
                                  "source_live": True})
        self.assertEqual("live", entity.native_value)
        media["sessions"].clear()  # Historical marker after call ends is not an active preemption.
        self.assertEqual("idle", entity.native_value)

    async def test_remove_update_and_runtime_replacement_keep_stable_ids(self):
        self.assertEqual(5, len(self.entities))
        original = [self.find("gate_main", kind) for kind in ("preview_status", "unlock_status")]
        for entity in original:
            await entity.async_added_to_hass()
        self.registry.items["gate_main"] = station(name="Renamed gate", enabled=False)
        self.registry.listener("updated", "gate_main")
        self.assertTrue(all(not entity.available for entity in original))
        self.assertEqual("Renamed gate", original[0].device_info["name"])
        self.registry.listener("removed", "gate_main")
        await asyncio.gather(*self.hass.tasks)
        self.assertTrue(all(entity.removed == [True] for entity in original))
        self.assertEqual(2, len(self.hass.removed))
        self.assertEqual([], self.hass.listeners["doorfast_entry-1_STATUS"])
        self.registry.items["gate_main"] = station()
        self.registry.listener("added", "gate_main")
        self.assertEqual([item.unique_id for item in original], [item.unique_id for item in self.entities[-2:]])
        self.assertTrue(self.find("gate_side", "preview_status").available)

    async def test_disabled_sensors_do_not_abort_station_update_and_removal(self):
        self.assertEqual(5, len(self.entities))
        disabled = [self.find("gate_main", kind) for kind in ("preview_status", "unlock_status")]
        for entity in disabled:
            entity.hass = None
            entity.entity_id = None
        self.registry.items["gate_main"] = station(name="New name")
        self.registry.listener("updated", "gate_main")
        self.registry.listener("removed", "gate_main")
        self.assertTrue(all(not item.available for item in disabled))
        self.assertEqual(2, len(self.hass.removed))
        self.assertEqual([], self.hass.tasks)
        self.assertTrue(all(item.removed == [] for item in disabled))


if __name__ == "__main__":
    unittest.main()
