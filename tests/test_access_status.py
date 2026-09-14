"""Tests for Doorfast access status exposed by the lock entity."""

import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "doorfast"
    / "access_status.py"
)
SPEC = importlib.util.spec_from_file_location("doorfast_access_status", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
access_attributes = MODULE.access_attributes


class AccessAttributesTest(unittest.TestCase):
    def test_exposes_protocol_result_without_unverified_fields(self):
        status = {
            "access": {
                "configured": True,
                "state": "protocol_completed",
                "generation": 7,
                "raw_status": 1,
                "physical_result_confirmed": False,
                "secret": "must not leak",
            }
        }
        self.assertEqual(
            {
                "configured": True,
                "state": "protocol_completed",
                "generation": 7,
                "raw_status": 1,
                "physical_result_confirmed": False,
            },
            access_attributes(status),
        )

    def test_rejects_missing_or_malformed_access_status(self):
        self.assertEqual({}, access_attributes({}))
        self.assertEqual({}, access_attributes({"access": None}))


if __name__ == "__main__":
    unittest.main()
