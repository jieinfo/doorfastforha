"""Lifecycle tests for the per-entry Doorfast station registry."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


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
load_component("const")
load_component("monitor")

entity_module = types.ModuleType("homeassistant.helpers.entity")
entity_module.Entity = object
sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
sys.modules.setdefault("homeassistant.helpers", types.ModuleType("homeassistant.helpers"))
sys.modules["homeassistant.helpers.entity"] = entity_module

stations_module = load_component("stations")
station_entity_module = load_component("station_entity")

DoorfastStation = client_types.DoorfastStation
DoorfastStationSnapshot = client_types.DoorfastStationSnapshot
StationRegistryCoordinator = stations_module.StationRegistryCoordinator
DoorfastStationEntity = station_entity_module.DoorfastStationEntity


def station(station_id, *, enabled=True, name=None, stream_name=None):
    suffix = "01" if station_id == "gate_main" else "02"
    return DoorfastStation(
        station_id=station_id,
        name=name or station_id.replace("_", " ").title(),
        logical_address=f"32:02:01:00:02:{suffix}",
        enabled=enabled,
        stream_name=stream_name or f"doorfast_{station_id}",
        route_source="discovered",
        route_fresh=True,
        monitorable=True,
        last_seen_ms=123,
    )


def snapshot(runtime_id, revision, *stations):
    return DoorfastStationSnapshot(runtime_id, revision, tuple(stations))


class FakeClient:
    def __init__(self, *snapshots):
        self.snapshots = list(snapshots)
        self.calls = 0

    async def stations(self):
        self.calls += 1
        return self.snapshots.pop(0)


class FakeMonitor:
    def __init__(self, station_id, closed):
        self.station_id = station_id
        self.closed = closed

    async def async_close(self):
        self.closed.append(self.station_id)


class FailingMonitor(FakeMonitor):
    async def async_close(self):
        await super().async_close()
        raise RuntimeError(f"failed to close {self.station_id}")


class StationRegistryTest(unittest.IsolatedAsyncioTestCase):
    def make_registry(self, client, closed):
        return StationRegistryCoordinator(
            client,
            entry_id="entry-1",
            monitor_factory=lambda _client, _runtime_id, item: FakeMonitor(
                item.station_id, closed
            ),
        )

    async def test_initial_enumeration_and_stable_monitor_identity(self):
        closed = []
        client = FakeClient(
            snapshot(
                "0123456789abcdef",
                1,
                station("gate_main"),
                station("gate_side"),
                station("gate_off", enabled=False),
            )
        )
        registry = self.make_registry(client, closed)
        events = []
        registry.add_listener(lambda event, station_id: events.append((event, station_id)))

        self.assertTrue(await registry.async_refresh())

        self.assertEqual(("gate_main", "gate_side"), registry.station_ids)
        self.assertIs(registry.monitor("gate_main"), registry.monitor("gate_main"))
        self.assertEqual(
            [("added", "gate_main"), ("added", "gate_side")], events
        )
        self.assertEqual([], closed)

    async def test_updates_identity_in_place_and_ignores_unchanged_revision(self):
        closed = []
        client = FakeClient(
            snapshot("0123456789abcdef", 1, station("gate_main")),
            snapshot("0123456789abcdef", 2, station("gate_main", name="Front Gate")),
            snapshot("0123456789abcdef", 2, station("gate_main", name="Ignored")),
        )
        registry = self.make_registry(client, closed)
        events = []
        registry.add_listener(lambda event, station_id: events.append((event, station_id)))
        await registry.async_refresh()
        monitor = registry.monitor("gate_main")

        self.assertTrue(await registry.async_refresh())
        self.assertFalse(await registry.async_refresh())

        self.assertEqual("Front Gate", registry.station("gate_main").name)
        self.assertIs(monitor, registry.monitor("gate_main"))
        self.assertEqual(
            [("added", "gate_main"), ("updated", "gate_main")], events
        )
        self.assertEqual([], closed)

    async def test_disable_and_removal_close_before_notification(self):
        order = []
        client = FakeClient(
            snapshot(
                "0123456789abcdef",
                1,
                station("gate_main"),
                station("gate_side"),
            ),
            snapshot(
                "0123456789abcdef",
                2,
                station("gate_main", enabled=False),
            ),
        )
        registry = StationRegistryCoordinator(
            client,
            entry_id="entry-1",
            monitor_factory=lambda _client, _runtime_id, item: FakeMonitor(
                item.station_id, order
            ),
        )
        registry.add_listener(
            lambda event, station_id: order.append(f"{event}:{station_id}")
        )
        await registry.async_refresh()
        order.clear()

        await registry.async_refresh()

        self.assertEqual((), registry.station_ids)
        self.assertEqual(
            [
                "gate_main",
                "removed:gate_main",
                "gate_side",
                "removed:gate_side",
            ],
            order,
        )

    async def test_runtime_restart_rebuilds_all_monitors(self):
        closed = []
        client = FakeClient(
            snapshot("0123456789abcdef", 3, station("gate_main")),
            snapshot("fedcba9876543210", 1, station("gate_main")),
        )
        registry = self.make_registry(client, closed)
        events = []
        registry.add_listener(lambda event, station_id: events.append((event, station_id)))
        await registry.async_refresh()
        first_monitor = registry.monitor("gate_main")

        await registry.async_refresh()

        self.assertEqual(["gate_main"], closed)
        self.assertIsNot(first_monitor, registry.monitor("gate_main"))
        self.assertEqual(
            [
                ("added", "gate_main"),
                ("removed", "gate_main"),
                ("added", "gate_main"),
            ],
            events,
        )

    async def test_close_cleans_monitors_and_listeners(self):
        closed = []
        client = FakeClient(snapshot("0123456789abcdef", 1, station("gate_main")))
        registry = self.make_registry(client, closed)
        events = []
        registry.add_listener(lambda event, station_id: events.append((event, station_id)))
        await registry.async_refresh()
        events.clear()

        await registry.async_close()

        self.assertEqual(["gate_main"], closed)
        self.assertEqual((), registry.station_ids)
        self.assertEqual([], events)

    async def test_close_cleans_every_monitor_when_one_close_fails(self):
        closed = []
        client = FakeClient(
            snapshot(
                "0123456789abcdef",
                1,
                station("gate_main"),
                station("gate_side"),
            )
        )
        registry = StationRegistryCoordinator(
            client,
            entry_id="entry-1",
            monitor_factory=lambda _client, _runtime_id, item: (
                FailingMonitor(item.station_id, closed)
                if item.station_id == "gate_main"
                else FakeMonitor(item.station_id, closed)
            ),
        )
        registry.add_listener(lambda _event, _station_id: None)
        await registry.async_refresh()

        with self.assertRaisesRegex(RuntimeError, "failed to close gate_main"):
            await registry.async_close()

        self.assertEqual(["gate_main", "gate_side"], closed)
        self.assertEqual((), registry.station_ids)

    async def test_failed_removal_keeps_monitor_for_retry(self):
        closed = []
        client = FakeClient(
            snapshot("0123456789abcdef", 1, station("gate_main")),
            snapshot("0123456789abcdef", 2),
        )
        registry = StationRegistryCoordinator(
            client,
            entry_id="entry-1",
            monitor_factory=lambda _client, _runtime_id, item: FailingMonitor(
                item.station_id, closed
            ),
        )
        await registry.async_refresh()
        monitor = registry.monitor("gate_main")

        with self.assertRaisesRegex(RuntimeError, "failed to close gate_main"):
            await registry.async_refresh()

        self.assertEqual(("gate_main",), registry.station_ids)
        self.assertIs(monitor, registry.monitor("gate_main"))

    def test_station_entity_uses_child_device_identity(self):
        entity = DoorfastStationEntity("entry-1", station("gate_main"))

        self.assertEqual(
            {
                "identifiers": {("doorfast", "entry-1_station_gate_main")},
                "name": "Gate Main",
                "manufacturer": "Doorfast",
                "via_device": ("doorfast", "entry-1"),
            },
            entity.device_info,
        )


if __name__ == "__main__":
    unittest.main()
