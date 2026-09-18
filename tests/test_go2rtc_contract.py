from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]


class Go2rtcContractTests(unittest.TestCase):
    def test_documented_ingress_and_local_api_contract(self):
        guide = (ROOT / "docs" / "go2rtc-webrtc-acceptance.md").read_text()
        self.assertIn("doorfast_preview:", guide)
        self.assertIn('listen: ":8554"', guide)
        self.assertIn("1984", guide)
        self.assertIn("Doorfast must not access", guide)

    def test_documented_acceptance_preserves_audio_boundary(self):
        guide = (ROOT / "docs" / "go2rtc-webrtc-acceptance.md").read_text()
        self.assertIn("does not validate two-way audio", guide)
        self.assertIn("exactly one producer", guide)
        self.assertIn("incoming call preemption", guide)


if __name__ == "__main__":
    unittest.main()
