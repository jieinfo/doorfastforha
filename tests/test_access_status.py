"""Tests for Doorfast access status exposed by the lock entity."""

import importlib.util
from pathlib import Path
import unittest
from copy import deepcopy


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


def unlock_status(phase="protocol_completed", station_id="gate_main", request_id="0000000000000002"):
    return {"runtime_id": "0123456789abcdef", "access": {"version": 1,
        "configured": True, "state": "protocol_completed", "station_id": "gate_side",
        "stations": [{"station_id": station_id, "mode": "direct", "configured": True,
                      "credential_source": "station", "reason": "none"}],
        "requests": [{"station_id": station_id, "runtime_id": "0123456789abcdef",
            "request_id": request_id, "phase": phase, "wait_reason": "none",
            "preparation_path": "lightweight", "mode": "direct", "credential_source": "station",
            "reused_session": False, "physical_result_confirmed": False, "attempts": 2,
            "busy_replies": 1, "remaining_ms": 0, "elapsed_ms": 50,
            "first_request_ms": 5, "first_media_ms": 20, "sent_ms": 20, "reply_ms": 50,
            "preparation_ms": 20, "reply_latency_ms": 30, "raw_status": 1,
            "error": "none", "cleanup_error": "none", "secret": "private material"}]}}


class StationUnlockAttributesTest(unittest.TestCase):
    def parse(self, status):
        return MODULE.station_access_attributes(status, "gate_main")

    def test_current_station_record_replaces_global_projection_and_suppresses_secrets(self):
        attrs = self.parse(unlock_status())
        self.assertEqual("protocol_completed", attrs["phase"])
        self.assertEqual("0000000000000002", attrs.get("request_id"))
        self.assertEqual((2, 1, 30), (attrs.get("attempts"), attrs.get("busy_replies"), attrs.get("reply_latency_ms")))
        self.assertIsNone(attrs.get("confirmed_ms"))
        self.assertFalse(attrs["physical_result_confirmed"])
        self.assertNotIn("private", str(attrs))
        self.assertEqual(attrs, MODULE.station_unlock_attributes(unlock_status(), "gate_main"))

    def test_invalid_identity_version_and_duplicate_records_cannot_show_success(self):
        original = unlock_status()
        cases = []
        for request_id in ("", "0" * 16, "f" * 17, "A" * 16, 2, True):
            status = deepcopy(original)
            status["access"]["requests"][0]["request_id"] = request_id
            cases.append(status)
        for version in (True, "1", 2, None):
            status = deepcopy(original)
            status["access"]["version"] = version
            cases.append(status)
        status = deepcopy(original)
        status["access"]["requests"][0]["runtime_id"] = "fedcba9876543210"
        cases.append(status)
        for same_station in (True, False):
            status = deepcopy(original)
            duplicate = deepcopy(status["access"]["requests"][0])
            if not same_station:
                duplicate["station_id"] = "gate_side"
            status["access"]["requests"].append(duplicate)
            cases.append(status)
        status = deepcopy(original)
        status["access"]["request"] = status["access"]["requests"].pop()
        cases.append(status)
        for status in cases:
            with self.subTest(status=status):
                self.assertEqual("unknown", self.parse(status)["phase"])
                self.assertNotIn("request_id", self.parse(status))

    def test_timings_are_nullable_bounded_and_never_extrapolated(self):
        status = unlock_status()
        record = status["access"]["requests"][0]
        record.update(first_request_ms=True, first_media_ms=-1, sent_ms=2**64,
                      reply_ms=51, preparation_ms=None, reply_latency_ms="30", remaining_ms=100)
        attrs = self.parse(status)
        for field in ("first_request_ms", "first_media_ms", "sent_ms", "reply_ms",
                      "preparation_ms", "reply_latency_ms", "remaining_ms"):
            self.assertIsNone(attrs.get(field), field)
        record["remaining_ms"] = 0
        self.assertEqual(0, self.parse(status).get("remaining_ms"))
        record["physical_result_confirmed"] = True
        self.assertFalse(self.parse(status)["physical_result_confirmed"])

    def test_station_metadata_modes_and_unknown_configuration(self):
        for mode, source, configured, reason in (("inherit", "global", True, "none"),
                ("direct", "station", True, "none"), ("disabled", "none", False, "disabled")):
            status = unlock_status()
            status["access"]["requests"] = []
            status["access"]["stations"][0].update(mode=mode, credential_source=source,
                                                     configured=configured, reason=reason)
            attrs = self.parse(status)
            self.assertEqual((mode, source, configured), (attrs.get("mode"), attrs.get("credential_source"), attrs.get("configured")))
            self.assertEqual("disabled" if mode == "disabled" else "idle", attrs["phase"])
        status["access"]["stations"] = []
        self.assertNotIn("configured", self.parse(status))

    def test_inconsistent_relative_timings_and_invalid_counters_are_unknown(self):
        status = unlock_status()
        row = status["access"]["requests"][0]
        row.update(reply_ms=10, reply_latency_ms=30, preparation_ms=19, attempts=True, busy_replies=2**64)
        attrs = self.parse(status)
        for field in ("reply_ms", "reply_latency_ms", "preparation_ms", "attempts", "busy_replies"):
            self.assertIsNone(attrs.get(field), field)
        row.update(phase="waiting_reply", remaining_ms=1001)
        self.assertIsNone(self.parse(status).get("remaining_ms"))
        legacy = {"access": {"station_id": "gate_main", "state": "protocol_completed",
                             "physical_result_confirmed": True}}
        self.assertFalse(self.parse(legacy)["physical_result_confirmed"])

    def test_unknown_enums_and_malformed_station_metadata_are_not_raw_attributes(self):
        status = unlock_status()
        row = status["access"]["requests"][0]
        for field in ("wait_reason", "mode", "credential_source", "preparation_path", "error", "cleanup_error"):
            row[field] = {"secret": "private"}
        attrs = self.parse(status)
        self.assertEqual("unknown", attrs["wait_reason"])
        self.assertNotIn("private", str(attrs))
        status["access"]["stations"][0]["configured"] = "yes"
        self.assertNotIn("configured", self.parse(status))

    def test_station_configuration_without_runtime_is_not_a_usable_claim(self):
        status = unlock_status()
        del status["runtime_id"]
        self.assertNotIn("configured", self.parse(status))


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
