"""Keep outgoing call labels aligned with the actual call target."""

import json
from pathlib import Path
import unittest


class CallLabelsTest(unittest.TestCase):
    def test_outgoing_call_targets_outdoor_station(self):
        component = Path(__file__).parents[1] / "custom_components" / "doorfast"
        for language, expected in (
            ("en", "Call outdoor station"),
            ("zh-Hans", "呼叫室外机"),
        ):
            with self.subTest(language=language):
                translations = json.loads(
                    (component / "translations" / f"{language}.json").read_text()
                )
                self.assertEqual(expected, translations["entity"]["button"]["call"]["name"])

    def test_buttons_translate_coordinator_call_states_errors_and_timers(self):
        component = Path(__file__).parents[1] / "custom_components" / "doorfast"
        states = {"idle", "preview", "ringing", "preparing", "calling", "talking", "ending", "ended", "failed"}
        errors = {"none", "busy", "station_unavailable", "ack_timeout", "dispatch_failed",
                  "dispatch_timeout", "media_failed", "preempted", "cancelled", "runtime_reset", "unknown"}
        for language in ("en", "zh-Hans"):
            translations = json.loads(
                (component / "translations" / f"{language}.json").read_text()
            )
            for key in ("call", "hangup"):
                with self.subTest(language=language, button=key):
                    attrs = translations["entity"]["button"][key].get("state_attributes", {})
                    self.assertEqual(states, set(attrs.get("call_state", {}).get("state", {})))
                    self.assertTrue(all(isinstance(label, str) and label.strip()
                                        for label in attrs["call_state"]["state"].values()))
                    self.assertEqual(errors, set(attrs.get("call_error", {}).get("state", {})))
                    self.assertEqual({"none", "startup", "confirmation", "session"},
                                     set(attrs.get("call_timer", {}).get("state", {})))
