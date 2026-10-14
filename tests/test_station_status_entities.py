"""Station status entities use scoped bridge facts without altering media leases."""

from __future__ import annotations

import asyncio
from copy import deepcopy
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
                if not hasattr(item, "cleanup"):
                    SensorEntity.__init__(item)  # Initialize the external HA test stub.
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
        self.registry.client.status["call"] = {
            "session": "talking", "station_id": "gate_main", "generation": 42,
        }
        self.assertEqual("in_call", entity.native_value)
        self.registry.client.online = False
        listener(self.registry.client.status)
        self.assertFalse(entity.available)
        self.assertIsNone(entity.native_value)
        self.assertGreater(entity.writes, 0)

    async def test_three_station_phases_expose_only_their_sampled_diagnostics(self):
        # Missing mapping, foreign history fallback, or ready=>live would break this.
        self.registry.items["gate_b2"] = station("gate_b2", "B2 gate")
        self.registry.listener("added", "gate_b2")
        self.assertEqual(7, len(self.entities))
        for station_id in ("gate_main", "gate_side", "gate_b2"):
            entity = self.find(station_id, "preview_status")
            await entity.async_added_to_hass()
            session = {"station_id": station_id, "generation": 9, "purpose": "preview",
                       "end_reason": "none", "failure_reason": "none",
                       "timer": "first_frame", "remaining_ms": 900,
                       "token": "do not expose", "address": "do not expose"}
            self.registry.client.status = {"media": {"sessions": [session], "last_results": [
                {"station_id": station_id, "generation": 8, "timer": "none", "remaining_ms": 0,
                 "end_reason": "local_hangup", "failure_reason": "cancelled"}]}}
            for state, ready, live, purpose, phase, timer, remaining, fault in (
                ("requesting", False, False, "preview", "requesting", "none", 0, "none"),
                ("awaiting_video", False, False, "preview", "awaiting_video", "first_frame", 900, "none"),
                ("viewing", True, True, "preview", "live", "none", 0, "none"),
                ("publishing", True, False, "preview", "reconnecting", "reconnect", 250, "source_stalled"),
                ("viewing", True, True, "call", "in_call", "session", 12000, "none"),
                ("failed", True, True, "preview", "failed", "none", 0, "first_frame_timeout"),
            ):
                with self.subTest(station_id=station_id, phase=phase):
                    session.update(state=state, ready=ready, source_live=live, purpose=purpose,
                                   timer=timer, remaining_ms=remaining, failure_reason=fault)
                    self.registry.client.status["call"] = {
                        "session": "talking" if purpose == "call" else "idle",
                        "station_id": station_id, "generation": 42 if purpose == "call" else 0,
                    }
                    entity._handle_status(self.registry.client.status)
                    attrs = entity.extra_state_attributes
                    self.assertEqual(phase, entity.native_value)
                    self.assertIn("monitor_timer", attrs)
                    self.assertEqual((timer, remaining, "none", fault, 8, 9),
                                     (attrs["monitor_timer"], attrs["monitor_remaining_ms"],
                                      attrs["monitor_end_reason"], attrs["monitor_failure_reason"],
                                      attrs["monitor_last_generation"], attrs["generation"]))
                    self.assertNotIn("do not expose", str(attrs))
                    for other in ("gate_main", "gate_side", "gate_b2"):
                        if other != station_id:
                            other_attrs = self.find(other, "preview_status").extra_state_attributes
                            self.assertEqual("unknown", other_attrs["monitor_timer"])
                            self.assertIsNone(other_attrs["monitor_last_generation"])
            session.update(state="viewing", ready=True, source_live="true", purpose="preview")
            self.assertIsNone(entity.native_value)
            self.assertIn("monitor_failure_reason", entity.extra_state_attributes)
            self.assertEqual("first_frame_timeout", entity.extra_state_attributes["monitor_failure_reason"])

    async def test_sensor_consumes_authoritative_outbound_stages_and_incoming_talking(self):
        entity = self.find("gate_main", "preview_status")
        side = self.find("gate_side", "preview_status")
        await entity.async_added_to_hass()
        status = {"runtime_id": "0123456789abcdef", "call": {
            "session": "idle", "station_id": "gate_main", "generation": 0},
            "station_call": {"operation_id": 17, "station_id": "gate_main", "stage": "preparing",
                "media_generation": 9, "call_generation": 0, "timer": "startup", "remaining_ms": 30000,
                "error": "none", "token": "private token"}, "media": {"sessions": []}}
        for stage, native, generation, timer, remaining, expected in (
            ("preparing", "idle", 0, "startup", 30000, "calling"),
            ("calling", "preview", 42, "confirmation", 900, "calling"),
            ("talking", "talking", 42, "session", 120000, "in_call"),
            ("ending", "talking", 42, "confirmation", 900, "stopping"),
            ("failed", "idle", 42, "none", 0, "reconnecting"),
            ("ended", "talking", 43, "none", 0, "in_call"),
        ):
            with self.subTest(stage=stage):
                if stage != "preparing":
                    status["media"]["sessions"] = [{"station_id": "gate_main", "generation": 9,
                        "purpose": "preview" if stage in ("failed", "ended") else "call",
                        "state": "viewing", "ready": True, "source_live": False}]
                status["call"].update(session=native, generation=generation)
                status["station_call"].update(stage=stage, call_generation=42 if stage != "preparing" else 0,
                    timer=timer, remaining_ms=remaining, error="media_failed" if stage == "failed" else "none")
                self.registry.client.status = status
                entity._handle_status(status)
                self.assertEqual(expected, entity.native_value)
                attrs = entity.extra_state_attributes
                self.assertEqual(stage, attrs.get("station_call_stage"))
                self.assertEqual((17, 9, 42 if stage != "preparing" else 0, timer, remaining), tuple(
                    attrs[name] for name in ("station_call_operation_id", "station_call_media_generation",
                        "station_call_call_generation", "station_call_timer", "station_call_remaining_ms")))
                self.assertNotIn("private", str(attrs))
                if stage != "preparing":
                    self.assertFalse(attrs["source_live"])
                self.assertNotEqual("in_call", side.native_value)
                self.assertIsNone(side.extra_state_attributes["station_call_operation_id"])
        self.registry.client.status = {"media": {"sessions": []}}
        self.assertEqual("idle", entity.native_value)
        self.assertIsNone(entity.extra_state_attributes.get("station_call_operation_id"))

    async def test_sensor_shows_real_incoming_talking_without_video(self):
        entity = self.find("gate_main", "preview_status")
        side = self.find("gate_side", "preview_status")
        await entity.async_added_to_hass()
        for terminal_history in (False, True):
            for media in (None, {"available": False, "sessions": []}, {}, {"sessions": []},
                          {"sessions": [{"station_id": "gate_main", "generation": 9,
                                         "state": "failed", "source_live": False}]}):
                with self.subTest(terminal_history=terminal_history, media=media):
                    status = {"runtime_id": "0123456789abcdef", "call": {
                        "session": "talking", "station_id": "gate_main", "generation": 43}}
                    if media is not None:
                        status["media"] = media
                    if terminal_history:
                        status["station_call"] = {"operation_id": 17, "station_id": "gate_main",
                            "stage": "ended", "media_generation": 9, "call_generation": 42,
                            "timer": "none", "remaining_ms": 0, "error": "cancelled"}
                    self.registry.client.status = status
                    entity._handle_status(status)
                    self.assertEqual("in_call", entity.native_value)
                    self.assertEqual(False if media and media.get("sessions") else None,
                                     entity.extra_state_attributes["source_live"])
                    self.assertNotEqual("in_call", side.native_value)

    async def test_preview_terminal_preemption_and_restart_do_not_restore_current_history(self):
        entity = self.find("gate_main", "preview_status")
        self.registry.client.status = {"media": {"preempted_station_id": "gate_main",
            "preempted_generation": 8, "sessions": [{"station_id": "gate_side", "generation": 9,
                "purpose": "call", "state": "viewing", "ready": True, "source_live": True}],
            "last_results": [{"station_id": "gate_main", "generation": 8,
                "timer": "none", "remaining_ms": 0, "end_reason": "preempted",
                "failure_reason": "preempted"}]}}
        self.assertEqual("preempted", entity.native_value)
        self.assertIn("monitor_timer", entity.extra_state_attributes)
        self.assertEqual(("none", 0, "preempted", "preempted", 8), tuple(
            entity.extra_state_attributes[field] for field in (
                "monitor_timer", "monitor_remaining_ms", "monitor_end_reason",
                "monitor_failure_reason", "monitor_last_generation")))
        self.registry.client.status = {"runtime_id": "new", "media": {
            "sessions": [], "last_results": []}}
        self.assertEqual("idle", entity.native_value)
        self.assertEqual(("unknown", None, "unknown", "unknown", None), tuple(
            entity.extra_state_attributes[field] for field in (
                "monitor_timer", "monitor_remaining_ms", "monitor_end_reason",
                "monitor_failure_reason", "monitor_last_generation")))
        self.registry.client.status = {"media": {"sessions": [{"station_id": "gate_main",
            "state": "viewing", "ready": True, "source_live": True}]}}
        self.assertEqual("live", entity.native_value)  # Existing phase contract stays independent.
        self.assertEqual("unknown", entity.extra_state_attributes["monitor_timer"])
        self.assertIsNone(entity.extra_state_attributes["monitor_last_generation"])
        self.registry.client.online = False
        self.assertIsNone(entity.extra_state_attributes["monitor_remaining_ms"])

    async def test_controller_diagnostics_keep_current_call_and_terminal_owner_distinct(self):
        controller = self.entities[0]
        status = {"call": {"session": "talking", "generation": 9, "station_id": "gate_side",
                  "timer": "session", "remaining_ms": 12000, "end_reason": "none", "fault": "none",
                  "last_station_id": "gate_main", "last_generation": 8},
                  "audio": {"ready": True, "generation": 9}}
        self.registry.client.status = status
        await controller.async_added_to_hass()
        attrs = controller.extra_state_attributes
        self.assertEqual("talking", controller.native_value)
        self.assertIn("call_timer", attrs)
        self.assertEqual(("session", 12000, "none", "none", "gate_main", 8), tuple(
            attrs[field] for field in ("call_timer", "call_remaining_ms", "call_end_reason",
                                      "call_fault", "call_last_station_id", "call_last_generation")))
        self.assertIs(status["call"], attrs["call"])
        self.assertIs(status["audio"], attrs["audio"])
        self.assertIs(status, self.registry.client.status)
        self.assertNotIn("call_timer", status)
        controller._handle_status({"call": {"session": "idle", "timer": [], "remaining_ms": 42,
                                          "last_station_id": "32:02:01:00:01:00", "last_generation": 8}})
        self.assertEqual(("unknown", None, "unknown", "unknown", None, None), tuple(
            controller.extra_state_attributes[field] for field in (
                "call_timer", "call_remaining_ms", "call_end_reason", "call_fault",
                "call_last_station_id", "call_last_generation")))
        controller._handle_status({})
        self.assertEqual("unknown", controller.native_value)
        self.assertIsNone(controller.extra_state_attributes["call_remaining_ms"])
        for call in (None, [], True, "talking"):
            controller._handle_status({"call": call})
            self.assertEqual("unknown", controller.native_value)
            self.assertEqual("unknown", controller.extra_state_attributes["call_fault"])

    async def test_controller_unknown_terminal_sentinel_preserves_raw_call_and_audio(self):
        # The actual controller must not present the wire sentinel as a terminal identity.
        controller = self.entities[0]
        status = {"call": {"session": "talking", "station_id": "gate_side", "generation": 9,
            "timer": "confirmation", "remaining_ms": 1200, "end_reason": "none",
            "fault": "confirmation_timeout", "last_station_id": "gate_main", "last_generation": 8},
            "audio": {"ready": True, "generation": 9}}
        self.registry.client.status = status
        await controller.async_added_to_hass()
        for station_id, expected_owner, expected_generation in (
            ("gate_main", "gate_main", 8), ("unknown", None, None),
        ):
            with self.subTest(station_id=station_id):
                status["call"]["last_station_id"] = station_id
                original = deepcopy(status)
                controller._handle_status(status)
                attrs = controller.extra_state_attributes
                self.assertEqual("talking", controller.native_value)
                self.assertIs(status, self.registry.client.status)
                self.assertIs(status["call"], attrs["call"])
                self.assertIs(status["audio"], attrs["audio"])
                self.assertEqual(original, status)
                self.assertEqual(("confirmation", 1200, "none", "confirmation_timeout",
                                  expected_owner, expected_generation), tuple(
                    attrs[field] for field in ("call_timer", "call_remaining_ms", "call_end_reason",
                                              "call_fault", "call_last_station_id", "call_last_generation")))

    async def test_push_and_poll_present_the_same_authoritative_diagnostics_for_three_stations(self):
        # Replacing refreshed raw status with event.status would change all these attributes.
        events = load_component("events")
        polling = load_component("polling")
        self.registry.items["gate_b2"] = station("gate_b2", "B2 gate")
        self.registry.listener("added", "gate_b2")
        controller = self.entities[0]
        await controller.async_added_to_hass()
        station_ids = ("gate_main", "gate_side", "gate_b2")
        previews = [self.find(station_id, "preview_status") for station_id in station_ids]
        for entity in previews:
            await entity.async_added_to_hass()
        snapshot = {"runtime_id": "runtime-a", "call": {
            "session": "talking", "station_id": "gate_side", "generation": 12,
            "timer": "session", "remaining_ms": 4000, "end_reason": "none", "fault": "none",
            "last_station_id": "gate_main", "last_generation": 11},
            "media": {"sessions": [
                {"station_id": station_id, "generation": 9, "status_revision": 7,
                 "state": "viewing", "purpose": "preview", "ready": True, "source_live": True,
                 "timer": "none", "remaining_ms": 0, "end_reason": "none", "failure_reason": "none"}
                for station_id in station_ids], "last_results": [
                {"station_id": station_id, "generation": 8, "timer": "none", "remaining_ms": 0,
                 "end_reason": "local_hangup", "failure_reason": "cancelled"}
                for station_id in station_ids]}}

        async def refresh():
            self.registry.client.status = deepcopy(snapshot)
            return self.registry.client.status

        async def noop():
            pass

        def dispatch(kind, value):
            if kind == "status":
                for listener in self.hass.listeners["doorfast_entry-1_STATUS"]:
                    listener(value)

        async def dispatch_poll():
            dispatch("status", self.registry.client.status)

        self.registry.client.refresh = refresh
        await polling.async_poll(self.registry.client, refresh, noop, noop, dispatch_poll)
        call_expected = {"call_timer": "session", "call_remaining_ms": 4000,
                         "call_end_reason": "none", "call_fault": "none",
                         "call_last_station_id": "gate_main", "call_last_generation": 11}
        for key, value in call_expected.items():
            self.assertEqual(value, controller.extra_state_attributes[key])
        poll_preview = [deepcopy(entity.extra_state_attributes) for entity in previews]
        gate = events.EventGate()
        for station_id in station_ids:
            payload = {"schema_version": 1, "event_id": 1, "event": "monitor_failed",
                       "generation": 9, "status_revision": 7, "timestamp_ms": 1000,
                       "runtime_id": "runtime-a", "station_id": station_id,
                       "status": {"timer": "reconnect", "remaining_ms": 100,
                                  "end_reason": "preempted", "failure_reason": "source_stalled"}}
            self.assertEqual(200, (await events.process_event(
                payload, self.registry.client, gate, dispatch, lambda _: False, station_ids))[0])
            self.assertEqual(poll_preview, [entity.extra_state_attributes for entity in previews])
            for key, value in call_expected.items():
                self.assertEqual(value, controller.extra_state_attributes[key])
            self.assertEqual(snapshot["call"], self.registry.client.status["call"])
            self.assertEqual(snapshot["media"], self.registry.client.status["media"])
            for changes in ({}, {"event_id": 2, "generation": 8},
                            {"event_id": 3, "status_revision": 6},
                            {"event_id": 4, "runtime_id": "old-runtime"},
                            {"event_id": 5, "generation": True}):
                with self.subTest(station_id=station_id, changes=changes):
                    response = await events.process_event(
                        {**payload, **changes}, self.registry.client, gate, dispatch,
                        lambda _: False, station_ids)
                    self.assertEqual(400 if changes.get("generation") is True else 202, response[0])
                    self.assertEqual(poll_preview, [entity.extra_state_attributes for entity in previews])
                    for key, value in call_expected.items():
                        self.assertEqual(value, controller.extra_state_attributes[key])
        snapshot.update(runtime_id="runtime-b")
        snapshot["media"] = {"sessions": [], "last_results": []}
        snapshot["call"] = {"session": "idle", "generation": 0,
                            "timer": "none", "remaining_ms": 0, "end_reason": "none", "fault": "none"}
        await polling.async_poll(self.registry.client, refresh, noop, noop, dispatch_poll)
        self.assertTrue(all(entity.extra_state_attributes["monitor_last_generation"] is None
                            for entity in previews))
        self.assertEqual(202, (await events.process_event(
            payload, self.registry.client, gate, dispatch, lambda _: False, station_ids))[0])
        self.assertTrue(all(entity.extra_state_attributes["monitor_end_reason"] == "unknown"
                            for entity in previews))
        self.assertIsNone(controller.extra_state_attributes["call_last_generation"])

    async def test_call_push_ignores_embedded_diagnostics_and_late_hangup(self):
        events = load_component("events")
        controller = self.entities[0]
        self.registry.client.status = {"call": {"session": "talking", "generation": 9,
            "timer": "session", "remaining_ms": 8000, "end_reason": "none", "fault": "none",
            "last_station_id": "gate_b2", "last_generation": 8}}
        await controller.async_added_to_hass()

        async def refresh():
            return self.registry.client.status

        def dispatch(kind, value):
            if kind == "status":
                controller._handle_status(value)

        self.registry.client.refresh = refresh
        payload = {"schema_version": 1, "event_id": 1, "event": "call_established",
                   "generation": 9, "timestamp_ms": 1000, "status": {
                       "call": {"end_reason": "peer_hangup", "timer": "none", "remaining_ms": 0}}}
        gate = events.EventGate()
        self.assertEqual(200, (await events.process_event(
            payload, self.registry.client, gate, dispatch, lambda _: False))[0])
        self.assertEqual("none", controller.extra_state_attributes["call_end_reason"])
        self.assertEqual(8000, controller.extra_state_attributes["call_remaining_ms"])
        for changes in ({}, {"event": "hangup", "generation": 8, "event_id": 2}):
            self.assertEqual(202, (await events.process_event(
                {**payload, **changes}, self.registry.client, gate, dispatch, lambda _: False))[0])
            self.assertEqual("none", controller.extra_state_attributes["call_end_reason"])
            self.assertEqual(8, controller.extra_state_attributes["call_last_generation"])

    async def test_native_attribute_translations_cover_entity_diagnostic_domains(self):
        # Missing/misplaced state_attributes makes these consumer values untranslated.
        timer_media = {"none", "confirmation", "session", "first_frame", "reconnect", "unknown"}
        timer_call = {"none", "confirmation", "session", "unknown"}
        ends_media = {"none", "local_hangup", "peer_hangup", "session_timeout",
                      "confirmation_timeout", "preempted", "runtime_reset", "unknown"}
        faults_call = {"none", "dispatch_failed", "dispatch_timeout", "confirmation_rejected",
                       "confirmation_timeout", "preempted", "cancelled", "runtime_reset",
                       "heartbeat_timeout", "unknown"}
        faults_media = (faults_call - {"heartbeat_timeout"}) | {
            "first_frame_timeout", "source_stalled", "encoder_failed", "publisher_failed", "capacity_busy"}
        domains = {"preview_status": {
            "monitor_timer": timer_media, "monitor_remaining_ms": None,
            "monitor_end_reason": ends_media, "monitor_failure_reason": faults_media,
            "monitor_last_generation": None}, "latest_event": {
            "call_timer": timer_call, "call_remaining_ms": None,
            "call_end_reason": ends_media | {"heartbeat_timeout"}, "call_fault": faults_call,
            "call_last_station_id": None, "call_last_generation": None}}
        for language in ("en", "zh-Hans"):
            translations = json.loads((COMPONENT / "translations" / f"{language}.json").read_text())
            for kind, fields in domains.items():
                entity = self.find("gate_main", kind) if kind == "preview_status" else self.entities[0]
                translated = translations["entity"]["sensor"][entity._attr_translation_key]
                self.assertIn("state_attributes", translated)
                attributes = translated["state_attributes"]
                self.assertEqual(set(fields), set(attributes))
                for name, codes in fields.items():
                    self.assertTrue(attributes[name]["name"].strip())
                    if codes is not None:
                        self.assertEqual(codes, set(attributes[name]["state"]))
                        self.assertTrue(all(label.strip() for label in attributes[name]["state"].values()))
                prefix = "monitor" if kind == "preview_status" else "call"
                end_labels = attributes[f"{prefix}_end_reason"]["state"]
                self.assertNotEqual(end_labels["confirmation_timeout"], end_labels["session_timeout"])

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
