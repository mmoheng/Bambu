import json
import tempfile
import unittest
from pathlib import Path

from bambu_companion.gui_config import (
    MAX_RECENT_FILES,
    add_recent_file,
    load_config,
    save_config,
)


class TestLoadConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "gui_config.json"

    def test_missing_file_returns_defaults(self):
        cfg = load_config(self.path)
        self.assertEqual(cfg["last_model_path"], "")
        self.assertEqual(cfg["current_settings"], {})
        self.assertEqual(cfg["recent_files"], [])
        self.assertEqual(cfg["full_profile_path"], "")

    def test_corrupt_file_returns_defaults_not_a_crash(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json", encoding="utf-8")
        cfg = load_config(self.path)
        self.assertEqual(cfg["last_model_path"], "")

    def test_non_dict_json_returns_defaults(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        cfg = load_config(self.path)
        self.assertEqual(cfg["recent_files"], [])

    def test_roundtrip_save_then_load(self):
        data = {
            "last_model_path": "C:\\models\\part.stl",
            "goal_label": "Balanced",
            "material": "PLA",
            "nozzle_diameter_mm": "0.4",
            "current_settings": {"wall_loops": 3},
            "recent_files": ["C:\\models\\part.stl"],
            "full_profile_path": "C:\\models\\my_profile.json",
        }
        save_config(data, self.path)
        loaded = load_config(self.path)
        self.assertEqual(loaded, data)

    def test_unknown_keys_are_ignored_on_load(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"last_model_path": "x.stl", "made_up_key": 1}), encoding="utf-8")
        cfg = load_config(self.path)
        self.assertEqual(cfg["last_model_path"], "x.stl")
        self.assertNotIn("made_up_key", cfg)

    def test_malformed_field_types_are_reset_to_defaults(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"current_settings": "not-a-dict", "recent_files": "not-a-list"}), encoding="utf-8"
        )
        cfg = load_config(self.path)
        self.assertEqual(cfg["current_settings"], {})
        self.assertEqual(cfg["recent_files"], [])


class TestSaveConfig(unittest.TestCase):
    def test_creates_parent_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "does" / "not" / "exist" / "gui_config.json"
            save_config({"last_model_path": "a.stl"}, nested)
            self.assertTrue(nested.exists())
            self.assertEqual(json.loads(nested.read_text())["last_model_path"], "a.stl")

    def test_missing_keys_are_filled_with_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gui_config.json"
            save_config({"last_model_path": "a.stl"}, path)
            loaded = load_config(path)
            self.assertEqual(loaded["material"], "")
            self.assertEqual(loaded["recent_files"], [])


class TestAddRecentFile(unittest.TestCase):
    def test_new_file_prepended(self):
        result = add_recent_file(["a.stl", "b.stl"], "c.stl")
        self.assertEqual(result, ["c.stl", "a.stl", "b.stl"])

    def test_existing_file_moved_to_front_not_duplicated(self):
        result = add_recent_file(["a.stl", "b.stl"], "b.stl")
        self.assertEqual(result, ["b.stl", "a.stl"])

    def test_list_capped_at_max_recent_files(self):
        existing = [f"{i}.stl" for i in range(MAX_RECENT_FILES)]
        result = add_recent_file(existing, "new.stl")
        self.assertEqual(len(result), MAX_RECENT_FILES)
        self.assertEqual(result[0], "new.stl")


if __name__ == "__main__":
    unittest.main()
