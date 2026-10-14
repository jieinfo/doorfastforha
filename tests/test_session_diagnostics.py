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
spec = importlib.util.spec_from_file_location(
    f"{PACKAGE}.station_status", COMPONENT / "station_status.py"
)
assert spec and spec.loader
station_status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(station_status)


def operation(**fields):
    return {"operation_id": 17, "station_id": "gate_main", "stage": "calling",
            "media_generation": 9, "call_generation": 42, "timer": "confirmation",
            "remaining_ms": 900, "error": "none", **fields}


def call_snapshot(**fields):
    return {"runtime_id": "0123456789abcdef",
            "call": {"session": "idle", "station_id": "gate_main", "generation": 0},
            "station_call": operation(), "media": {"sessions": [
                {"station_id": "gate_main", "generation": 9, "purpose": "call",
                 "state": "viewing", "ready": True, "source_live": False,
                 "started_ms": 100, "first_frame_ms": 300}]}, **fields}


def record(station_id="gate_main", generation=9, **fields):
    return {"station_id": station_id, "generation": generation,
            "timer": "first_frame", "remaining_ms": 900,
            "end_reason": "none", "failure_reason": "none", **fields}


def terminal(station_id="gate_main", generation=8, **fields):
    return record(station_id, generation, **{
        "timer": "none", "remaining_ms": 0,
        "end_reason": "local_hangup", "failure_reason": "cancelled", **fields})


