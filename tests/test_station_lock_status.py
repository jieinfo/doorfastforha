"""Read-only station lock availability and scoped progress updates."""

import asyncio
import sys
import types
import unittest

import test_station_status_entities as support
from test_access_status import unlock_status


class LockEntity(support.SensorEntity):
    async def async_added_to_hass(self):
        pass


lock_module = types.ModuleType("homeassistant.components.lock")
lock_module.LockEntity = LockEntity
sys.modules["homeassistant.components.lock"] = lock_module
lock = support.load_component("lock")
lock.er = support.entity_registry_module
lock.async_dispatcher_connect = support.connect


class StationLockStatusTest(unittest.IsolatedAsyncioTestCase):
    async def test_station_configuration_controls_availability_without_changing_identity(self):
        registry = support.FakeRegistry()
        hass = support.FakeHass(registry)
        entity = lock.DoorfastStationLock(hass, support.FakeEntry(), support.station())
        self.assertEqual("doorfast_entry-1_station_gate_main_unlock", entity.unique_id)
        self.assertTrue(entity.available)  # Legacy metadata absence remains supported.
        registry.client.status = unlock_status()
        self.assertTrue(entity.available)
        self.assertEqual("0000000000000002", getattr(entity, "extra_state_attributes", {}).get("request_id"))
        metadata = registry.client.status["access"]["stations"][0]
        for reason in ("disabled", "material_missing", "material_invalid", "store_unavailable", "station_unavailable"):
            metadata.update(mode="disabled" if reason == "disabled" else "direct", configured=False,
                            credential_source="none", reason=reason)
            self.assertFalse(entity.available, reason)
        metadata.update(mode="direct", configured=True, credential_source="station", reason="none")
        registry.client.online = False
        self.assertFalse(entity.available)
        registry.client.online = True
        entity.station = support.station(enabled=False)
        self.assertFalse(entity.available)

    async def test_present_v1_requires_a_valid_usable_matching_station_configuration(self):
        registry = support.FakeRegistry()
        hass = support.FakeHass(registry)
        entity = lock.DoorfastStationLock(hass, support.FakeEntry(), support.station())
        registry.client.status = {"runtime_id": "0123456789abcdef", "access": {"version": 1}}
        self.assertFalse(entity.available)
        registry.client.status = {"runtime_id": "0123456789abcdef", "access": {
            "version": 1, "stations": [{"station_id": "gate_side", "mode": "direct",
                "configured": True, "credential_source": "station", "reason": "none"}]}}
        self.assertFalse(entity.available)
        registry.client.status["access"]["stations"][0]["station_id"] = "gate_main"
        self.assertTrue(entity.available)
        registry.client.status["access"]["stations"][0]["configured"] = "yes"
        self.assertFalse(entity.available)

    async def test_offline_and_recovery_notify_lock_without_unlocking(self):
        registry = support.FakeRegistry()
        hass = support.FakeHass(registry)
        entity = lock.DoorfastStationLock(hass, support.FakeEntry(), support.station())
        entity.entity_id = "lock.gate_main"
        # This client deliberately provides no control method: status updates
        # must be read-only, including offline/recovery.
        await entity.async_added_to_hass()
        self.assertEqual(1, len(hass.listeners.get("doorfast_entry-1_STATUS", [])))
        listener = hass.listeners["doorfast_entry-1_STATUS"][0]
        writes = entity.writes
        for online in (False, True):
            registry.client.online = online
            listener(registry.client.status)
            self.assertEqual(online, entity.available)
            self.assertTrue(entity.is_locked)
        self.assertEqual(writes + 2, entity.writes)

    async def test_removed_lock_unsubscribes_and_disabled_lock_is_not_written(self):
        registry = support.FakeRegistry()
        hass = support.FakeHass(registry)
        entities = []

        def add(items):
            for item in items:
                item.hass = hass
                item.entity_id = f"lock.{item.unique_id}"
                entities.append(item)

        await lock.async_setup_entry(hass, support.FakeEntry(), add)
        main = next(item for item in entities if getattr(item, "station", None) is not None
                    and item.station.station_id == "gate_main")
        side = next(item for item in entities if getattr(item, "station", None) is not None
                    and item.station.station_id == "gate_side")
        await side.async_added_to_hass()
        main.hass = None
        main.entity_id = None
        registry.items["gate_main"] = support.station(name="Renamed")
        registry.listener("updated", "gate_main")
        registry.listener("removed", "gate_main")
        registry.listener("removed", "gate_side")
        await asyncio.gather(*hass.tasks)
        self.assertEqual([], main.removed)
        self.assertEqual([True], side.removed)
        self.assertEqual([], hass.listeners["doorfast_entry-1_STATUS"])
        self.assertEqual(2, len(hass.removed))


if __name__ == "__main__":
    unittest.main()
