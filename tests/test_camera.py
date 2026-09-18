"""Tests for the Doorfast streaming camera contract."""

import importlib.util
from enum import IntFlag
from pathlib import Path
import sys
import types
import unittest


class CameraEntityFeature(IntFlag):
    STREAM = 2


class Camera:
    def __init__(self):
        pass

    @property
    def supported_features(self):
        return self._attr_supported_features


camera_module = types.ModuleType("homeassistant.components.camera")
camera_module.Camera = Camera
camera_module.CameraEntityFeature = CameraEntityFeature
sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
sys.modules.setdefault("homeassistant.components", types.ModuleType("homeassistant.components"))
sys.modules["homeassistant.components.camera"] = camera_module

ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / "doorfast"
package = types.ModuleType("custom_components.doorfast")
package.__path__ = [str(COMPONENT)]
sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
sys.modules["custom_components.doorfast"] = package
const_spec = importlib.util.spec_from_file_location(
    "custom_components.doorfast.const", COMPONENT / "const.py"
)
const_module = importlib.util.module_from_spec(const_spec)
sys.modules[const_spec.name] = const_module
assert const_spec.loader is not None
const_spec.loader.exec_module(const_module)
spec = importlib.util.spec_from_file_location(
    "custom_components.doorfast.camera", COMPONENT / "camera.py"
)
camera_integration = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = camera_integration
assert spec.loader is not None
spec.loader.exec_module(camera_integration)
DoorfastCamera = camera_integration.DoorfastCamera


class FakeClient:
    def __init__(self):
        self.status = {
            "media": {
                "installed": True,
                "available": True,
                "state": "idle",
                "generation": 0,
            }
        }

    async def latest_video_frame(self):
        return b"jpeg"


class FakeMonitor:
    snapshot = {
        "state": "publishing",
        "generation": 9,
        "ready": True,
        "viewer_count": 1,
        "status_revision": 4,
    }


class DoorfastCameraTest(unittest.IsolatedAsyncioTestCase):
    async def test_exposes_opaque_source_and_keeps_jpeg_fallback(self):
        camera = DoorfastCamera(FakeClient(), "entry-1", FakeMonitor())

        self.assertEqual(b"jpeg", await camera.async_camera_image())
        self.assertEqual(
            "doorfast://entry-1/preview", await camera.stream_source()
        )
        self.assertTrue(camera.supported_features & CameraEntityFeature.STREAM)
        self.assertEqual(
            {
                "monitor_state": "publishing",
                "monitor_generation": 9,
                "monitor_ready": True,
            },
            camera.extra_state_attributes,
        )

    async def test_hides_source_when_media_module_is_unavailable(self):
        client = FakeClient()
        client.status["media"]["available"] = False
        camera = DoorfastCamera(client, "entry-1", FakeMonitor())

        self.assertIsNone(await camera.stream_source())


if __name__ == "__main__":
    unittest.main()