class SessionDiagnosticsTest(unittest.TestCase):
    def test_station_call_phases_require_actual_control_owner_and_distinct_media_key(self):
        # purpose-only classification and comparing control to media both break this matrix.
        for stage, native, phase in (
            ("preparing", "idle", "calling"), ("calling", "idle", "calling"),
            ("calling", "preview", "calling"), ("talking", "talking", "in_call"),
            ("talking", "idle", "requesting"), ("ending", "talking", "stopping"),
            ("failed", "idle", "requesting"), ("ended", "idle", "requesting"),
        ):
            with self.subTest(stage=stage, native=native):
                terminal = stage in ("failed", "ended")
                status = call_snapshot(station_call=operation(stage=stage,
                    timer="none" if terminal else "confirmation",
                    remaining_ms=0 if terminal else 900))
                status["call"].update(session=native, generation=42)
                attrs = station_status.preview_attributes(status, "gate_main")
                self.assertEqual(phase, attrs["phase"])
                self.assertFalse(attrs["source_live"])
                self.assertEqual(200, attrs["first_frame_latency_ms"])

    def test_call_media_without_operation_or_matching_talking_is_only_requesting(self):
        for native in ({"session": "idle", "generation": 0},
                       {"session": "talking", "station_id": "gate_side", "generation": 42},
                       {"session": "talking", "station_id": "gate_main", "generation": 0}):
            status = call_snapshot(call=native)
            status.pop("station_call")
            status["media"]["sessions"][0]["source_live"] = True
            self.assertEqual("requesting", station_status.preview_attributes(status, "gate_main")["phase"])

    def test_active_call_identity_conflicts_and_ambiguous_media_do_not_manufacture_talking(self):
        for mutation in (
            lambda s: s["call"].update(station_id="gate_side"),
            lambda s: s["call"].update(generation=9),
            lambda s: s["station_call"].update(media_generation=42),
            lambda s: s["station_call"].update(station_id="gate_side"),
            lambda s: s["station_call"].update(call_generation=0),
            lambda s: s["station_call"].update(media_generation=0),
            lambda s: s["media"]["sessions"].append(deepcopy(s["media"]["sessions"][0])),
            lambda s: s["media"]["sessions"].append(None),
            lambda s: s["media"]["sessions"][0].update(generation=True),
            lambda s: s["call"].update(generation=True),
            lambda s: s.update(runtime_id="old runtime"),
        ):
            status = call_snapshot(call={"session": "talking", "station_id": "gate_main", "generation": 42},
                                   station_call=operation(stage="talking", timer="session"))
            mutation(status)
            self.assertNotEqual("in_call", station_status.preview_attributes(status, "gate_main")["phase"])

    def test_preparation_does_not_require_a_ready_media_row(self):
        for media in ({"sessions": []}, {}, None, {"available": False, "sessions": []}):
            status = call_snapshot(media=media, station_call=operation(stage="preparing",
                media_generation=0, call_generation=0, timer="startup", remaining_ms=30000))
            self.assertEqual("calling", station_status.preview_attributes(status, "gate_main")["phase"])
            self.assertNotEqual("calling", station_status.preview_attributes(status, "gate_side")["phase"])

    def test_incoming_talking_survives_unrelated_terminal_operation_and_old_snapshots(self):
        for terminal_station in (None, "gate_side", "gate_main"):
            status = call_snapshot(call={"session": "talking", "station_id": "gate_main", "generation": 43})
            if terminal_station is None:
                status.pop("station_call")
                status.pop("runtime_id")
            else:
                status["station_call"] = operation(station_id=terminal_station,
                    stage="ended", timer="none", remaining_ms=0)
            self.assertEqual("in_call", station_status.preview_attributes(status, "gate_main")["phase"])

        status = call_snapshot(call={"session": "talking", "station_id": "gate_main", "generation": 43},
            station_call=operation(operation_id=0, station_id="", stage="idle", media_generation=0,
                                   call_generation=0, timer="none", remaining_ms=0))
        self.assertEqual("in_call", station_status.preview_attributes(status, "gate_main")["phase"])
        self.assertIsNone(station_status.preview_attributes(status, "gate_main")["station_call_operation_id"])

    def test_incoming_talking_is_independent_of_video_with_absent_or_terminal_operation(self):
        # A media availability/row gate applied to incoming control would break this.
        for operation_kind in ("absent", "idle", "ended", "failed"):
            for media in (None, {"available": False, "sessions": []}, {}, {"sessions": []},
                          {"sessions": [{"station_id": "gate_main", "state": "failed",
                                         "source_live": False}]}):
                with self.subTest(operation_kind=operation_kind, media=media):
                    status = {"runtime_id": "0123456789abcdef", "call": {
                        "session": "talking", "station_id": "gate_main", "generation": 43}}
                    if media is not None:
                        status["media"] = media
                    if operation_kind == "idle":
                        status["station_call"] = operation(operation_id=0, station_id="", stage="idle",
                            media_generation=0, call_generation=0, timer="none", remaining_ms=0)
                    elif operation_kind != "absent":
                        status["station_call"] = operation(stage=operation_kind, timer="none", remaining_ms=0)
                    attrs = station_status.preview_attributes(status, "gate_main")
                    self.assertEqual("in_call", attrs["phase"])
                    self.assertEqual(False if media and media.get("sessions") else None, attrs["source_live"])
                    self.assertNotEqual("in_call", station_status.preview_attributes(status, "gate_side")["phase"])
                    status["station_call"] = operation(stage="talking", timer="session", call_generation=43)
                    self.assertNotEqual("in_call", station_status.preview_attributes(status, "gate_main")["phase"])
                    status["station_call"]["operation_id"] = True
                    self.assertNotEqual("in_call", station_status.preview_attributes(status, "gate_main")["phase"])

    def test_terminal_operation_preserves_live_preview_and_bounded_historical_attributes(self):
        for stage, error in (("failed", "ack_timeout"), ("ended", "cancelled")):
            status = call_snapshot(station_call=operation(stage=stage, timer="none", remaining_ms=0, error=error,
                token="private token", sdp="private sdp"))
            status["media"]["sessions"][0].update(purpose="preview", source_live=True)
            original = deepcopy(status)
            attrs = station_status.preview_attributes(status, "gate_main")
            self.assertEqual("live", attrs["phase"])
            self.assertEqual((stage, error, 17, 9, 42, "none", 0, "0123456789abcdef"), tuple(
                attrs.get(name) for name in ("station_call_stage", "station_call_error", "station_call_operation_id",
                    "station_call_media_generation", "station_call_call_generation", "station_call_timer",
                    "station_call_remaining_ms", "station_call_runtime_id")))
            self.assertNotIn("private", str(attrs))
            self.assertEqual(original, status)
            self.assertIsNone(station_status.preview_attributes(status, "gate_side").get("station_call_operation_id"))

    def test_station_call_codes_and_uint64_values_are_validated_before_consumption(self):
        for key in ("operation_id", "media_generation", "call_generation", "remaining_ms"):
            for value in (True, False, -1, 2**64, "9", 9.0, None, [], {}):
                with self.subTest(key=key, value=value):
                    status = call_snapshot(call={"session": "talking", "station_id": "gate_main", "generation": 42})
                    status["station_call"][key] = value
                    attrs = station_status.preview_attributes(status, "gate_main")
                    self.assertNotEqual("in_call", attrs["phase"])
                    self.assertIsNone(attrs.get("station_call_operation_id"))
        for key, values in (
            ("stage", (None, True, [], {}, "private stage")),
            ("timer", (None, True, [], {}, "first_frame", "private timer")),
            ("error", (None, True, [], {}, "confirmation_timeout", "private error")),
            ("station_id", (None, True, [], {}, "Gate", "secret token")),
        ):
            for value in values:
                status = call_snapshot()
                status["station_call"][key] = value
                attrs = station_status.preview_attributes(status, "gate_main")
                self.assertIsNone(attrs.get("station_call_operation_id"))
                self.assertNotIn("private", str(attrs))
        for value in (None, True, [], {}, "private operation"):
            status = call_snapshot(station_call=value,
                call={"session": "talking", "station_id": "gate_main", "generation": 42})
            self.assertNotEqual("in_call", station_status.preview_attributes(status, "gate_main")["phase"])
        for changes in ({"operation_id": 0}, {"stage": "idle"}, {"stage": "unknown"}):
            status = call_snapshot(station_call=operation(**changes),
                call={"session": "talking", "station_id": "gate_main", "generation": 42})
            self.assertNotEqual("in_call", station_status.preview_attributes(status, "gate_main")["phase"])

    def test_station_call_whitelists_and_terminal_timer_invariants(self):
        errors = ("none", "busy", "station_unavailable", "ack_timeout", "dispatch_failed",
                  "dispatch_timeout", "media_failed", "preempted", "cancelled", "runtime_reset", "unknown")
        for error in errors:
            status = call_snapshot(station_call=operation(error=error))
            self.assertEqual(error, station_status.preview_attributes(status, "gate_main").get("station_call_error"))
        for timer in ("none", "startup", "confirmation", "session"):
            status = call_snapshot(station_call=operation(timer=timer, remaining_ms=2**64 - 1))
            self.assertEqual(timer, station_status.preview_attributes(status, "gate_main").get("station_call_timer"))
            self.assertEqual(2**64 - 1, station_status.preview_attributes(status, "gate_main").get("station_call_remaining_ms"))
        for terminal in (operation(stage="failed", timer="session", remaining_ms=0),
                         operation(stage="ended", timer="none", remaining_ms=1)):
            attrs = station_status.preview_attributes(call_snapshot(station_call=terminal), "gate_main")
            self.assertIsNone(attrs.get("station_call_operation_id"))

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
