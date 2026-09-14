"""Tests for command payloads emitted by the Doorfast client."""

import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock


ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / "doorfast"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


aiohttp = types.ModuleType("aiohttp")
aiohttp.ClientTimeout = object
sys.modules.setdefault("aiohttp", aiohttp)
sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
helpers = sys.modules.setdefault(
    "homeassistant.helpers", types.ModuleType("homeassistant.helpers")
)
aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
aiohttp_client.async_get_clientsession = lambda hass: None
helpers.aiohttp_client = aiohttp_client
sys.modules.setdefault("homeassistant.helpers.aiohttp_client", aiohttp_client)

package = types.ModuleType("custom_components.doorfast")
package.__path__ = [str(COMPONENT)]
sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
sys.modules["custom_components.doorfast"] = package
load_module("custom_components.doorfast.const", COMPONENT / "const.py")
load_module("custom_components.doorfast.generation", COMPONENT / "generation.py")
client_module = load_module("custom_components.doorfast.client", COMPONENT / "client.py")
DoorfastClient = client_module.DoorfastClient


class AnswerPayloadTest(unittest.IsolatedAsyncioTestCase):
    async def test_uses_current_generation_and_verified_media_defaults(self):
        client = DoorfastClient.__new__(DoorfastClient)
        client.status = {"call": {"generation": 7}}
        client._request = AsyncMock(return_value={"queued": True})

        result = await client.answer()

        self.assertEqual({"queued": True}, result)
        client._request.assert_awaited_once_with(
            "POST",
            "/api/v1/answer",
            {
                "generation": 7,
                "primary_media_port": 8303,
                "secondary_media_port": 8302,
                "duration_seconds": 120,
            },
        )


if __name__ == "__main__":
    unittest.main()
