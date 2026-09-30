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
