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

    def test_exposes_safe_station_and_manual_progress_metadata(self):
        self.assertEqual(
            {
                "station_id": "gate_main",
                "manual_state": "waiting_media",
                "manual_station_id": "gate_side",
            },
            access_attributes({"access": {
                "station_id": "gate_main",
                "manual_state": "waiting_media",
                "manual_station_id": "gate_side",
                "material": "must not leak",
            }}),
        )

    def test_rejects_malformed_metadata_instead_of_exposing_arbitrary_values(self):
        self.assertEqual({}, access_attributes({"access": {
            "station_id": {"secret": "must not leak"},
            "manual_state": "unexpected text",
            "manual_station_id": "../not-a-station",
            "state": "secret text",
            "generation": True,
            "raw_status": -1,
            "configured": "true",
            "physical_result_confirmed": "true",
        }}))


if __name__ == "__main__":
    unittest.main()
