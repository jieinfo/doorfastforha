import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReleaseMetadataTests(unittest.TestCase):
    def test_hacs_release_metadata_and_docs_match(self):
        manifest = json.loads((ROOT / "custom_components/doorfast/manifest.json").read_text())
        hacs = json.loads((ROOT / "hacs.json").read_text())
        readme = (ROOT / "README.md").read_text()
        workflow = (ROOT / ".github/workflows/release.yml").read_text()

        self.assertEqual(manifest["domain"], "doorfast")
        self.assertEqual(manifest["version"], "0.3.7")
        self.assertEqual(hacs["name"], "Doorfast")
        self.assertIn("jieinfo/doorfastforha", readme)
        self.assertIn("category=integration", readme)
        self.assertIn("workflow_dispatch", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("manifest.json", workflow)


if __name__ == "__main__":
    unittest.main()
