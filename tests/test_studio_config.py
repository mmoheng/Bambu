import tempfile
import unittest
from pathlib import Path

from bambu_companion.bridge.studio_config import load_studio_config, update_studio_config


class TestStudioConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "nested" / "studio_config.json"

    def test_missing_file_gives_defaults(self):
        cfg = load_studio_config(self.path)
        self.assertEqual(cfg["studio_executable"], "")
        self.assertEqual(cfg["working_strategy"], "")
        self.assertEqual(cfg["default_machine"], "Bambu Lab A1 0.4 nozzle")

    def test_update_merges_and_persists(self):
        update_studio_config(self.path, studio_executable=r"G:\Bambu Studio\bambu-studio.exe")
        update_studio_config(self.path, working_strategy="presets+bed")
        cfg = load_studio_config(self.path)
        self.assertEqual(cfg["studio_executable"], r"G:\Bambu Studio\bambu-studio.exe")
        self.assertEqual(cfg["working_strategy"], "presets+bed")
        self.assertEqual(cfg["default_filament"], "Bambu PLA Basic @BBL A1")  # untouched default

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(KeyError):
            update_studio_config(self.path, access_code="nope")
        self.assertFalse(self.path.exists())

    def test_corrupt_or_wrongly_typed_file_falls_back_to_defaults(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json")
        self.assertEqual(load_studio_config(self.path)["default_process"], "0.20mm Standard @BBL A1")
        self.path.write_text('{"studio_executable": 42, "unknown": "x", "working_strategy": "project"}')
        cfg = load_studio_config(self.path)
        self.assertEqual(cfg["studio_executable"], "")  # a non-string is ignored
        self.assertEqual(cfg["working_strategy"], "project")
        self.assertNotIn("unknown", cfg)


if __name__ == "__main__":
    unittest.main()
