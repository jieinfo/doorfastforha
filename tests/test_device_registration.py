"""Device linking contracts without a running Home Assistant instance."""

from __future__ import annotations

import ast
import importlib.util
import logging
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


COMPONENT = Path(__file__).parents[1] / "custom_components" / "doorfast"


class DeviceRegistry:
    """Per-entry registry boundary; deliberately excludes deprecated lookups."""

    def __init__(self):
        self.devices = {}

    def async_get_device_by_identifier(self, identifier, config_entry_id):
        return self.devices.get((config_entry_id, identifier))

    def async_get_or_create(self, *, config_entry_id, identifiers, name,
                            manufacturer, sw_version=None, via_device_id=None):
        key = (config_entry_id, next(iter(identifiers)))
        if key not in self.devices:
            self.devices[key] = types.SimpleNamespace(
                id=f"device-{len(self.devices) + 1}", name_by_user=None, area_id=None,
            )
        device = self.devices[key]
        device.name = name
        device.manufacturer = manufacturer
        device.sw_version = sw_version
        if via_device_id is not None:
            device.via_device_id = via_device_id
        return device


def load_component(name):
    # Isolate these imports from the existing tests' dependency-free HA stubs.
    package = types.ModuleType("custom_components.doorfast")
    package.__path__ = [str(COMPONENT)]
    entity = types.ModuleType("homeassistant.helpers.entity")
    entity.Entity = object
    with patch.dict(sys.modules, {
        "custom_components.doorfast": package,
        "homeassistant.helpers.entity": entity,
    }):
        spec = importlib.util.spec_from_file_location(
            f"custom_components.doorfast.{name}", COMPONENT / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module


class DeviceRegistrationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = DeviceRegistry()
        self.hass = types.SimpleNamespace(data={}, devices=self.registry)
        self.station_module = load_component("station_entity")
        self.dr = types.ModuleType("homeassistant.helpers.device_registry")
        self.dr.async_get = lambda hass: hass.devices
        helpers = types.ModuleType("homeassistant.helpers")
        helpers.device_registry = self.dr
        self.modules = patch.dict(sys.modules, {
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.device_registry": self.dr,
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def station_entity(self, entry_id="entry-1", station_id="gate_main"):
        entity = self.station_module.DoorfastStationEntity(
            entry_id, types.SimpleNamespace(station_id=station_id, name="Main Gate")
        )
        entity.hass = self.hass
        return entity

    def controller(self, entry_id="entry-1"):
        return self.registry.async_get_or_create(
            config_entry_id=entry_id, identifiers={("doorfast", entry_id)},
            name="Doorfast Bridge", manufacturer="Doorfast", sw_version="0.1.0",
        )

    def test_station_uses_registered_controller_id_without_deprecated_fields(self):
        self.controller().id = "existing-controller-id"
        info = self.station_entity().device_info
        self.assertEqual({
            "identifiers": {("doorfast", "entry-1_station_gate_main")},
            "name": "Main Gate", "manufacturer": "Doorfast",
            "via_device_id": "existing-controller-id",
        }, info)
        # Exercise the payload against an API accepting only current fields.
        device = self.registry.async_get_or_create(config_entry_id="entry-1", **info)
        self.assertEqual("existing-controller-id", device.via_device_id)

    def test_same_station_name_under_two_entries_keeps_separate_controllers(self):
        self.controller("entry-1").id = "controller-one"
        self.controller("entry-2").id = "controller-two"
        for entry_id, parent in (("entry-1", "controller-one"), ("entry-2", "controller-two")):
            with self.subTest(entry=entry_id):
                info = self.station_entity(entry_id).device_info
                self.assertEqual(parent, info.get("via_device_id"))
                self.assertEqual({("doorfast", f"{entry_id}_station_gate_main")}, info["identifiers"])

    def test_unattached_and_missing_parent_do_not_emit_invalid_links(self):
        entity = self.station_entity()
        for attached in (False, True):
            entity.hass = self.hass if attached else None
            info = entity.device_info
            self.assertNotIn("via_device", info)
            self.assertNotIn("via_device_id", info)
            self.assertEqual({("doorfast", "entry-1_station_gate_main")}, info["identifiers"])

    def test_parent_lookup_does_not_accept_another_entries_matching_identifier(self):
        other = self.registry.async_get_or_create(
            config_entry_id="entry-other", identifiers={("doorfast", "entry-1")},
            name="Other Controller", manufacturer="Doorfast",
        )
        other.id = "wrong-controller-id"
        self.assertNotIn("via_device_id", self.station_entity().device_info)

    def test_device_link_resolves_the_current_registry_after_parent_replacement(self):
        entity = self.station_entity()
        self.assertNotIn("via_device_id", entity.device_info)
        parent = self.controller()
        parent.id = "first-controller-id"
        self.assertEqual("first-controller-id", entity.device_info.get("via_device_id"))
        self.registry.devices.clear()
        self.assertNotIn("via_device_id", entity.device_info)
        self.controller().id = "replacement-controller-id"
        self.assertEqual("replacement-controller-id", entity.device_info.get("via_device_id"))

    def test_older_ha_keeps_the_supported_identifier_link(self):
        # HA 2026.7 and earlier do not accept via_device_id during registration.
        self.hass.devices = types.SimpleNamespace(
            async_get_device=lambda **kwargs: types.SimpleNamespace(id="legacy-parent")
        )
        info = self.station_entity().device_info
        self.assertEqual(("doorfast", "entry-1"), info.get("via_device"))
        self.assertNotIn("via_device_id", info)

    async def run_setup(self, entry_id="entry-1"):
        # Execute the actual entry setup, with only its external HA/network
        # boundaries substituted. This covers ordering, not source-text checks.
        source = ast.parse((COMPONENT / "__init__.py").read_text())
        setup = next(node for node in source.body
                     if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry")
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), setup], type_ignores=[])
        ast.fix_missing_locations(module)
        const = load_component("const")
        lifecycle = load_component("setup_lifecycle")
        polling = load_component("polling")
        captured = []
        station_module = self.station_module
        devices = self.registry

        class Client:
            online = True
            status = {"runtime_id": "runtime-a"}

            async def refresh(self):
                pass

            async def monitor_status(self):
                return {"runtime_id": "runtime-a", "sessions": [], "status_revision": 1}

        class StationRegistry:
            station_ids = ()

            async def async_refresh(self):
                pass

            async def async_close(self):
                pass

        class Pcm:
            def register(self):
                pass

            async def reconcile(self, *args):
                pass

            async def release_entry(self, *args):
                pass

        class Provider:
            async def async_reconcile_monitor(self):
                pass

            async def async_close_entry(self):
                pass

        class ConfigEntries:
            async def async_forward_entry_setups(self, entry, platforms):
                parent = devices.async_get_device_by_identifier(("doorfast", entry.entry_id), entry.entry_id)
                captured.append(None if parent is None else parent.id)
                # Camera and sensor platforms can run before controller entities.
                for station_id in ("gate_main", "gate_side"):
                    entity = station_module.DoorfastStationEntity(
                        entry.entry_id, types.SimpleNamespace(station_id=station_id, name=station_id)
                    )
                    entity.hass = self_hass
                    captured.append(entity.device_info)

            async def async_unload_platforms(self, *args):
                return True

        async def frontend(*args):
            pass

        class EventView:
            def __init__(self, *args):
                pass

        self_hass = self.hass
        self.hass.config_entries = ConfigEntries()
        self.hass.services = types.SimpleNamespace(
            has_service=lambda *args: True, async_remove=lambda *args: None,
        )
        self.hass.http = types.SimpleNamespace(register_view=lambda *args: None)
        entry = types.SimpleNamespace(
            entry_id=entry_id, data={"server_address": "http://bridge.invalid"}, options={},
            async_on_unload=lambda *args: None, add_update_listener=lambda *args: None,
        )
        namespace = {key: value for key, value in vars(const).items() if key.isupper()}
        namespace.update(
            dr=self.dr, DoorfastClient=lambda *args: Client(),
            StationRegistryCoordinator=lambda *args, **kwargs: StationRegistry(),
            PcmWebSocketManager=lambda *args: Pcm(), DoorfastWebRTCProvider=lambda *args, **kwargs: Provider(),
            EventGate=object, async_poll=polling.async_poll,
            sync_monitor_state=lifecycle.sync_monitor_state,
            rollback_entry_setup=lifecycle.rollback_entry_setup,
            async_register_frontend=frontend, async_unregister_frontend=frontend,
            async_register_webrtc_provider=lambda *args: lambda: None,
            async_dispatcher_send=lambda *args: None, is_ringing=lambda *args: False,
            async_track_time_interval=lambda *args: lambda: None,
            _async_update_listener=lambda *args: None, timedelta=lambda **kwargs: None,
            DoorfastEventView=EventView, _LOGGER=logging.getLogger(__name__),
            SERVICE_NAMES=(),
        )
        exec(compile(module, str(COMPONENT / "__init__.py"), "exec"), namespace)
        self.assertTrue(await namespace["async_setup_entry"](self.hass, entry))
        return captured

    async def test_first_setup_registers_controller_before_station_platforms(self):
        captured = await self.run_setup()
        self.assertEqual("device-1", captured[0])
        self.assertEqual(["device-1", "device-1"], [info.get("via_device_id") for info in captured[1:]])

    async def test_entry_setup_keeps_each_controllers_stations_separate(self):
        for entry_id, parent_id in (("entry-1", "device-1"), ("entry-2", "device-2")):
            captured = await self.run_setup(entry_id)
            self.assertEqual(parent_id, captured[0])
            self.assertEqual([parent_id, parent_id], [info.get("via_device_id") for info in captured[1:]])
            self.assertEqual([
                {("doorfast", f"{entry_id}_station_gate_main")},
                {("doorfast", f"{entry_id}_station_gate_side")},
            ], [info["identifiers"] for info in captured[1:]])

    async def test_reload_reuses_existing_devices_and_user_metadata(self):
        parent = self.controller()
        parent.id = "existing-controller-id"
        parent.name_by_user = "My Controller"
        parent.area_id = "entrance"
        child = self.registry.async_get_or_create(
            config_entry_id="entry-1", identifiers={("doorfast", "entry-1_station_gate_main")},
            name="Old station name", manufacturer="Doorfast", via_device_id=parent.id,
        )
        child.id = "existing-station-id"
        child.name_by_user = "My Gate"
        child.area_id = "yard"
        for _ in range(2):
            captured = await self.run_setup()
            self.assertEqual("existing-controller-id", captured[0])
            for info in captured[1:]:
                self.assertEqual("existing-controller-id", info.get("via_device_id"))
                self.registry.async_get_or_create(config_entry_id="entry-1", **info)
        self.assertEqual("existing-station-id", child.id)
        self.assertEqual(("My Gate", "yard"), (child.name_by_user, child.area_id))
        self.assertEqual(("My Controller", "entrance"), (parent.name_by_user, parent.area_id))
        self.assertEqual(3, len(self.registry.devices))
