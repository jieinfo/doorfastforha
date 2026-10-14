"""Pure diagnostic validation never infers timings or borrows session history."""

from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import types
import unittest

COMPONENT = Path(__file__).parents[1] / "custom_components" / "doorfast"
PACKAGE = "doorfast_diagnostics_test"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(COMPONENT)]
sys.modules[PACKAGE] = package
spec = importlib.util.spec_from_file_location(
    f"{PACKAGE}.session_diagnostics", COMPONENT / "session_diagnostics.py"
)
assert spec and spec.loader
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)


def record(station_id="gate_main", generation=9, **fields):
    return {"station_id": station_id, "generation": generation,
            "timer": "first_frame", "remaining_ms": 900,
            "end_reason": "none", "failure_reason": "none", **fields}


def terminal(station_id="gate_main", generation=8, **fields):
    return record(station_id, generation, **{
        "timer": "none", "remaining_ms": 0,
        "end_reason": "local_hangup", "failure_reason": "cancelled", **fields})


class SessionDiagnosticsTest(unittest.TestCase):
    def test_call_diagnostics_are_additive_and_do_not_expose_access_material(self):
        status = {"call": {"timer": "confirmation", "remaining_ms": 1200,
                  "end_reason": "none", "fault": "confirmation_timeout",
                  "station_id": "gate_side", "generation": 9,
                  "last_station_id": "gate_main", "last_generation": 8,
                  "token": "secret", "sdp": "secret"}, "audio": {"ready": True}}
        original = deepcopy(status)
        self.assertEqual({"call_timer": "confirmation", "call_remaining_ms": 1200,
                          "call_end_reason": "none", "call_fault": "confirmation_timeout",
                          "call_last_station_id": "gate_main", "call_last_generation": 8},
                         diagnostics.call_diagnostics(status))
        self.assertEqual(original, status)

    def test_old_or_malformed_status_has_unknown_enums_and_missing_numbers(self):
        for status in ({}, None, [], {"call": None}, {"call": []}):
            with self.subTest(status=status):
                self.assertEqual({"call_timer": "unknown", "call_remaining_ms": None,
                                  "call_end_reason": "unknown", "call_fault": "unknown",
                                  "call_last_station_id": None, "call_last_generation": None},
                                 diagnostics.call_diagnostics(status))
        for status in ({}, None, [], {"media": []}, {"media": {"sessions": []}}):
            self.assertEqual({"monitor_timer": "unknown", "monitor_remaining_ms": None,
                              "monitor_end_reason": "unknown", "monitor_failure_reason": "unknown",
                              "monitor_last_generation": None},
                             diagnostics.station_diagnostics(status, "gate_main"))

    def test_unknown_enum_types_cannot_leak_or_keep_a_countdown(self):
        for value in (None, True, [], {}, 1, 1.5, "private diagnostic", ""):
            with self.subTest(value=value):
                call = diagnostics.call_diagnostics({"call": {
                    "timer": value, "remaining_ms": 40, "end_reason": value, "fault": value}})
                media = diagnostics.station_diagnostics({"media": {"sessions": [
                    record(timer=value, end_reason=value, failure_reason=value)]}}, "gate_main")
                self.assertEqual(("unknown", None, "unknown", "unknown"),
                                 (call["call_timer"], call["call_remaining_ms"],
                                  call["call_end_reason"], call["call_fault"]))
                self.assertEqual(("unknown", None, "unknown", "unknown"),
                                 (media["monitor_timer"], media["monitor_remaining_ms"],
                                  media["monitor_end_reason"], media["monitor_failure_reason"]))

    def test_call_and_media_timer_domains_are_distinct(self):
        for timer, expected_call in (("none", "none"), ("confirmation", "confirmation"),
                                     ("session", "session"), ("first_frame", "unknown"),
                                     ("reconnect", "unknown")):
            call = diagnostics.call_diagnostics({"call": {"timer": timer, "remaining_ms": 0}})
            media = diagnostics.station_diagnostics({"media": {"sessions": [
                record(timer=timer, remaining_ms=0)]}}, "gate_main")
            self.assertEqual(expected_call, call["call_timer"])
            self.assertEqual(timer, media["monitor_timer"])
            self.assertEqual(None if expected_call == "unknown" else 0, call["call_remaining_ms"])

    def test_fault_domains_reject_media_only_call_faults_and_call_only_media_faults(self):
        shared = ("none", "dispatch_failed", "dispatch_timeout", "confirmation_rejected",
                  "confirmation_timeout", "preempted", "cancelled", "runtime_reset", "unknown")
        for fault in shared + ("heartbeat_timeout", "first_frame_timeout", "source_stalled",
                               "encoder_failed", "publisher_failed", "capacity_busy"):
            call = diagnostics.call_diagnostics({"call": {"fault": fault}})
            media = diagnostics.station_diagnostics({"media": {"sessions": [
                record(failure_reason=fault)]}}, "gate_main")
            self.assertEqual(fault if fault in shared + ("heartbeat_timeout",) else "unknown",
                             call["call_fault"])
            self.assertEqual("unknown" if fault == "heartbeat_timeout" else fault,
                             media["monitor_failure_reason"])

    def test_end_domains_and_confirmation_timeout_remain_distinct(self):
        for reason in ("none", "local_hangup", "peer_hangup", "session_timeout",
                       "confirmation_timeout", "preempted", "runtime_reset", "unknown",
                       "heartbeat_timeout"):
            self.assertEqual(reason, diagnostics.call_diagnostics({"call": {
                "end_reason": reason}})["call_end_reason"])
            self.assertEqual("unknown" if reason == "heartbeat_timeout" else reason,
                             diagnostics.station_diagnostics({"media": {"sessions": [
                                 record(end_reason=reason)]}}, "gate_main")["monitor_end_reason"])

    def test_remaining_is_exact_nonnegative_uint64_and_not_computed_from_timestamps(self):
        for value in (True, False, [], {}, 1.0, -1, 2**64, "12", None, 0, 2**64 - 1):
            expected = value if type(value) is int and 0 <= value < 2**64 else None
            call = diagnostics.call_diagnostics({"call": {"timer": "session",
                "remaining_ms": value, "started_ms": 1, "deadline_ms": 999999}})
            media = diagnostics.station_diagnostics({"media": {"sessions": [
                record(remaining_ms=value)]}}, "gate_main")
            self.assertEqual(expected, call["call_remaining_ms"])
            self.assertEqual(expected, media["monitor_remaining_ms"])
        self.assertIsNone(diagnostics.call_diagnostics({"call": {
            "timer": "session", "started_ms": 1, "deadline_ms": 999999}})["call_remaining_ms"])

    def test_current_nine_wins_while_last_generation_remains_eight(self):
        status = {"media": {"sessions": [record()], "last_results": [terminal()]}}
        self.assertEqual({"monitor_timer": "first_frame", "monitor_remaining_ms": 900,
                          "monitor_end_reason": "none", "monitor_failure_reason": "none",
                          "monitor_last_generation": 8},
                         diagnostics.station_diagnostics(status, "gate_main"))
        status["media"]["last_results"] = []
        self.assertIsNone(diagnostics.station_diagnostics(status, "gate_main")["monitor_last_generation"])

    def test_last_results_are_station_scoped_and_restart_history_is_empty(self):
        status = {"media": {"sessions": [], "last_results": [
            terminal("gate_main", 8), terminal("gate_side", 4, end_reason="peer_hangup"),
            terminal("gate_b2", 3, end_reason="preempted", failure_reason="preempted")]}}
        for station_id, generation, reason in (("gate_main", 8, "local_hangup"),
                                              ("gate_side", 4, "peer_hangup"),
                                              ("gate_b2", 3, "preempted")):
            result = diagnostics.station_diagnostics(status, station_id)
            self.assertEqual(("none", 0, reason, generation),
                             (result["monitor_timer"], result["monitor_remaining_ms"],
                              result["monitor_end_reason"], result["monitor_last_generation"]))
        self.assertEqual("unknown", diagnostics.station_diagnostics(status, "foreign")["monitor_end_reason"])
        self.assertIsNone(diagnostics.station_diagnostics({"runtime_id": "new", "media": {
            "sessions": [], "last_results": []}}, "gate_main")["monitor_last_generation"])

    def test_ambiguous_malformed_or_invalid_generation_active_never_borrows_history(self):
        for sessions in ([record(), record()], [None], {},
                         *([record(generation=value)] for value in (True, [], {}, 1.0, -1, 0, 2**64, None))):
            result = diagnostics.station_diagnostics({"media": {
                "sessions": sessions, "last_results": [terminal()]}}, "gate_main")
            self.assertEqual("unknown", result["monitor_end_reason"])
            self.assertIsNone(result["monitor_remaining_ms"])

    def test_ambiguous_or_invalid_terminal_markers_are_not_current_generations(self):
        for results in ([terminal(), terminal()], [None], {},
                        [terminal(timer="session")], [terminal(remaining_ms=1)],
                        *([terminal(generation=value)] for value in (True, [], {}, 1.0, -1, 0, 2**64, None))):
            for sessions in ([], [record()]):
                attrs = diagnostics.station_diagnostics({"media": {
                    "sessions": sessions, "last_results": results}}, "gate_main")
                self.assertIsNone(attrs["monitor_last_generation"])
                if not sessions:
                    self.assertEqual("unknown", attrs["monitor_end_reason"])

    def test_call_terminal_identity_requires_a_valid_pair(self):
        for station_id, generation in (("32:02:01:00:01:00", 8), ("password secret", 8),
                                       ([], 8), ("Gate", 8), ("a" * 33, 8),
                                       ("gate_main", True), ("gate_main", 0),
                                       ("gate_main", 2**64), ("gate_main", None)):
            attrs = diagnostics.call_diagnostics({"call": {
                "last_station_id": station_id, "last_generation": generation}})
            self.assertIsNone(attrs["call_last_station_id"])
            self.assertIsNone(attrs["call_last_generation"])

    def test_call_unknown_terminal_sentinel_is_unavailable_without_mutating_status(self):
        # Removing the call-specific sentinel guard would expose an ambiguous owner.
        self.assertEqual("unknown", diagnostics.require_station_id("unknown"))
        media = diagnostics.station_diagnostics({"media": {
            "sessions": [], "last_results": [terminal("unknown", 8)]}}, "unknown")
        self.assertEqual(8, media["monitor_last_generation"])
        self.assertEqual("local_hangup", media["monitor_end_reason"])
        for station_id, expected_owner, expected_generation in (
            ("gate_main", "gate_main", 8), ("unknown", None, None),
        ):
            with self.subTest(station_id=station_id):
                status = {"call": {"session": "talking", "station_id": "gate_side", "generation": 9,
                    "timer": "confirmation", "remaining_ms": 1200, "end_reason": "none",
                    "fault": "confirmation_timeout", "last_station_id": station_id, "last_generation": 8},
                    "audio": {"ready": True, "generation": 9}}
                original = deepcopy(status)
                self.assertEqual({"call_timer": "confirmation", "call_remaining_ms": 1200,
                    "call_end_reason": "none", "call_fault": "confirmation_timeout",
                    "call_last_station_id": expected_owner, "call_last_generation": expected_generation},
                    diagnostics.call_diagnostics(status))
                self.assertEqual(original, status)


if __name__ == "__main__":
    unittest.main()
