import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / "doorfast"

ha_button = types.ModuleType("homeassistant.components.button")
class _ButtonEntity:
    def async_write_ha_state(self): pass
    async def async_remove(self, force_remove=False): pass
ha_button.ButtonEntity = _ButtonEntity
sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
sys.modules.setdefault("homeassistant.components", types.ModuleType("homeassistant.components"))
sys.modules["homeassistant.components.button"] = ha_button
ha_entity = types.ModuleType("homeassistant.helpers.entity")
ha_entity.Entity = object
sys.modules.setdefault("homeassistant.helpers", types.ModuleType("homeassistant.helpers"))
sys.modules["homeassistant.helpers.entity"] = ha_entity
entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
entity_registry.async_get = lambda hass: types.SimpleNamespace()
sys.modules["homeassistant.helpers.entity_registry"] = entity_registry
sys.modules["homeassistant.helpers"].entity_registry = entity_registry
package = types.ModuleType("custom_components.doorfast")
package.__path__ = [str(COMPONENT)]
sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
sys.modules["custom_components.doorfast"] = package

def load(name):
    spec = importlib.util.spec_from_file_location(
        f"custom_components.doorfast.{name}", COMPONENT / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module

load("const")
load("client_types")
station_entity = load("station_entity")
button = load("button")


class Station:
    station_id = "gate_main"
    name = "Main Gate"
    enabled = True


class Monitor:
    call_state = "idle"
    call_error = None

    def __init__(self):
        self.listeners = []

    def add_listener(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)


class Registry:
    station_ids = ("gate_main",)

    def __init__(self, monitor):
        self._monitor = monitor

    def station(self, station_id):
        assert station_id == "gate_main"
        return Station()

    def monitor(self, station_id):
        assert station_id == "gate_main"
        return self._monitor

    def add_listener(self, listener):
        self.listener = listener
        return lambda: None


class Entry:
    entry_id = "entry-1"

    def async_on_unload(self, callback):
        self.unload = callback


class Hass:
    def __init__(self, client, registry):
        self.data = {
            "doorfast": {"entry-1": client},
            "doorfast_stations": {"entry-1": registry},
        }
        self.tasks = []

    def async_create_task(self, coroutine):
        import asyncio

        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task


class StationButtonTest(unittest.IsolatedAsyncioTestCase):
    async def test_call_and_hangup_buttons_are_station_scoped(self):
        client = types.SimpleNamespace(
            call_station=AsyncMock(return_value={"state": "calling"}),
            hangup_station=AsyncMock(return_value={"state": "idle"}),
            online=True,
        )
        monitor = types.SimpleNamespace(
            call_state="idle", call_error=None,
            async_call=AsyncMock(return_value={"state": "calling"}),
            async_hangup=AsyncMock(return_value={"state": "idle"}),
        )
        call_button = button.StationCallButton(client, "entry-1", Station(), monitor)
        hangup_button = button.StationHangupButton(client, "entry-1", Station(), monitor)

        await call_button.async_press()
        await hangup_button.async_press()

        monitor.async_call.assert_awaited_once_with()
        monitor.async_hangup.assert_awaited_once_with()
        self.assertEqual(
            "doorfast_entry-1_station_gate_main_call", call_button.unique_id
        )
        self.assertEqual(
            "doorfast_entry-1_station_gate_main_hangup", hangup_button.unique_id
        )

    async def test_monitor_listener_updates_both_station_buttons(self):
        monitor = Monitor()
        client = types.SimpleNamespace(online=True)
        registry = Registry(monitor)
        hass = Hass(client, registry)
        entry = Entry()
        added = []

        await button.async_setup_entry(hass, entry, added.extend)
        self.assertEqual(1, len(monitor.listeners))
        call_button, hangup_button = added[-2:]
        call_button.async_write_ha_state = AsyncMock()
        hangup_button.async_write_ha_state = AsyncMock()

        monitor.listeners[0](monitor)
        await asyncio.gather(*hass.tasks)

        call_button.async_write_ha_state.assert_awaited_once_with()
        hangup_button.async_write_ha_state.assert_awaited_once_with()
