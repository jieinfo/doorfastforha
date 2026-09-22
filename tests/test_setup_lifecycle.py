from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / "doorfast"


class FakeServices:
    def __init__(self, names=()):
        self.names = set(names)
        self.removed = []

    def has_service(self, domain, name):
        return (domain, name) in self.names

    def async_remove(self, domain, name):
        self.removed.append((domain, name))
        self.names.discard((domain, name))


class FakeConfigEntries:
    def __init__(self, should_fail=False):
        self.should_fail = should_fail
        self.unload_calls = []

    async def async_unload_platforms(self, entry, platforms):
        self.unload_calls.append((entry.entry_id, tuple(platforms)))
        if self.should_fail:
            raise RuntimeError("platform unload failed")


class FakeHass:
    def __init__(self, service_names=()):
        self.data = {}
        self.services = FakeServices(("doorfast", name) for name in service_names)
        self.config_entries = FakeConfigEntries()


class FakeEntry:
    entry_id = "entry-1"


class FakePcm:
    def __init__(self):
        self.released = []

    async def release_entry(self, entry_id):
        self.released.append(entry_id)


class FakeMonitor:
    def __init__(self):
        self.closed = 0

    async def async_close(self):
        self.closed += 1


class FakeProvider:
    def __init__(self):
        self.closed = 0
        self.reconciled = 0

    async def async_close_entry(self):
        self.closed += 1

    async def async_reconcile_monitor(self):
        self.reconciled += 1


class FakeRegistry:
    def __init__(self):
        self.closed = 0

    async def async_close(self):
        self.closed += 1


class FakeStatusMonitor:
    def __init__(self):
        self.applied = []
        self.generation = None
        self.status_revision = 0

    async def async_apply_status(self, status):
        self.applied.append(status)


class FakeStationRegistry:
    def __init__(self):
        self.monitors = {
            "gate_main": FakeStatusMonitor(),
            "gate_side": FakeStatusMonitor(),
        }

    @property
    def station_ids(self):
        return tuple(self.monitors)

    def monitor(self, station_id):
        return self.monitors[station_id]


