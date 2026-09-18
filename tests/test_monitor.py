"""Lifecycle tests for the Doorfast monitor coordinator."""

import asyncio
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
spec = importlib.util.spec_from_file_location(
    "custom_components.doorfast.monitor", COMPONENT / "monitor.py"
)
monitor_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = monitor_module
assert spec.loader is not None
spec.loader.exec_module(monitor_module)
MonitorCoordinator = monitor_module.MonitorCoordinator


class FakeClient:
    def __init__(self):
        self.calls = []
        self.start_result = {"state": "queued", "generation": 7}

    async def start_monitor(self):
        self.calls.append(("start",))
        return dict(self.start_result)

    async def stop_monitor(self, generation):
        self.calls.append(("stop", generation))
        return {"state": "stopping", "generation": generation}

    async def set_monitor_viewer(self, generation, active):
        self.calls.append(("viewer", generation, active))
        return {"state": "queued", "generation": generation, "active": active}


class MonitorCoordinatorTest(unittest.IsolatedAsyncioTestCase):
    async def test_start_tracks_generation_and_status_ready(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(client, grace_seconds=0.01)

        self.assertEqual(7, await coordinator.async_start())
        await coordinator.async_apply_status(
            {"generation": 7, "state": "publishing", "status_revision": 3}
        )

        self.assertEqual(7, coordinator.generation)
        self.assertEqual("publishing", coordinator.state)
        self.assertTrue(coordinator.ready)
        self.assertEqual(3, coordinator.status_revision)
        self.assertEqual([("start",)], client.calls)

    async def test_viewer_reference_count_uses_one_doorfast_viewer(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(client, grace_seconds=0.01)

        await coordinator.async_acquire_viewer()
        await coordinator.async_acquire_viewer()
        self.assertEqual(2, coordinator.viewer_count)
        self.assertEqual(
            [("start",), ("viewer", 7, True)], client.calls
        )

        await coordinator.async_release_viewer()
        self.assertEqual(1, coordinator.viewer_count)
        await coordinator.async_release_viewer()
        self.assertEqual(0, coordinator.viewer_count)
        self.assertEqual(
            [("start",), ("viewer", 7, True), ("viewer", 7, False)],
            client.calls,
        )

        await asyncio.sleep(0.03)
        self.assertEqual(
            [("start",), ("viewer", 7, True), ("viewer", 7, False),
             ("stop", 7)],
            client.calls,
        )

    async def test_new_generation_cleans_old_viewers_and_cancels_grace_stop(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(client, grace_seconds=0.05)

        await coordinator.async_acquire_viewer()
        await coordinator.async_release_viewer()
        await coordinator.async_apply_status(
            {"generation": 8, "state": "publishing", "status_revision": 1}
        )
        self.assertEqual(8, coordinator.generation)
        self.assertEqual(0, coordinator.viewer_count)
        self.assertTrue(coordinator.ready)

        await asyncio.sleep(0.07)
        self.assertEqual([("start",), ("viewer", 7, True),
                          ("viewer", 7, False)], client.calls)

    async def test_preempt_stops_immediately_and_unload_is_idempotent(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(client, grace_seconds=10)

        await coordinator.async_acquire_viewer()
        await coordinator.async_preempt()
        self.assertEqual("idle", coordinator.state)
        self.assertIsNone(coordinator.generation)
        self.assertEqual(0, coordinator.viewer_count)
        await coordinator.async_unload()
        self.assertEqual(
            [("start",), ("viewer", 7, True), ("stop", 7)], client.calls
        )

    async def test_failed_generation_can_be_restarted(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(client, grace_seconds=0.01)

        await coordinator.async_start()
        await coordinator.async_apply_status(
            {"generation": 7, "state": "failed", "status_revision": 4}
        )
        client.start_result = {"state": "queued", "generation": 8}
        self.assertEqual(8, await coordinator.async_start())
        self.assertEqual([("start",), ("start",)], client.calls)

    async def test_preempt_and_stop_events_clear_generation(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(client, grace_seconds=10)

        await coordinator.async_acquire_viewer()
        await coordinator.async_apply_status(
            {"event": "monitor_preempted", "generation": 7}
        )
        self.assertIsNone(coordinator.generation)
        self.assertEqual(0, coordinator.viewer_count)

        await coordinator.async_acquire_viewer()
        await coordinator.async_apply_status(
            {"event": "monitor_stopped", "generation": 7}
        )
        self.assertIsNone(coordinator.generation)
        self.assertEqual(0, coordinator.viewer_count)

    async def test_wait_ready_wakes_for_publish_and_terminal_state(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(client, grace_seconds=10)
        generation = await coordinator.async_start()
        ready = asyncio.create_task(
            coordinator.async_wait_ready(generation, timeout=1)
        )
        await asyncio.sleep(0)

        await coordinator.async_apply_status(
            {"generation": generation, "state": "publishing", "status_revision": 2}
        )
        await asyncio.wait_for(ready, 0.1)

        client.start_result = {"state": "queued", "generation": 8}
        await coordinator.async_apply_status(
            {"event": "monitor_stopped", "generation": generation}
        )
        generation = await coordinator.async_start()
        failed = asyncio.create_task(
            coordinator.async_wait_ready(generation, timeout=1)
        )
        await asyncio.sleep(0)
        await coordinator.async_apply_status(
            {"event": "monitor_failed", "generation": generation}
        )
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(failed, 0.1)


if __name__ == "__main__":
    unittest.main()
