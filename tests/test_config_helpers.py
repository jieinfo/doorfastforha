"""Tests for Doorfast configuration helpers."""

import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "doorfast"
    / "config_helpers.py"
)
SPEC = importlib.util.spec_from_file_location("doorfast_config_helpers", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
normalize_bridge_url = MODULE.normalize_bridge_url
is_doorfast_status = MODULE.is_doorfast_status


class NormalizeBridgeUrlTest(unittest.TestCase):
    def test_adds_bridge_path(self):
        self.assertEqual(
            "http://doorfast.local/cgi-bin/doorfast",
            normalize_bridge_url("http://doorfast.local/"),
        )

    def test_preserves_existing_bridge_path(self):
        expected = "http://doorfast.local/cgi-bin/doorfast"
        self.assertEqual(expected, normalize_bridge_url(expected + "/"))


class DoorfastStatusTest(unittest.TestCase):
    def test_requires_boolean_running_flag(self):
        self.assertTrue(is_doorfast_status({"running": True}))
        for payload in ({}, {"running": False}, {"running": 1}, {"running": "true"}):
            with self.subTest(payload=payload):
                self.assertFalse(is_doorfast_status(payload))


if __name__ == "__main__":
    unittest.main()
