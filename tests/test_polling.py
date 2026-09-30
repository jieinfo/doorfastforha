import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "custom_components" / "doorfast" / "polling.py"
package = types.ModuleType("custom_components.doorfast")
package.__path__ = [str(ROOT / "custom_components" / "doorfast")]
sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
sys.modules["custom_components.doorfast"] = package
spec = importlib.util.spec_from_file_location(
    "custom_components.doorfast.polling", MODULE_PATH
)
polling = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = polling


class PollingTest(unittest.IsolatedAsyncioTestCase):
    async def test_monitor_sync_failure_keeps_bridge_online(self):
        client = types.SimpleNamespace(online=False)
        events = []

        async def refresh():
            client.online = True

        async def station_refresh():
            raise RuntimeError("preview status raced with station refresh")

        async def sync_monitor():
            events.append("sync")

        async def dispatch():
            events.append("dispatch")

        await polling.async_poll(
            client, refresh, station_refresh, sync_monitor, dispatch
        )

        self.assertTrue(client.online)
        self.assertEqual(["sync", "dispatch"], events)

    async def test_bridge_refresh_failure_marks_client_offline(self):
        client = types.SimpleNamespace(online=True)

        async def refresh():
            raise TimeoutError("bridge timeout")

        async def unexpected():
            raise AssertionError("must not run after bridge failure")

        await polling.async_poll(client, refresh, unexpected, unexpected, unexpected)

        self.assertFalse(client.online)


if spec.loader is not None:
    spec.loader.exec_module(polling)
