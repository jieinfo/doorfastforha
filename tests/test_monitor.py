"""Lifecycle tests for the Doorfast monitor coordinator."""

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


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
        self.start_result = {"state": "publishing", "generation": 7}

    async def start_monitor(self, runtime_id, station_id):
        self.calls.append(("start", runtime_id, station_id))
        return dict(self.start_result)

    async def stop_monitor(self, runtime_id, station_id, generation):
        self.calls.append(("stop", runtime_id, station_id, generation))
        return {"state": "stopping", "generation": generation}

    async def set_monitor_viewer(self, runtime_id, station_id, generation, active):
        self.calls.append(("viewer", runtime_id, station_id, generation, active))
        return {"state": "queued", "generation": generation, "active": active}


class MonitorCoordinatorTest(unittest.IsolatedAsyncioTestCase):
    async def test_provisional_preview_lease_does_not_wait_for_ready(self):
        client = FakeClient()
        client.start_result = {"state": "requesting", "generation": 7}
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")

        lease = await coordinator.async_acquire_viewer(wait_ready=False)

        self.assertEqual(7, lease.generation)
        self.assertTrue(coordinator.lease_owned(lease))
        self.assertFalse(coordinator.lease_active(lease))
        self.assertEqual(
            [("start", "runtime-a", "gate_main")], client.calls
        )
        await coordinator.async_release_viewer(lease)
        self.assertNotIn(
            ("viewer", "runtime-a", "gate_main", 7, False), client.calls
        )
        await coordinator.async_stop()

    async def test_shared_stopping_wait_yields_even_with_explicit_ready(self):
        coordinator = MonitorCoordinator(FakeClient(), "runtime-a", "gate_main")
        lease = await coordinator.async_acquire_viewer()
        await coordinator.async_apply_status({
            "generation": 7, "state": "stopping", "ready": True,
            "encoder_running": True, "status_revision": 1,
        })
        wait = asyncio.create_task(coordinator.async_wait_ready(7))
        try:
            await asyncio.sleep(0)
            self.assertFalse(wait.done())
            self.assertTrue(coordinator.lease_owned(lease))
            await coordinator.async_apply_status({
                "generation": 7, "state": "publishing", "ready": True,
                "encoder_running": True, "status_revision": 2,
            })
            await asyncio.wait_for(wait, 1)
        finally:
            wait.cancel()
            await asyncio.gather(wait, return_exceptions=True)
            await coordinator.async_stop()

    async def test_second_viewer_preserves_published_generation_during_stopping(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        first = await coordinator.async_acquire_viewer()
        await coordinator.async_apply_status({
            "generation": 7, "state": "stopping", "ready": False,
            "encoder_running": True, "status_revision": 1,
        })
        client.start_result = {"generation": 8, "state": "publishing"}
        second_task = asyncio.create_task(coordinator.async_acquire_viewer())
        try:
            for _ in range(10):
                await asyncio.sleep(0)
            self.assertTrue(coordinator.lease_owned(first))
            self.assertEqual(7, coordinator.generation)
            self.assertNotIn(("stop", "runtime-a", "gate_main", 7), client.calls)
            self.assertFalse(second_task.done())
            await coordinator.async_apply_status({
                "generation": 7, "state": "publishing", "ready": True,
                "encoder_running": True, "status_revision": 2,
            })
            second = await asyncio.wait_for(second_task, 1)
            self.assertEqual(7, second.generation)
            self.assertTrue(coordinator.lease_owned(first))
            self.assertTrue(coordinator.lease_owned(second))
            self.assertEqual(2, coordinator.viewer_count)
            self.assertEqual(1, client.calls.count(("start", "runtime-a", "gate_main")))
        finally:
            second_task.cancel()
            await asyncio.gather(second_task, return_exceptions=True)
            await coordinator.async_stop()

    async def test_start_works_when_eager_task_requires_explicit_loop(self):
        original_task = asyncio.Task

        def task_requiring_loop(coro, *, loop=None, eager_start=False, **kwargs):
            if eager_start and loop is None:
                coro.close()
                raise AttributeError("'NoneType' object has no attribute 'is_running'")
            return original_task(coro, loop=loop, eager_start=eager_start, **kwargs)

        client = FakeClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")

        with patch.object(monitor_module.asyncio, "Task", task_requiring_loop):
            generation = await coordinator.async_start()

        self.assertEqual(7, generation)
        self.assertEqual([("start", "runtime-a", "gate_main")], client.calls)

    async def test_acquire_recovers_same_generation_after_stopping(self):
        class GatedClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.started = asyncio.Event()
                self.start_results = [
                    {"state": "queued", "generation": 7},
                    {"state": "publishing", "generation": 8},
                ]

            async def start_monitor(self, runtime_id, station_id):
                self.calls.append(("start", runtime_id, station_id))
                response = self.start_results.pop(0)
                self.started.set()
                return dict(response)

        client = GatedClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        acquire = asyncio.create_task(coordinator.async_acquire_viewer())
        await client.started.wait()
        await coordinator.async_apply_status(
            {"generation": 7, "state": "stopping", "status_revision": 1}
        )

        lease = await asyncio.wait_for(acquire, 1)
        self.assertEqual((8, 1), (lease.generation, lease.lease_id))
        self.assertEqual(1, coordinator.viewer_count)
        self.assertEqual(2, client.calls.count(("start", "runtime-a", "gate_main")))

    async def test_acquire_recovers_after_stopping_then_monitor_stopped(self):
        class GatedClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.started = asyncio.Event()
                self.start_results = [
                    {"state": "queued", "generation": 7},
                    {"state": "publishing", "generation": 8},
                ]

            async def start_monitor(self, runtime_id, station_id):
                self.calls.append(("start", runtime_id, station_id))
                response = self.start_results.pop(0)
                self.started.set()
                return dict(response)

        client = GatedClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        acquire = asyncio.create_task(coordinator.async_acquire_viewer())
        await client.started.wait()
        await coordinator.async_apply_status(
            {"generation": 7, "state": "stopping", "status_revision": 1}
        )
        await coordinator.async_apply_status(
            {"event": "monitor_stopped", "generation": 7, "status_revision": 2}
        )

        lease = await asyncio.wait_for(acquire, 1)
        self.assertEqual(8, lease.generation)
        self.assertEqual(1, coordinator.viewer_count)
        self.assertEqual(2, client.calls.count(("start", "runtime-a", "gate_main")))

    async def test_parallel_acquires_follow_one_recovered_generation(self):
        class GatedClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.started = asyncio.Event()
                self.start_results = [
                    {"state": "queued", "generation": 7},
                    {"state": "publishing", "generation": 8},
                ]

            async def start_monitor(self, runtime_id, station_id):
                self.calls.append(("start", runtime_id, station_id))
                response = self.start_results.pop(0)
                self.started.set()
                return dict(response)

        client = GatedClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        first = asyncio.create_task(coordinator.async_acquire_viewer())
        second = asyncio.create_task(coordinator.async_acquire_viewer())
        await client.started.wait()
        await coordinator.async_apply_status(
            {"generation": 7, "state": "stopping", "status_revision": 1}
        )

        left, right = await asyncio.wait_for(asyncio.gather(first, second), 1)
        self.assertEqual((8, 8), (left.generation, right.generation))
        self.assertNotEqual(left.lease_id, right.lease_id)
        self.assertEqual(2, coordinator.viewer_count)
        self.assertEqual(2, client.calls.count(("start", "runtime-a", "gate_main")))

    async def test_pending_acquires_survive_stopping_and_requesting_before_first_frame(self):
        class RetryingClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.start_results = [
                    {"state": "queued", "generation": 7},
                    {"state": "requesting", "generation": 8},
                ]
                self.restarted = asyncio.Event()

            async def start_monitor(self, runtime_id, station_id):
                self.calls.append(("start", runtime_id, station_id))
                response = self.start_results.pop(0)
                if response["generation"] == 8:
                    self.restarted.set()
                return response

        client = RetryingClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        first = asyncio.create_task(coordinator.async_acquire_viewer())
        second = asyncio.create_task(coordinator.async_acquire_viewer())
        await asyncio.sleep(0)
        await coordinator.async_apply_status(
            {"generation": 7, "state": "stopping", "status_revision": 1}
        )
        await asyncio.wait_for(client.restarted.wait(), 1)
        self.assertFalse(first.done())
        self.assertFalse(second.done())
        self.assertEqual(0, coordinator.viewer_count)

        await coordinator.async_apply_status(
            {"generation": 8, "state": "publishing", "ready": True, "status_revision": 2}
        )
        left, right = await asyncio.wait_for(asyncio.gather(first, second), 1)
        self.assertEqual((8, 8), (left.generation, right.generation))
        self.assertNotEqual(left.lease_id, right.lease_id)
        self.assertEqual(2, coordinator.viewer_count)
        self.assertEqual(
            [("start", "runtime-a", "gate_main"),
             ("stop", "runtime-a", "gate_main", 7),
             ("start", "runtime-a", "gate_main"),
             ("viewer", "runtime-a", "gate_main", 8, True)],
            client.calls,
        )

    async def test_cancelling_follower_does_not_stop_other_pending_acquire(self):
        class SlowStartClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.start_result = {"state": "queued", "generation": 7}
                self.start_entered = asyncio.Event()
                self.finish_start = asyncio.Event()

            async def start_monitor(self, runtime_id, station_id):
                self.calls.append(("start", runtime_id, station_id))
                self.start_entered.set()
                await self.finish_start.wait()
                return dict(self.start_result)

        client = SlowStartClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        follower_waiting = asyncio.Event()
        original_wait = coordinator.async_wait_ready

        async def observe_wait(generation, timeout=None):
            if asyncio.current_task() is second:
                follower_waiting.set()
            return await original_wait(generation, timeout)

        coordinator.async_wait_ready = observe_wait
        first = asyncio.create_task(coordinator.async_acquire_viewer())
        await client.start_entered.wait()
        second = asyncio.create_task(coordinator.async_acquire_viewer())
        await asyncio.sleep(0)
        client.finish_start.set()
        await asyncio.wait_for(follower_waiting.wait(), 1)
        second.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second

        self.assertNotIn(("stop", "runtime-a", "gate_main", 7), client.calls)
        await coordinator.async_apply_status(
            {"generation": 7, "state": "publishing", "ready": True, "status_revision": 1}
        )
        lease = await asyncio.wait_for(first, 1)
        self.assertEqual(7, lease.generation)
        self.assertEqual(1, coordinator.viewer_count)
        self.assertEqual(1, client.calls.count(("start", "runtime-a", "gate_main")))

    async def test_cancelling_starter_keeps_generation_for_pending_follower(self):
        client = FakeClient()
        client.start_result = {"state": "queued", "generation": 7}
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        first = asyncio.create_task(coordinator.async_acquire_viewer())
        await asyncio.sleep(0)
        second = asyncio.create_task(coordinator.async_acquire_viewer())
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first

        self.assertNotIn(("stop", "runtime-a", "gate_main", 7), client.calls)
        await coordinator.async_apply_status(
            {"generation": 7, "state": "publishing", "ready": True, "status_revision": 1}
        )
        lease = await asyncio.wait_for(second, 1)
        self.assertEqual(7, lease.generation)
        self.assertEqual(1, coordinator.viewer_count)

    async def test_last_pending_cancel_stops_shared_generation_once(self):
        client = FakeClient()
        client.start_result = {"state": "queued", "generation": 7}
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        first = asyncio.create_task(coordinator.async_acquire_viewer())
        await asyncio.sleep(0)
        second = asyncio.create_task(coordinator.async_acquire_viewer())
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        second.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second

        self.assertIsNone(coordinator.generation)
        self.assertEqual(0, coordinator.viewer_count)
        self.assertEqual(1, client.calls.count(("stop", "runtime-a", "gate_main", 7)))

    async def test_new_pending_acquire_prevents_queued_cancel_cleanup(self):
        client = FakeClient()
        client.start_result = {"state": "queued", "generation": 7}
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        first = asyncio.create_task(coordinator.async_acquire_viewer())
        await asyncio.sleep(0)
        self.assertEqual(7, coordinator.generation)

        await coordinator._lifecycle_lock.acquire()
        try:
            first.cancel()
            for _ in range(10):
                await asyncio.sleep(0)
                if not coordinator._pending_acquires:
                    break
            self.assertFalse(coordinator._pending_acquires)

            second = asyncio.create_task(coordinator.async_acquire_viewer())
            for _ in range(10):
                await asyncio.sleep(0)
                if len(coordinator._pending_acquires) == 1:
                    break
            self.assertEqual(1, len(coordinator._pending_acquires))
        finally:
            coordinator._lifecycle_lock.release()

        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertNotIn(("stop", "runtime-a", "gate_main", 7), client.calls)
        await coordinator.async_apply_status(
            {"generation": 7, "state": "publishing", "ready": True, "status_revision": 1}
        )
        lease = await asyncio.wait_for(second, 1)
        self.assertEqual(7, lease.generation)
        self.assertEqual(1, client.calls.count(("start", "runtime-a", "gate_main")))

    async def test_acquire_waiting_on_admission_cannot_cross_terminal_epoch(self):
        class BlockingStopClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.start_results = [
                    {"state": "queued", "generation": 7},
                    {"state": "publishing", "generation": 8},
                ]
                self.stop_entered = asyncio.Event()
                self.release_stop = asyncio.Event()

            async def start_monitor(self, runtime_id, station_id):
                self.calls.append(("start", runtime_id, station_id))
                return dict(self.start_results.pop(0))

            async def stop_monitor(self, runtime_id, station_id, generation):
                self.calls.append(("stop", runtime_id, station_id, generation))
                if not self.stop_entered.is_set():
                    self.stop_entered.set()
                    await self.release_stop.wait()
                return {"state": "stopping", "generation": generation}

        for terminal in ("async_stop", "async_preempt"):
            with self.subTest(terminal=terminal):
                client = BlockingStopClient()
                coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
                first = asyncio.create_task(coordinator.async_acquire_viewer())
                await asyncio.sleep(0)
                first.cancel()
                await asyncio.wait_for(client.stop_entered.wait(), 1)

                second = asyncio.create_task(coordinator.async_acquire_viewer())
                await asyncio.sleep(0)
                self.assertFalse(second.done())
                terminate = asyncio.create_task(getattr(coordinator, terminal)())
                await asyncio.sleep(0)
                self.assertEqual(1, coordinator._terminal_epoch)
                client.release_stop.set()

                with self.assertRaises(asyncio.CancelledError):
                    await first
                await asyncio.wait_for(terminate, 1)
                with self.assertRaisesRegex(RuntimeError, "monitor request was terminated"):
                    await asyncio.wait_for(second, 1)
                self.assertEqual(1, client.calls.count(("start", "runtime-a", "gate_main")))
                self.assertEqual(0, coordinator.viewer_count)

    async def test_failed_follower_does_not_stop_other_pending_acquire(self):
        client = FakeClient()
        client.start_result = {"state": "queued", "generation": 7}
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        original_wait = coordinator.async_wait_ready
        follower_waiting = asyncio.Event()
        second = None

        async def fail_follower(generation, timeout=None):
            if asyncio.current_task() is second:
                follower_waiting.set()
                raise RuntimeError("follower wait failed")
            await original_wait(generation, timeout)

        coordinator.async_wait_ready = fail_follower
        first = asyncio.create_task(coordinator.async_acquire_viewer())
        await asyncio.sleep(0)
        second = asyncio.create_task(coordinator.async_acquire_viewer())
        await asyncio.wait_for(follower_waiting.wait(), 1)
        with self.assertRaisesRegex(RuntimeError, "follower wait failed"):
            await second

        self.assertNotIn(("stop", "runtime-a", "gate_main", 7), client.calls)
        await coordinator.async_apply_status(
            {"generation": 7, "state": "publishing", "ready": True, "status_revision": 1}
        )
        lease = await asyncio.wait_for(first, 1)
        self.assertEqual(7, lease.generation)
        self.assertEqual(1, coordinator.viewer_count)

    async def test_failed_later_acquire_does_not_stop_previously_leased_generation(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main", grace_seconds=10)
        lease = await coordinator.async_acquire_viewer()
        await coordinator.async_release_viewer(lease)

        async def failed_wait(generation, timeout=None):
            raise RuntimeError("waiting failed")

        coordinator.async_wait_ready = failed_wait
        with self.assertRaisesRegex(RuntimeError, "waiting failed"):
            await coordinator.async_acquire_viewer()

        self.assertEqual(7, coordinator.generation)
        self.assertNotIn(("stop", "runtime-a", "gate_main", 7), client.calls)

    async def test_acquire_recovers_if_stopping_arrives_before_viewer_registration(self):
        class GatedClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.started = asyncio.Event()
                self.start_results = [
                    {"state": "publishing", "generation": 7},
                    {"state": "publishing", "generation": 8},
                ]

            async def start_monitor(self, runtime_id, station_id):
                self.calls.append(("start", runtime_id, station_id))
                response = self.start_results.pop(0)
                self.started.set()
                return dict(response)

        client = GatedClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        wait_returned = asyncio.Event()
        release_wait = asyncio.Event()
        original_wait = coordinator.async_wait_ready

        async def gated_wait(generation, timeout=25.0):
            await original_wait(generation, timeout)
            wait_returned.set()
            await release_wait.wait()

        coordinator.async_wait_ready = gated_wait
        acquire = asyncio.create_task(coordinator.async_acquire_viewer())
        await client.started.wait()
        await wait_returned.wait()
        await coordinator.async_apply_status(
            {"generation": 7, "state": "stopping", "status_revision": 1}
        )
        release_wait.set()

        lease = await asyncio.wait_for(acquire, 1)
        self.assertEqual(8, lease.generation)
        self.assertEqual(1, coordinator.viewer_count)
        self.assertEqual(2, client.calls.count(("start", "runtime-a", "gate_main")))

    async def test_preempted_event_during_recovery_stop_does_not_start_new_generation(self):
        class BlockingClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.start_entered = asyncio.Event()
                self.stop_entered = asyncio.Event()
                self.release_stop = asyncio.Event()
                self.start_results = [
                    {"state": "queued", "generation": 7},
                    {"state": "publishing", "generation": 8},
                ]

            async def start_monitor(self, runtime_id, station_id):
                self.calls.append(("start", runtime_id, station_id))
                self.start_entered.set()
                return dict(self.start_results.pop(0))

            async def stop_monitor(self, runtime_id, station_id, generation):
                self.calls.append(("stop", runtime_id, station_id, generation))
                self.stop_entered.set()
                await self.release_stop.wait()
                return {"state": "stopped", "generation": generation}

        client = BlockingClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        acquire = asyncio.create_task(coordinator.async_acquire_viewer())
        await client.start_entered.wait()
        await coordinator.async_apply_status(
            {"generation": 7, "state": "stopping", "status_revision": 1}
        )
        await client.stop_entered.wait()
        terminal = asyncio.create_task(
            coordinator.async_apply_status(
                {
                    "event": "monitor_preempted",
                    "generation": 7,
                    "status_revision": 2,
                }
            )
        )
        await terminal
        client.release_stop.set()
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(acquire, 1)
        self.assertNotIn(("start", "runtime-a", "gate_main"), client.calls[1:])
        self.assertEqual(0, coordinator.viewer_count)

    async def test_failed_event_during_recovery_stop_does_not_start_new_generation(self):
        class BlockingClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.start_entered = asyncio.Event()
                self.stop_entered = asyncio.Event()
                self.release_stop = asyncio.Event()
                self.start_results = [
                    {"state": "queued", "generation": 7},
                    {"state": "publishing", "generation": 8},
                ]

            async def start_monitor(self, runtime_id, station_id):
                self.calls.append(("start", runtime_id, station_id))
                self.start_entered.set()
                return dict(self.start_results.pop(0))

            async def stop_monitor(self, runtime_id, station_id, generation):
                self.calls.append(("stop", runtime_id, station_id, generation))
                self.stop_entered.set()
                await self.release_stop.wait()
                return {"state": "stopped", "generation": generation}

        client = BlockingClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        acquire = asyncio.create_task(coordinator.async_acquire_viewer())
        await client.start_entered.wait()
        await coordinator.async_apply_status(
            {"generation": 7, "state": "stopping", "status_revision": 1}
        )
        await client.stop_entered.wait()
        await coordinator.async_apply_status(
            {
                "event": "monitor_failed",
                "generation": 7,
                "status_revision": 2,
            }
        )
        client.release_stop.set()
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(acquire, 1)
        self.assertNotIn(("start", "runtime-a", "gate_main"), client.calls[1:])
        self.assertEqual(0, coordinator.viewer_count)

    async def test_async_preempt_during_recovery_stop_does_not_start_new_generation(self):
        class BlockingClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.start_entered = asyncio.Event()
                self.stop_entered = asyncio.Event()
                self.release_stop = asyncio.Event()
                self.start_results = [
                    {"state": "queued", "generation": 7},
                    {"state": "publishing", "generation": 8},
                ]

            async def start_monitor(self, runtime_id, station_id):
                self.calls.append(("start", runtime_id, station_id))
                self.start_entered.set()
                return dict(self.start_results.pop(0))

            async def stop_monitor(self, runtime_id, station_id, generation):
                self.calls.append(("stop", runtime_id, station_id, generation))
                self.stop_entered.set()
                await self.release_stop.wait()
                return {"state": "stopped", "generation": generation}

        client = BlockingClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        acquire = asyncio.create_task(coordinator.async_acquire_viewer())
        await client.start_entered.wait()
        await coordinator.async_apply_status(
            {"generation": 7, "state": "stopping", "status_revision": 1}
        )
        await client.stop_entered.wait()
        terminal_marked = asyncio.Event()
        original_begin_terminal = coordinator._begin_terminal

        def mark_terminal(*, unload=False):
            result = original_begin_terminal(unload=unload)
            terminal_marked.set()
            return result

        coordinator._begin_terminal = mark_terminal
        preempt = asyncio.create_task(coordinator.async_preempt())
        await terminal_marked.wait()
        client.release_stop.set()
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(acquire, 1)
        await asyncio.wait_for(preempt, 1)
        self.assertNotIn(("start", "runtime-a", "gate_main"), client.calls[1:])
        self.assertEqual(0, coordinator.viewer_count)

    async def test_lease_active_requires_current_ready_epoch_and_registration(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        lease = await coordinator.async_acquire_viewer()
        self.assertTrue(coordinator.lease_active(lease))
        await coordinator.async_apply_status(
            {"event": "monitor_preempted", "generation": lease.generation}
        )
        self.assertFalse(coordinator.lease_active(lease))

    async def test_lease_stays_owned_while_publication_is_temporarily_not_ready(self):
        coordinator = MonitorCoordinator(FakeClient(), "runtime-a", "gate_main")
        lease = await coordinator.async_acquire_viewer()
        await coordinator.async_apply_status(
            {"generation": 7, "state": "publishing", "ready": True,
             "encoder_running": True, "status_revision": 7}
        )
        await coordinator.async_apply_status(
            {"generation": 7, "state": "requesting", "ready": False,
             "encoder_running": True, "status_revision": 8}
        )

        self.assertTrue(coordinator.lease_owned(lease))
        self.assertFalse(coordinator.lease_active(lease))
        self.assertFalse(coordinator.ready)
        self.assertTrue(coordinator.publisher_running)

        await coordinator.async_apply_status(
            {"generation": 7, "state": "failed", "status_revision": 9}
        )
        self.assertFalse(coordinator.lease_owned(lease))
        self.assertIsNone(coordinator.publisher_running)
        await coordinator.async_apply_status(
            {"generation": 7, "state": "publishing", "ready": True,
             "encoder_running": True, "status_revision": 8}
        )
        self.assertFalse(coordinator.lease_owned(lease))
        self.assertIsNone(coordinator.publisher_running)

    async def test_explicit_ready_and_publisher_status_override_state_heuristics(self):
        coordinator = MonitorCoordinator(FakeClient(), "runtime-a", "gate_main")
        await coordinator.async_start()
        await coordinator.async_apply_status(
            {"generation": 7, "state": "publishing", "ready": False,
             "encoder_running": "true", "status_revision": 1}
        )
        self.assertFalse(coordinator.ready)
        self.assertIsNone(coordinator.publisher_running)

        await coordinator.async_apply_status(
            {"generation": 7, "state": "requesting", "ready": True,
             "encoder_running": False, "status_revision": 2}
        )
        self.assertTrue(coordinator.ready)
        self.assertFalse(coordinator.publisher_running)
        await coordinator.async_apply_status(
            {"generation": 7, "state": "publishing", "ready": "false",
             "status_revision": 3}
        )
        self.assertTrue(coordinator.ready)
        self.assertFalse(coordinator.publisher_running)

    async def test_preempt_and_local_stop_clear_publisher_and_lease(self):
        coordinator = MonitorCoordinator(FakeClient(), "runtime-a", "gate_main")
        lease = await coordinator.async_acquire_viewer()
        await coordinator.async_apply_status(
            {"generation": 7, "state": "publishing", "encoder_running": True,
             "status_revision": 1}
        )
        await coordinator.async_apply_status(
            {"event": "monitor_preempted", "generation": 7,
             "status_revision": 2}
        )
        self.assertFalse(coordinator.lease_owned(lease))
        self.assertIsNone(coordinator.publisher_running)

        coordinator = MonitorCoordinator(FakeClient(), "runtime-a", "gate_main")
        lease = await coordinator.async_acquire_viewer()
        await coordinator.async_apply_status(
            {"generation": 7, "state": "publishing", "encoder_running": True,
             "status_revision": 1}
        )
        await coordinator.async_stop()
        self.assertFalse(coordinator.lease_owned(lease))
        self.assertIsNone(coordinator.publisher_running)

    async def test_explicit_preempt_does_not_leave_a_recoverable_generation(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        await coordinator.async_acquire_viewer()
        await coordinator.async_preempt()
        self.assertIsNone(coordinator._recoverable_generation)

        client.start_result = {"state": "publishing", "generation": 8}
        lease = await coordinator.async_acquire_viewer()

        self.assertEqual(8, lease.generation)
        self.assertEqual(
            [("stop", "runtime-a", "gate_main", 7)],
            [call for call in client.calls if call[0] == "stop"],
        )

    async def test_failed_event_during_pending_start_terminates_accepted_generation(self):
        class GatedClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.start_entered = asyncio.Event()
                self.release_start = asyncio.Event()

            async def start_monitor(self, runtime_id, station_id):
                self.calls.append(("start", runtime_id, station_id))
                self.start_entered.set()
                await self.release_start.wait()
                return dict(self.start_result)

        client = GatedClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        acquire = asyncio.create_task(coordinator.async_acquire_viewer())
        await client.start_entered.wait()
        await coordinator.async_apply_status(
            {
                "event": "monitor_failed",
                "generation": 7,
                "status_revision": 1,
            }
        )
        client.release_start.set()

        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(acquire, 1)
        self.assertEqual(0, coordinator.viewer_count)
        self.assertIn(("stop", "runtime-a", "gate_main", 7), client.calls)

    async def test_cancelled_pending_start_stops_accepted_generation(self):
        class GatedClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.start_accepted = asyncio.Event()
                self.finish_start = asyncio.Event()
                self.start_result = {"state": "publishing", "generation": 8}

            async def start_monitor(self, runtime_id, station_id):
                self.calls.append(("start", runtime_id, station_id))
                self.start_accepted.set()
                await self.finish_start.wait()
                return dict(self.start_result)

        client = GatedClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        acquire = asyncio.create_task(coordinator.async_acquire_viewer())
        await client.start_accepted.wait()
        acquire.cancel()
        client.finish_start.set()

        with self.assertRaises(asyncio.CancelledError):
            await acquire
        self.assertIn(("stop", "runtime-a", "gate_main", 8), client.calls)
        self.assertEqual(0, coordinator.viewer_count)

        await coordinator.async_apply_status(
            {"event": "monitor_stopped", "generation": 7}
        )
        self.assertNotIn(("stop", "runtime-a", "gate_main", 7), client.calls)

    async def test_cancelled_start_after_response_stops_accepted_generation(self):
        class GatedClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.start_accepted = asyncio.Event()
                self.finish_start = asyncio.Event()
                self.start_result = {"state": "publishing", "generation": 8}

            async def start_monitor(self, runtime_id, station_id):
                self.calls.append(("start", runtime_id, station_id))
                self.start_accepted.set()
                await self.finish_start.wait()
                return dict(self.start_result)

        client = GatedClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        response_processed = asyncio.Event()
        original_response_generation = coordinator._response_generation

        def observe_response(response):
            generation = original_response_generation(response)
            response_processed.set()
            return generation

        coordinator._response_generation = observe_response
        acquire = asyncio.create_task(coordinator.async_acquire_viewer())
        await client.start_accepted.wait()
        await coordinator._lock.acquire()
        client.finish_start.set()
        await response_processed.wait()
        acquire.cancel()
        coordinator._lock.release()

        with self.assertRaises(asyncio.CancelledError):
            await acquire
        self.assertIn(("stop", "runtime-a", "gate_main", 8), client.calls)
        self.assertEqual(0, coordinator.viewer_count)

    async def test_cancelled_acquire_after_start_response_stops_own_generation(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        wait_entered = asyncio.Event()
        release_wait = asyncio.Event()

        async def gated_wait(generation, timeout=25.0):
            wait_entered.set()
            await release_wait.wait()

        coordinator.async_wait_ready = gated_wait
        acquire = asyncio.create_task(coordinator.async_acquire_viewer())
        await wait_entered.wait()
        acquire.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await acquire
        self.assertIn(("stop", "runtime-a", "gate_main", 7), client.calls)
        self.assertEqual(0, coordinator.viewer_count)

    async def test_wait_failure_stops_unleased_generation_once(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")

        async def failed_wait(generation, timeout=None):
            raise RuntimeError("publisher failed before first frame")

        coordinator.async_wait_ready = failed_wait
        with self.assertRaisesRegex(RuntimeError, "publisher failed before first frame"):
            await coordinator.async_acquire_viewer()

        self.assertEqual(0, coordinator.viewer_count)
        self.assertIsNone(coordinator.generation)
        self.assertEqual(1, client.calls.count(("stop", "runtime-a", "gate_main", 7)))

    async def test_cancelled_pending_retry_stops_only_current_unleased_generation(self):
        class RetryingClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.start_results = [
                    {"state": "queued", "generation": 7},
                    {"state": "requesting", "generation": 8},
                ]
                self.restarted = asyncio.Event()

            async def start_monitor(self, runtime_id, station_id):
                self.calls.append(("start", runtime_id, station_id))
                response = self.start_results.pop(0)
                if response["generation"] == 8:
                    self.restarted.set()
                return response

        client = RetryingClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        acquire = asyncio.create_task(coordinator.async_acquire_viewer())
        await asyncio.sleep(0)
        await coordinator.async_apply_status(
            {"generation": 7, "state": "stopping", "status_revision": 1}
        )
        await asyncio.wait_for(client.restarted.wait(), 1)
        acquire.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await acquire
        self.assertEqual(0, coordinator.viewer_count)
        self.assertIsNone(coordinator.generation)
        self.assertEqual(1, client.calls.count(("stop", "runtime-a", "gate_main", 7)))
        self.assertEqual(1, client.calls.count(("stop", "runtime-a", "gate_main", 8)))

    async def test_cancelled_acquire_during_viewer_enable_cleans_viewer_edge(self):
        class GatedClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.viewer_enabled = asyncio.Event()
                self.release_viewer = asyncio.Event()

            async def set_monitor_viewer(self, runtime_id, station_id, generation, active):
                self.calls.append(("viewer", runtime_id, station_id, generation, active))
                if active:
                    self.viewer_enabled.set()
                    await self.release_viewer.wait()
                return {"state": "publishing", "generation": generation}

        client = GatedClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        acquire = asyncio.create_task(coordinator.async_acquire_viewer())
        await client.viewer_enabled.wait()
        acquire.cancel()
        client.release_viewer.set()

        with self.assertRaises(asyncio.CancelledError):
            await acquire
        self.assertIn(("viewer", "runtime-a", "gate_main", 7, False), client.calls)
        self.assertIn(("stop", "runtime-a", "gate_main", 7), client.calls)
        self.assertEqual(0, coordinator.viewer_count)

    async def test_cancelled_after_viewer_enable_before_lease_registration_cleans_edge(self):
        class CancellingClient(FakeClient):
            async def set_monitor_viewer(self, runtime_id, station_id, generation, active):
                self.calls.append(("viewer", runtime_id, station_id, generation, active))
                if active:
                    asyncio.current_task().cancel()
                return {"state": "publishing", "generation": generation}

        client = CancellingClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        acquire = asyncio.create_task(coordinator.async_acquire_viewer())

        with self.assertRaises(asyncio.CancelledError):
            await acquire
        self.assertIn(("viewer", "runtime-a", "gate_main", 7, False), client.calls)
        self.assertIn(("stop", "runtime-a", "gate_main", 7), client.calls)
        self.assertEqual(0, coordinator.viewer_count)

    async def test_invalid_registration_release_failure_stops_and_resets_generation(self):
        class GatedFailingClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.viewer_enabled = asyncio.Event()
                self.release_viewer = asyncio.Event()

            async def set_monitor_viewer(self, runtime_id, station_id, generation, active):
                self.calls.append(("viewer", runtime_id, station_id, generation, active))
                if active:
                    self.viewer_enabled.set()
                    await self.release_viewer.wait()
                else:
                    raise RuntimeError("viewer disable failed")
                return {"state": "publishing", "generation": generation}

        client = GatedFailingClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        acquire = asyncio.create_task(coordinator.async_acquire_viewer())
        await client.viewer_enabled.wait()
        await coordinator.async_apply_status(
            {"generation": 7, "state": "stopping", "status_revision": 1}
        )
        client.release_viewer.set()

        with self.assertRaises(RuntimeError):
            await acquire
        self.assertIn(("viewer", "runtime-a", "gate_main", 7, False), client.calls)
        self.assertIn(("stop", "runtime-a", "gate_main", 7), client.calls)
        self.assertIsNone(coordinator.generation)
        self.assertEqual(0, coordinator.viewer_count)

    async def test_stopping_invalid_registration_clears_recoverable_marker_before_next_acquire(self):
        class GatedClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.viewer_enabled = asyncio.Event()
                self.release_viewer = asyncio.Event()
                self.start_results = [
                    {"state": "publishing", "generation": 7},
                    {"state": "queued", "generation": 8},
                ]
                self.second_start = asyncio.Event()

            async def start_monitor(self, runtime_id, station_id):
                self.calls.append(("start", runtime_id, station_id))
                result = dict(self.start_results.pop(0))
                if len(self.calls) > 1:
                    self.second_start.set()
                return result

            async def set_monitor_viewer(self, runtime_id, station_id, generation, active):
                self.calls.append(("viewer", runtime_id, station_id, generation, active))
                if active:
                    self.viewer_enabled.set()
                    await self.release_viewer.wait()
                return {"state": "publishing", "generation": generation}

        client = GatedClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        acquire = asyncio.create_task(coordinator.async_acquire_viewer())
        await client.viewer_enabled.wait()
        await coordinator.async_apply_status(
            {"generation": 7, "state": "stopping", "status_revision": 1}
        )
        client.release_viewer.set()

        await client.second_start.wait()
        self.assertIsNone(coordinator._recoverable_generation)
        await coordinator.async_apply_status(
            {"generation": 8, "state": "publishing", "status_revision": 2}
        )
        lease = await asyncio.wait_for(acquire, 1)
        self.assertEqual(8, lease.generation)

    async def test_cancelled_second_acquire_does_not_stop_existing_generation(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        existing = await coordinator.async_acquire_viewer()
        wait_entered = asyncio.Event()
        release_wait = asyncio.Event()

        async def gated_wait(generation, timeout=25.0):
            wait_entered.set()
            await release_wait.wait()

        coordinator.async_wait_ready = gated_wait
        acquire = asyncio.create_task(coordinator.async_acquire_viewer())
        await wait_entered.wait()
        acquire.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await acquire
        self.assertNotIn(("stop", "runtime-a", "gate_main", 7), client.calls)
        self.assertEqual(1, coordinator.viewer_count)
        await coordinator.async_release_viewer(existing)

    async def test_pending_start_response_does_not_overwrite_stopping_status(self):
        class GatedClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.start_entered = asyncio.Event()
                self.release_start = asyncio.Event()
                self.start_results = [
                    {"state": "queued", "generation": 7},
                    {"state": "publishing", "generation": 8},
                ]

            async def start_monitor(self, runtime_id, station_id):
                self.calls.append(("start", runtime_id, station_id))
                response = self.start_results.pop(0)
                if response["generation"] == 7:
                    self.start_entered.set()
                    await self.release_start.wait()
                return dict(response)

        client = GatedClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        acquire = asyncio.create_task(coordinator.async_acquire_viewer())
        await client.start_entered.wait()
        await coordinator.async_apply_status(
            {"generation": 7, "state": "stopping", "status_revision": 1}
        )
        client.release_start.set()

        lease = await asyncio.wait_for(acquire, 1)
        self.assertEqual(8, lease.generation)
        self.assertEqual(2, client.calls.count(("start", "runtime-a", "gate_main")))

    async def test_start_response_does_not_overwrite_newer_publishing_status(self):
        class GatedClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.start_entered = asyncio.Event()
                self.release_start = asyncio.Event()

            async def start_monitor(self, runtime_id, station_id):
                self.calls.append(("start", runtime_id, station_id))
                self.start_entered.set()
                await self.release_start.wait()
                return {"state": "queued", "generation": 7}

        client = GatedClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        start = asyncio.create_task(coordinator.async_start())
        await client.start_entered.wait()
        await coordinator.async_apply_status(
            {"generation": 7, "state": "publishing", "status_revision": 1}
        )
        client.release_start.set()

        self.assertEqual(7, await start)
        self.assertEqual("publishing", coordinator.state)
        self.assertTrue(coordinator.ready)
        self.assertEqual(1, coordinator.status_revision)

    async def test_recovery_start_response_does_not_overwrite_newer_status(self):
        class GatedClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.start_entered = asyncio.Event()
                self.release_start = asyncio.Event()

            async def start_monitor(self, runtime_id, station_id):
                self.calls.append(("start", runtime_id, station_id))
                self.start_entered.set()
                await self.release_start.wait()
                return {"state": "queued", "generation": 7}

        client = GatedClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        client.release_start.set()
        await coordinator.async_start()
        await coordinator.async_apply_status(
            {"event": "monitor_stopped", "generation": 7, "status_revision": 1}
        )
        client.start_entered.clear()
        client.release_start.clear()
        acquire = asyncio.create_task(coordinator.async_acquire_viewer())
        await client.start_entered.wait()
        await coordinator.async_apply_status(
            {"generation": 7, "state": "publishing", "status_revision": 2}
        )
        client.release_start.set()

        lease = await asyncio.wait_for(acquire, 1)
        self.assertEqual(7, lease.generation)
        self.assertEqual("publishing", coordinator.state)
        self.assertTrue(coordinator.ready)
        self.assertEqual(2, coordinator.status_revision)

    async def test_release_failure_restores_lease_for_later_cleanup(self):
        class FlakyClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.fail_release = True

            async def set_monitor_viewer(self, runtime_id, station_id, generation, active):
                self.calls.append(("viewer", runtime_id, station_id, generation, active))
                if not active and self.fail_release:
                    self.fail_release = False
                    raise RuntimeError("viewer release failed")
                return {"state": "publishing", "generation": generation}

        client = FlakyClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        lease = await coordinator.async_acquire_viewer()
        with self.assertRaises(RuntimeError):
            await coordinator.async_release_viewer(lease)
        self.assertEqual(1, coordinator.viewer_count)
        await coordinator.async_release_viewer(lease)
        self.assertEqual(0, coordinator.viewer_count)

    async def test_pending_acquire_outlives_previous_ready_deadline(self):
        client = FakeClient()
        client.start_result = {"state": "queued", "generation": 7}
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        loop = asyncio.get_running_loop()
        real_time = loop.time
        elapsed = 0

        def advanced_time():
            return real_time() + elapsed

        with patch.object(loop, "time", advanced_time), patch.object(
            loop, "slow_callback_duration", 30
        ):
            acquire = asyncio.create_task(coordinator.async_acquire_viewer())
            await asyncio.sleep(0)
            elapsed = 26
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(acquire.done())
            await coordinator.async_apply_status(
                {"generation": 7, "state": "publishing", "ready": True, "status_revision": 1}
            )
            lease = await asyncio.wait_for(acquire, 1)

        self.assertEqual(7, lease.generation)
        self.assertEqual(1, coordinator.viewer_count)

    async def test_unbounded_wait_finishes_without_leaving_waiter_task(self):
        client = FakeClient()
        client.start_result = {"state": "queued", "generation": 7}
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main")
        generation = await coordinator.async_start()
        ready = asyncio.create_task(coordinator.async_wait_ready(generation))
        await asyncio.sleep(0)
        self.assertFalse(ready.done())

        await coordinator.async_apply_status(
            {"generation": 7, "state": "publishing", "ready": True, "status_revision": 1}
        )
        await asyncio.wait_for(ready, 1)
        self.assertFalse(
            any(
                task.get_coro().__qualname__.endswith("MonitorCoordinator._wait_ready")
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
            )
        )

    async def test_start_tracks_generation_and_status_ready(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main", grace_seconds=0.01)

        self.assertEqual(7, await coordinator.async_start())
        await coordinator.async_apply_status(
            {"generation": 7, "state": "publishing", "status_revision": 3}
        )

        self.assertEqual(7, coordinator.generation)
        self.assertEqual("publishing", coordinator.state)
        self.assertTrue(coordinator.ready)
        self.assertEqual(3, coordinator.status_revision)
        self.assertEqual([("start", "runtime-a", "gate_main")], client.calls)

    async def test_stale_idle_snapshot_does_not_clear_active_generation(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(
            client, "runtime-a", "gate_main", grace_seconds=0.01
        )

        await coordinator.async_acquire_viewer()
        await coordinator.async_apply_status(
            {"generation": 7, "state": "publishing", "status_revision": 10}
        )

        # A poll started before the publishing event can return later with an
        # older idle snapshot. It must not invalidate the active viewer.
        await coordinator.async_apply_status(
            {"generation": 0, "state": "idle", "status_revision": 9}
        )

        self.assertEqual(7, coordinator.generation)
        self.assertEqual("publishing", coordinator.state)
        self.assertTrue(coordinator.ready)
        self.assertEqual(1, coordinator.viewer_count)

    async def test_newer_requesting_status_clears_ready(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(
            client, "runtime-a", "gate_main", grace_seconds=0.01
        )

        await coordinator.async_acquire_viewer()
        await coordinator.async_apply_status(
            {"generation": 7, "state": "publishing", "status_revision": 10}
        )
        await coordinator.async_apply_status(
            {"generation": 7, "state": "requesting", "status_revision": 11}
        )

        self.assertEqual(7, coordinator.generation)
        self.assertEqual("requesting", coordinator.state)
        self.assertFalse(coordinator.ready)

    async def test_stale_stop_event_does_not_clear_newer_status(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(
            client, "runtime-a", "gate_main", grace_seconds=0.01
        )

        await coordinator.async_start()
        await coordinator.async_apply_status(
            {"generation": 7, "state": "publishing", "status_revision": 10}
        )
        await coordinator.async_apply_status(
            {
                "event": "monitor_stopped",
                "generation": 7,
                "status_revision": 9,
            }
        )

        self.assertEqual(7, coordinator.generation)
        self.assertTrue(coordinator.ready)

    async def test_terminal_event_revision_prevents_generation_revival(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(
            client, "runtime-a", "gate_main", grace_seconds=0.01
        )

        await coordinator.async_start()
        await coordinator.async_apply_status(
            {"generation": 7, "state": "publishing", "status_revision": 10}
        )
        await coordinator.async_apply_status(
            {
                "event": "monitor_stopped",
                "generation": 7,
                "status_revision": 12,
            }
        )
        await coordinator.async_apply_status(
            {"generation": 7, "state": "publishing", "status_revision": 11}
        )

        self.assertIsNone(coordinator.generation)
        self.assertEqual("idle", coordinator.state)
        self.assertEqual(12, coordinator.status_revision)

    async def test_stale_prior_generation_is_ignored_before_identity_check(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(
            client, "runtime-a", "gate_main", grace_seconds=0.01
        )
        client.start_result = {
            "state": "publishing",
            "generation": 8,
            "status_revision": 10,
        }

        await coordinator.async_start()
        await coordinator.async_apply_status(
            {"generation": 7, "state": "publishing", "status_revision": 9}
        )

        self.assertEqual(8, coordinator.generation)
        self.assertEqual(10, coordinator.status_revision)

    async def test_stale_poll_waiting_on_start_lock_is_ignored(self):
        class BlockingClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.start_entered = asyncio.Event()
                self.release_start = asyncio.Event()

            async def start_monitor(self, runtime_id, station_id):
                self.calls.append(("start", runtime_id, station_id))
                self.start_entered.set()
                await self.release_start.wait()
                return {
                    "state": "publishing",
                    "generation": 7,
                    "status_revision": 2,
                }

        client = BlockingClient()
        coordinator = MonitorCoordinator(
            client, "runtime-a", "gate_main", grace_seconds=0.01
        )
        starting = asyncio.create_task(coordinator.async_start())
        await client.start_entered.wait()
        stale_poll = asyncio.create_task(
            coordinator.async_apply_status(
                {"generation": 0, "state": "idle", "status_revision": 1}
            )
        )
        await asyncio.sleep(0)

        client.release_start.set()
        await starting
        await stale_poll

        self.assertEqual(7, coordinator.generation)
        self.assertEqual("publishing", coordinator.state)
        self.assertEqual(2, coordinator.status_revision)

    async def test_viewer_reference_count_uses_one_doorfast_viewer(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main", grace_seconds=0.01)

        first = await coordinator.async_acquire_viewer()
        second = await coordinator.async_acquire_viewer()
        self.assertEqual(2, coordinator.viewer_count)
        self.assertEqual(
            [("start", "runtime-a", "gate_main"),
             ("viewer", "runtime-a", "gate_main", 7, True)], client.calls
        )

        await coordinator.async_release_viewer(first)
        self.assertEqual(1, coordinator.viewer_count)
        await coordinator.async_release_viewer(second)
        self.assertEqual(0, coordinator.viewer_count)
        self.assertEqual(
            [("start", "runtime-a", "gate_main"),
             ("viewer", "runtime-a", "gate_main", 7, True),
             ("viewer", "runtime-a", "gate_main", 7, False)],
            client.calls,
        )

        await asyncio.sleep(0.03)
        self.assertEqual(
            [("start", "runtime-a", "gate_main"),
             ("viewer", "runtime-a", "gate_main", 7, True),
             ("viewer", "runtime-a", "gate_main", 7, False),
             ("stop", "runtime-a", "gate_main", 7)],
            client.calls,
        )

    async def test_late_release_from_old_generation_does_not_touch_new_generation(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main", grace_seconds=10)

        old = await coordinator.async_acquire_viewer()
        self.assertEqual(7, old.generation)
        await coordinator.async_apply_status(
            {"event": "monitor_stopped", "generation": 7, "status_revision": 1}
        )
        client.start_result = {"state": "publishing", "generation": 8}
        new = await coordinator.async_acquire_viewer()
        await coordinator.async_release_viewer(old)
        self.assertEqual(1, coordinator.viewer_count)
        self.assertEqual(8, coordinator.generation)
        await coordinator.async_release_viewer(new)
        self.assertEqual(0, coordinator.viewer_count)

    async def test_acquire_waits_for_publishing_before_enabling_viewer(self):
        class StrictClient(FakeClient):
            async def set_monitor_viewer(self, runtime_id, station_id, generation, active):
                if self.start_result["state"] == "queued":
                    raise RuntimeError("viewer is not allowed before publishing")
                return await super().set_monitor_viewer(
                    runtime_id, station_id, generation, active
                )

        client = StrictClient()
        client.start_result = {"state": "queued", "generation": 7}
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main", grace_seconds=0.01)
        task = asyncio.create_task(coordinator.async_acquire_viewer())
        await asyncio.sleep(0)
        self.assertEqual([("start", "runtime-a", "gate_main")], client.calls)

        client.start_result = {"state": "publishing", "generation": 7}
        await coordinator.async_apply_status(
            {"generation": 7, "state": "publishing", "status_revision": 1}
        )
        lease = await asyncio.wait_for(task, 1)
        self.assertEqual(7, lease.generation)
        self.assertEqual(
            [("start", "runtime-a", "gate_main"),
             ("viewer", "runtime-a", "gate_main", 7, True)],
            client.calls,
        )

    async def test_new_generation_status_is_rejected_and_keeps_active_viewers(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main", grace_seconds=0.05)

        await coordinator.async_acquire_viewer()
        with self.assertRaises(ValueError):
            await coordinator.async_apply_status(
                {"generation": 8, "state": "publishing", "status_revision": 1}
            )
        self.assertEqual(7, coordinator.generation)
        self.assertEqual(1, coordinator.viewer_count)
        self.assertTrue(coordinator.ready)
        self.assertEqual(
            [("start", "runtime-a", "gate_main"),
             ("viewer", "runtime-a", "gate_main", 7, True)],
            client.calls,
        )

    async def test_preempt_stops_immediately_and_unload_is_idempotent(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main", grace_seconds=10)

        await coordinator.async_acquire_viewer()
        await coordinator.async_preempt()
        self.assertEqual("idle", coordinator.state)
        self.assertIsNone(coordinator.generation)
        self.assertEqual(0, coordinator.viewer_count)
        await coordinator.async_unload()
        self.assertEqual(
            [("start", "runtime-a", "gate_main"),
             ("viewer", "runtime-a", "gate_main", 7, True),
             ("stop", "runtime-a", "gate_main", 7)], client.calls
        )

    async def test_failed_generation_can_be_restarted(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main", grace_seconds=0.01)

        await coordinator.async_start()
        await coordinator.async_apply_status(
            {"generation": 7, "state": "failed", "status_revision": 4}
        )
        client.start_result = {"state": "queued", "generation": 8}
        self.assertEqual(8, await coordinator.async_start())
        self.assertEqual(
            [("start", "runtime-a", "gate_main"),
             ("start", "runtime-a", "gate_main")],
            client.calls,
        )

    async def test_acquire_during_stopping_restarts_generation(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main", grace_seconds=10)

        lease = await coordinator.async_acquire_viewer()
        await coordinator.async_release_viewer(lease)
        await coordinator.async_apply_status(
            {"generation": 7, "state": "stopping", "status_revision": 1}
        )
        self.assertFalse(coordinator.ready)
        client.start_result = {"state": "publishing", "generation": 8}

        lease = await coordinator.async_acquire_viewer()
        self.assertEqual(8, lease.generation)
        self.assertEqual("publishing", coordinator.state)
        self.assertEqual(1, coordinator.viewer_count)
        self.assertEqual(
            [
                ("start", "runtime-a", "gate_main"),
                ("viewer", "runtime-a", "gate_main", 7, True),
                ("viewer", "runtime-a", "gate_main", 7, False),
                ("stop", "runtime-a", "gate_main", 7),
                ("start", "runtime-a", "gate_main"),
                ("viewer", "runtime-a", "gate_main", 8, True),
            ],
            client.calls,
        )

    async def test_stop_conflict_is_idempotent_for_local_lifecycle(self):
        class ConflictError(Exception):
            status = 409

        class ConflictClient(FakeClient):
            async def stop_monitor(self, runtime_id, station_id, generation):
                self.calls.append(("stop", runtime_id, station_id, generation))
                raise ConflictError("already stopped")

        client = ConflictClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main", grace_seconds=0)
        await coordinator.async_start()

        await coordinator.async_stop()

        self.assertEqual("idle", coordinator.state)
        self.assertIsNone(coordinator.generation)
        self.assertFalse(coordinator.ready)
        self.assertEqual(0, coordinator.viewer_count)

    async def test_preempt_and_stop_events_clear_generation(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main", grace_seconds=10)

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
        coordinator = MonitorCoordinator(client, "runtime-a", "gate_main", grace_seconds=10)
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

    async def test_two_stations_emit_isolated_mutation_tuples(self):
        client = FakeClient()
        main = MonitorCoordinator(client, "runtime-a", "gate_main", grace_seconds=10)
        side = MonitorCoordinator(client, "runtime-a", "gate_side", grace_seconds=10)

        self.assertEqual(7, await main.async_start())
        client.start_result = {"state": "publishing", "generation": 8}
        self.assertEqual(8, await side.async_start())

        self.assertEqual(
            [
                ("start", "runtime-a", "gate_main"),
                ("start", "runtime-a", "gate_side"),
            ],
            client.calls,
        )

    async def test_rejects_mismatched_runtime_station_and_generation(self):
        client = FakeClient()
        coordinator = MonitorCoordinator(
            client, "runtime-a", "gate_main", grace_seconds=10
        )
        await coordinator.async_start()

        mismatches = (
            {"runtime_id": "runtime-b", "station_id": "gate_main", "generation": 7},
            {"runtime_id": "runtime-a", "station_id": "gate_side", "generation": 7},
            {"runtime_id": "runtime-a", "station_id": "gate_main", "generation": 8},
        )
        for mismatch in mismatches:
            with self.subTest(mismatch=mismatch):
                with self.assertRaises(ValueError):
                    await coordinator.async_apply_status(
                        {**mismatch, "event": "monitor_preempted"}
                    )
                self.assertEqual(7, coordinator.generation)

    async def test_capacity_busy_start_leaves_coordinator_idle(self):
        class BusyClient(FakeClient):
            async def start_monitor(self, runtime_id, station_id):
                self.calls.append(("start", runtime_id, station_id))
                raise RuntimeError("monitor capacity exhausted")

        client = BusyClient()
        coordinator = MonitorCoordinator(
            client, "runtime-a", "gate_main", grace_seconds=10
        )

        with self.assertRaisesRegex(RuntimeError, "capacity exhausted"):
            await coordinator.async_start()

        self.assertIsNone(coordinator.generation)
        self.assertEqual("idle", coordinator.state)

    async def test_preempting_one_station_does_not_change_the_other(self):
        client = FakeClient()
        main = MonitorCoordinator(client, "runtime-a", "gate_main", grace_seconds=10)
        side = MonitorCoordinator(client, "runtime-a", "gate_side", grace_seconds=10)
        await main.async_start()
        client.start_result = {"state": "publishing", "generation": 8}
        await side.async_start()

        await main.async_apply_status(
            {
                "runtime_id": "runtime-a",
                "station_id": "gate_main",
                "event": "monitor_preempted",
                "generation": 7,
            }
        )

        self.assertIsNone(main.generation)
        self.assertEqual(8, side.generation)


if __name__ == "__main__":
    unittest.main()
