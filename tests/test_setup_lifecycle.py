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

    async def async_apply_status(self, status):
        self.applied.append(status)


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
