import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / "doorfast"


class FakeHttp:
    def __init__(self):
        self.calls = []

    async def async_register_static_paths(self, paths):
        self.calls.append(paths)


class FakeHass:
    def __init__(self):
        self.data = {}
        self.http = FakeHttp()


class FrontendRegistrationTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        http = types.ModuleType("homeassistant.components.http")
        http.StaticPathConfig = lambda url, path, cache: (url, path, cache)
        frontend = types.ModuleType("homeassistant.components.frontend")
        frontend.add_extra_js_url = lambda hass, url: hass.data.setdefault("extra_modules", set()).add(url)
        frontend.remove_extra_js_url = lambda hass, url: hass.data.setdefault("extra_modules", set()).discard(url)
        sys.modules["homeassistant"] = types.ModuleType("homeassistant")
        sys.modules["homeassistant.components"] = types.ModuleType("homeassistant.components")
        sys.modules["homeassistant.components.http"] = http
        sys.modules["homeassistant.components.frontend"] = frontend
        package = types.ModuleType("custom_components.doorfast")
        package.__path__ = [str(COMPONENT)]
        sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
        sys.modules["custom_components.doorfast"] = package
        const_spec = importlib.util.spec_from_file_location("custom_components.doorfast.const", COMPONENT / "const.py")
        const_module = importlib.util.module_from_spec(const_spec)
        sys.modules["custom_components.doorfast.const"] = const_module
        const_spec.loader.exec_module(const_module)
        spec = importlib.util.spec_from_file_location("custom_components.doorfast.frontend", COMPONENT / "frontend.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    async def test_registers_static_assets_and_module_once(self):
        hass = FakeHass()
        await asyncio.gather(
            self.module.async_register_frontend(hass, "one"),
            self.module.async_register_frontend(hass, "one"),
        )
        self.assertEqual(1, len(hass.http.calls))
        [static_path] = hass.http.calls[0]
        self.assertEqual("/doorfast_static", static_path[0])
        self.assertTrue(static_path[1].endswith("custom_components/doorfast/frontend"))
        self.assertTrue(static_path[2])
        module_url = "/doorfast_static/doorfast-ptt-card.mjs?v=0.1.0"
        self.assertEqual({module_url}, hass.data["extra_modules"])

        await self.module.async_register_frontend(hass, "two")
        await self.module.async_unregister_frontend(hass, "one")
        self.assertEqual({module_url}, hass.data["extra_modules"])
        await self.module.async_unregister_frontend(hass, "two")
        self.assertEqual(set(), hass.data["extra_modules"])

        # The HTTP route remains registered and a reload only restores the module URL.
        await self.module.async_register_frontend(hass, "one")
        self.assertEqual(1, len(hass.http.calls))
        self.assertEqual({module_url}, hass.data["extra_modules"])


if __name__ == "__main__":
    unittest.main()
