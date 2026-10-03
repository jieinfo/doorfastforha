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
