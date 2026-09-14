"""Tests for Doorfast service routing."""

import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components" / "doorfast" / "routing.py"
)
SPEC = importlib.util.spec_from_file_location("doorfast_routing", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
select_client = MODULE.select_client


class SelectClientTest(unittest.TestCase):
    def test_selects_only_client_when_entry_is_omitted(self):
        client = object()
        self.assertIs(client, select_client({"first": client}, None))

    def test_selects_explicit_client(self):
        first, second = object(), object()
        self.assertIs(second, select_client({"first": first, "second": second}, "second"))

    def test_rejects_ambiguous_missing_and_unknown_entries(self):
        for clients, entry_id in (({}, None), ({"a": 1, "b": 2}, None), ({"a": 1}, "b")):
            with self.subTest(clients=clients, entry_id=entry_id):
                with self.assertRaises(ValueError):
                    select_client(clients, entry_id)


if __name__ == "__main__":
    unittest.main()
