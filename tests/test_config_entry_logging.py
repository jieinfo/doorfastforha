from pathlib import Path
import unittest


SOURCE = (Path(__file__).resolve().parents[1] / "custom_components" /
          "doorfast" / "__init__.py").read_text()


class ConfigEntryLoggingTests(unittest.TestCase):
    def test_successful_setup_logs_copyable_entry_id_and_relay_path(self):
        self.assertIn("_LOGGER.info(", SOURCE)
        self.assertIn("config entry ID=%s", SOURCE)
        self.assertIn("relay endpoint=/api/doorfast/%s", SOURCE)
        self.assertIn("entry.entry_id, entry.entry_id", SOURCE)


if __name__ == "__main__":
    unittest.main()
