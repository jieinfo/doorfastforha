"""Tests for Doorfast call-generation selection."""

import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components" / "doorfast" / "generation.py"
)
SPEC = importlib.util.spec_from_file_location("doorfast_generation", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
resolve_generation = MODULE.resolve_generation


class ResolveGenerationTest(unittest.TestCase):
    def test_reads_current_call_generation(self):
        self.assertEqual(7, resolve_generation({"call": {"generation": 7}}, None))

    def test_explicit_generation_takes_priority(self):
        self.assertEqual(
            8,
            resolve_generation({"call": {"generation": 7}}, 8),
        )

    def test_rejects_missing_or_invalid_generation(self):
        invalid = (
            {},
            {"call": None},
            {"call": {}},
            {"call": {"generation": 0}},
            {"call": {"generation": "7"}},
            {"call": {"generation": True}},
        )
        for status in invalid:
            with self.subTest(status=status):
                with self.assertRaises(ValueError):
                    resolve_generation(status, None)


if __name__ == "__main__":
    unittest.main()
