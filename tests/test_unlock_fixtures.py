"""Consume actual C serializer golden output without a C checkout/compiler."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from test_access_status import MODULE

GOLDEN = Path(__file__).parent / "fixtures/station_unlock_v1.json"
TIMINGS = ("first_request_ms", "confirmed_ms", "first_media_ms", "sent_ms",
           "reply_ms", "preparation_ms", "reply_latency_ms")


class UnlockGoldenTest(unittest.TestCase):
    def setUp(self):
        self.fixtures = json.loads(GOLDEN.read_text())

    def test_eight_producer_lifecycle_shapes(self):
        expected = ("waiting_media", "waiting_reply", "protocol_completed",
                    "protocol_rejected", "preparation_timeout", "expired", "cancelled", "idle")
        self.assertEqual(8, len(self.fixtures))
        for fixture, phase in zip(self.fixtures, expected):
            with self.subTest(case=fixture["case"]):
                attrs = MODULE.station_access_attributes(fixture["status"], "gate_main")
                self.assertEqual(phase, attrs["phase"])
                self.assertTrue(fixture["accepted"]["submitted"])
                self.assertEqual("waiting_media", fixture["accepted"]["phase"])
                self.assertFalse(attrs["physical_result_confirmed"])
                if phase == "idle":
                    self.assertNotIn("request_id", attrs)
                    continue
                row = fixture["status"]["access"]["requests"][0]
                self.assertEqual(row, fixture["status"]["access"]["request"])
                for key in TIMINGS:
                    self.assertEqual(row.get(key), attrs[key], key)
                self.assertEqual(row["request_id"], attrs["request_id"])
                other = MODULE.station_access_attributes(fixture["status"], "gate_side")
                self.assertNotIn("request_id", other)

    def test_bounded_malformed_or_old_runtime_records_never_become_success(self):
        original = self.fixtures[2]["status"]
        cases = []
        for field, value in (("request_id", "0" * 16), ("runtime_id", "f" * 16),
                             ("phase", "door_open"), ("station_id", "../gate_main")):
            status = deepcopy(original)
            status["access"]["requests"][0][field] = value
            cases.append(status)
        for version in (True, 2):
            status = deepcopy(original)
            status["access"]["version"] = version
            cases.append(status)
        status = deepcopy(original)
        status["runtime_id"] = self.fixtures[-1]["status"]["runtime_id"]
        cases.append(status)
        for status in cases:
            with self.subTest(status=status):
                attrs = MODULE.station_access_attributes(status, "gate_main")
                self.assertEqual("unknown", attrs["phase"])
                self.assertNotIn("request_id", attrs)

    def test_fixture_and_parser_outputs_contain_no_credential_material(self):
        output = GOLDEN.read_text().lower()
        parsed = repr([MODULE.station_access_attributes(f["status"], "gate_main")
                       for f in self.fixtures]).lower()
        for forbidden in ("0011223344556677", "8899aabbccddeeff", "access_material",
                          "password", "secret", "fingerprint", "logical_address"):
            self.assertNotIn(forbidden, output)
            self.assertNotIn(forbidden, parsed)
