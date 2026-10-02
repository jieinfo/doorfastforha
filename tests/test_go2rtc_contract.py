from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]


class Go2rtcContractTests(unittest.TestCase):
    def test_api_endpoint_is_configurable_without_a_fixed_provider_url(self):
        constants = (
            ROOT / "custom_components" / "doorfast" / "const.py"
        ).read_text()
        flow = (
            ROOT / "custom_components" / "doorfast" / "config_flow.py"
        ).read_text()
        provider = (
            ROOT / "custom_components" / "doorfast" / "webrtc.py"
        ).read_text()
        setup = (ROOT / "custom_components" / "doorfast" / "__init__.py").read_text()

        self.assertIn('CONF_GO2RTC_API_URL = "go2rtc_api_url"', constants)
        self.assertIn('CONF_GO2RTC_USERNAME = "go2rtc_username"', constants)
        self.assertIn('CONF_GO2RTC_PASSWORD = "go2rtc_password"', constants)
        self.assertIn("DoorfastOptionsFlow", flow)
        self.assertNotIn("self.config_entry = config_entry", flow)
        self.assertIn("go2rtc_credentials_required", flow)
        self.assertIn("add_update_listener", setup)
        self.assertNotIn("ws://127.0.0.1:1984/api/ws", provider)
        self.assertIn('"auth": self._go2rtc_auth', provider)

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