def load_module():
    # Import the helper without loading Home Assistant-dependent package code.
    package = types.ModuleType("custom_components.doorfast")
    package.__path__ = [str(COMPONENT)]
    sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
    sys.modules["custom_components.doorfast"] = package
    const_spec = importlib.util.spec_from_file_location(
        "custom_components.doorfast.const", COMPONENT / "const.py"
    )
    const_module = importlib.util.module_from_spec(const_spec)
    sys.modules["custom_components.doorfast.const"] = const_module
    assert const_spec.loader is not None
    const_spec.loader.exec_module(const_module)
    spec = importlib.util.spec_from_file_location(
        "custom_components.doorfast.setup_lifecycle", COMPONENT / "setup_lifecycle.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


MODULE = load_module()


class SetupRollbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_poll_does_not_clear_monitor_start_in_flight(self):
        registry = FakeStationRegistry()
        provider = FakeProvider()
        client = types.SimpleNamespace(status={"runtime_id": "runtime-a"})

        async def monitor_status():
            registry.monitors["gate_main"].generation = 7
            registry.monitors["gate_main"].status_revision = 1
            return {"runtime_id": "runtime-a", "status_revision": 9, "sessions": []}

        client.monitor_status = monitor_status
        await MODULE.sync_monitor_state(client, registry, provider)

        self.assertEqual([], registry.monitors["gate_main"].applied)
        self.assertEqual(
            ["idle"],
            [item.get("state") for item in registry.monitors["gate_side"].applied],
        )

    async def test_poll_clears_generation_missing_before_and_after_request(self):
        registry = FakeStationRegistry()
        registry.monitors["gate_main"].generation = 7
        registry.monitors["gate_main"].status_revision = 3
        provider = FakeProvider()
        client = types.SimpleNamespace(status={"runtime_id": "runtime-a"})

        async def monitor_status():
            return {"runtime_id": "runtime-a", "status_revision": 9, "sessions": []}

        client.monitor_status = monitor_status
        await MODULE.sync_monitor_state(
            client, registry, provider, station_id="gate_main"
        )

        self.assertEqual(
            ["idle"],
            [item.get("state") for item in registry.monitors["gate_main"].applied],
        )

    async def test_polls_authoritative_sessions_by_station_only(self):
        registry = FakeStationRegistry()
        provider = FakeProvider()
        client = types.SimpleNamespace(
            status={"runtime_id": "runtime-a"},
            monitor_status=lambda: None,
        )

        async def monitor_status():
            return {
                "runtime_id": "runtime-a",
                "sessions": [
                    {"station_id": "gate_main", "generation": 7,
                     "state": "publishing", "status_revision": 4},
                    {"station_id": "gate_side", "generation": 7,
                     "state": "viewing", "status_revision": 5},
                    {"station_id": "unknown", "generation": 9,
                     "state": "publishing", "status_revision": 6},
                ],
            }

        client.monitor_status = monitor_status
        await MODULE.sync_monitor_state(client, registry, provider)

        self.assertEqual(
            [
                {"runtime_id": "runtime-a", "station_id": "gate_main",
                 "generation": 7, "state": "publishing", "status_revision": 4}
            ],
            registry.monitors["gate_main"].applied,
        )
        self.assertEqual(
            [
                {"runtime_id": "runtime-a", "station_id": "gate_side",
                 "generation": 7, "state": "viewing", "status_revision": 5}
            ],
            registry.monitors["gate_side"].applied,
        )
        self.assertEqual(1, provider.reconciled)

    async def test_session_uses_global_revision_when_it_is_newer(self):
        registry = FakeStationRegistry()
        provider = FakeProvider()
        client = types.SimpleNamespace(status={"runtime_id": "runtime-a"})

        async def monitor_status():
            return {
                "runtime_id": "runtime-a",
                "status_revision": 143,
                "sessions": [
                    {
                        "station_id": "gate_main",
                        "generation": 7,
                        "state": "publishing",
                        "status_revision": 110,
                    }
                ],
            }

        client.monitor_status = monitor_status
        await MODULE.sync_monitor_state(client, registry, provider)

        self.assertEqual(143, registry.monitors["gate_main"].applied[0]["status_revision"])

    async def test_later_poll_replaces_a_relay_accelerated_station_state(self):
        registry = FakeStationRegistry()
        provider = FakeProvider()
        client = types.SimpleNamespace(status={"runtime_id": "runtime-a"})
        replies = iter((
            {
                "runtime_id": "runtime-a",
                "sessions": [
                    {"station_id": "gate_main", "generation": 7,
                     "state": "publishing", "status_revision": 4},
                    {"station_id": "gate_side", "generation": 7,
                     "state": "viewing", "status_revision": 5},
                ],
            },
            {
                "runtime_id": "runtime-a",
                "sessions": [
                    {"station_id": "gate_main", "generation": 7,
                     "state": "idle", "status_revision": 5},
                    {"station_id": "gate_side", "generation": 7,
                     "state": "viewing", "status_revision": 5},
                ],
            },
        ))

        async def monitor_status():
            return next(replies)

        client.monitor_status = monitor_status
        await MODULE.sync_monitor_state(client, registry, provider)
        await MODULE.sync_monitor_state(client, registry, provider)

        self.assertEqual("idle", registry.monitors["gate_main"].applied[-1]["state"])
        self.assertEqual("viewing", registry.monitors["gate_side"].applied[-1]["state"])

    async def test_relay_sync_updates_only_the_matching_station(self):
        registry = FakeStationRegistry()
        provider = FakeProvider()
        client = types.SimpleNamespace(status={"runtime_id": "runtime-a"})

        async def monitor_status():
            return {
                "runtime_id": "runtime-a",
                "sessions": [
                    {"station_id": "gate_main", "generation": 7,
                     "state": "publishing", "status_revision": 4},
                    {"station_id": "gate_side", "generation": 7,
                     "state": "viewing", "status_revision": 5},
                ],
            }

        client.monitor_status = monitor_status
        relay_event = {
            "runtime_id": "runtime-a",
            "station_id": "gate_main",
            "event": "monitor_preempted",
            "generation": 7,
            "status_revision": 5,
        }
        await MODULE.sync_monitor_state(
            client,
            registry,
            provider,
            station_id="gate_main",
            relay_event=relay_event,
        )

        self.assertEqual(
            ["publishing", "monitor_preempted"],
            [item.get("state", item.get("event"))
             for item in registry.monitors["gate_main"].applied],
        )
        self.assertEqual([], registry.monitors["gate_side"].applied)

    async def test_syncs_polled_media_then_reconciles_provider(self):
        monitor = FakeStatusMonitor()
        provider = FakeProvider()
        client = types.SimpleNamespace(
            status={"media": {"state": "publishing", "generation": 9}}
        )

        await MODULE.sync_monitor_state(client, monitor, provider)

        self.assertEqual([client.status["media"]], monitor.applied)
        self.assertEqual(1, provider.reconciled)

    async def test_syncs_relay_event_after_authoritative_media(self):
        monitor = FakeStatusMonitor()
        provider = FakeProvider()
        client = types.SimpleNamespace(
            status={
                "media": {"state": "idle", "generation": 0},
                "media_event": {
                    "event": "monitor_preempted",
                    "generation": 9,
                },
            }
        )

        await MODULE.sync_monitor_state(
            client, monitor, provider, include_event=True
        )

        self.assertEqual(
            [client.status["media"], client.status["media_event"]],
            monitor.applied,
        )
        self.assertEqual(1, provider.reconciled)

    async def test_cleans_all_resources_when_first_entry_setup_fails(self):
        hass = FakeHass(("unlock", "call_elevator", "answer", "hangup"))
        hass.data["doorfast"] = {FakeEntry.entry_id: object()}
        hass.data["doorfast_event_views"] = {FakeEntry.entry_id: object()}
        hass.data["doorfast_monitors"] = {FakeEntry.entry_id: object()}
        hass.data["doorfast_webrtc_providers"] = {FakeEntry.entry_id: object()}
        hass.data["doorfast_webrtc_unsubscribers"] = {
            FakeEntry.entry_id: object()
        }
        hass.data["doorfast_stations"] = {FakeEntry.entry_id: object()}
        pcm = FakePcm()
        monitor = FakeMonitor()
        provider = FakeProvider()
        registry = FakeRegistry()
        provider_unregistered = []
        unregistered = []

        async def unregister_frontend(_hass, entry_id):
            unregistered.append(entry_id)

        await MODULE.rollback_entry_setup(
            hass,
            FakeEntry(),
            pcm_ws=pcm,
            unregister_frontend=unregister_frontend,
            platforms_forward_attempted=True,
            frontend_registered=True,
            view_created=True,
            service_names=("unlock", "call_elevator", "answer", "hangup"),
            monitor=monitor,
            provider=provider,
            station_registry=registry,
            unregister_webrtc=lambda: provider_unregistered.append(True),
        )

        self.assertEqual(pcm.released, [FakeEntry.entry_id])
        self.assertEqual(unregistered, [FakeEntry.entry_id])
        self.assertNotIn("doorfast", hass.data)
        self.assertNotIn("doorfast_event_views", hass.data)
        self.assertNotIn("doorfast_monitors", hass.data)
        self.assertNotIn("doorfast_webrtc_providers", hass.data)
        self.assertNotIn("doorfast_webrtc_unsubscribers", hass.data)
        self.assertNotIn("doorfast_stations", hass.data)
        self.assertEqual(1, monitor.closed)
        self.assertEqual(1, provider.closed)
        self.assertEqual(1, registry.closed)
        self.assertEqual([True], provider_unregistered)
        self.assertEqual(
            hass.services.removed,
            [("doorfast", name) for name in ("unlock", "call_elevator", "answer", "hangup")],
        )

    async def test_preserves_resources_owned_by_another_entry(self):
        hass = FakeHass(("unlock", "call_elevator", "answer", "hangup"))
        hass.data["doorfast"] = {FakeEntry.entry_id: object(), "entry-2": object()}
        hass.data["doorfast_event_views"] = {FakeEntry.entry_id: object(), "entry-2": object()}
        hass.data["doorfast_stations"] = {FakeEntry.entry_id: object(), "entry-2": object()}
        pcm = FakePcm()
        registry = FakeRegistry()

        async def unregister_frontend(_hass, _entry_id):
            raise AssertionError("frontend was not registered for this failed entry")

        hass.config_entries.should_fail = True
        await MODULE.rollback_entry_setup(
            hass,
            FakeEntry(),
            pcm_ws=pcm,
            unregister_frontend=unregister_frontend,
            platforms_forward_attempted=True,
            frontend_registered=False,
            view_created=True,
            service_names=("unlock", "call_elevator", "answer", "hangup"),
            station_registry=registry,
        )

        self.assertEqual(pcm.released, [FakeEntry.entry_id])
        self.assertEqual(set(hass.data["doorfast"]), {"entry-2"})
        self.assertEqual(set(hass.data["doorfast_event_views"]), {"entry-2"})
        self.assertEqual(set(hass.data["doorfast_stations"]), {"entry-2"})
        self.assertEqual(1, registry.closed)
        self.assertEqual(hass.services.removed, [])


if __name__ == "__main__":
    unittest.main()
