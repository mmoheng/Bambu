import os
import unittest
from pathlib import Path
from unittest import mock

from bambu_companion import app_dirs
from bambu_companion.bridge import printer_config, studio_config


class TestUserDataRoot(unittest.TestCase):
    def test_appdata_is_used_when_set(self):
        with mock.patch.dict(os.environ, {"APPDATA": "/somewhere/Roaming"}):
            self.assertEqual(app_dirs.user_data_root(), Path("/somewhere/Roaming"))

    def test_windows_without_appdata_still_resolves_to_the_roaming_folder(self):
        # Claude Desktop can start the connector without APPDATA in its
        # environment; it must still find what the setup script saved.
        root = app_dirs.user_data_root(env={}, os_name="nt", home=Path("/home/matt"))
        self.assertEqual(root, Path("/home/matt/AppData/Roaming"))

    def test_other_systems_fall_back_to_home(self):
        root = app_dirs.user_data_root(env={}, os_name="posix", home=Path("/home/matt"))
        self.assertEqual(root, Path("/home/matt"))

    def test_setup_script_and_connector_agree_on_the_printer_config_location(self):
        with mock.patch.dict(os.environ, {"APPDATA": "/somewhere/Roaming"}):
            self.assertEqual(
                printer_config.default_config_path(),
                Path("/somewhere/Roaming/BambuCompanion/printer_config.json"),
            )
            self.assertEqual(printer_config.default_config_path().parent, studio_config.config_dir())


if __name__ == "__main__":
    unittest.main()
