import tempfile
import unittest
import zipfile
from pathlib import Path

from bambu_companion.bridge.studio_runner import (
    SliceJobSpec,
    build_cli_args,
    verify_settings_applied,
)


class TestBuildCliArgs(unittest.TestCase):
    def test_arg_order_and_content(self):
        spec = SliceJobSpec(
            model_path=Path("model.stl"),
            process_settings_path=Path("process.json"),
            filament_settings_path=Path("filament.json"),
            output_3mf_path=Path("out.3mf"),
        )
        args = build_cli_args(spec)
        self.assertEqual(
            args,
            [
                "model.stl",
                "--load-settings",
                "process.json",
                "--load-filaments",
                "filament.json",
                "--slice",
                "0",
                "--export-3mf",
                "out.3mf",
            ],
        )

    def test_extra_args_are_appended(self):
        spec = SliceJobSpec(
            model_path=Path("m.stl"),
            process_settings_path=Path("p.json"),
            filament_settings_path=Path("f.json"),
            output_3mf_path=Path("o.3mf"),
            extra_args=["--arrange", "1"],
        )
        args = build_cli_args(spec)
        self.assertEqual(args[-2:], ["--arrange", "1"])


class TestVerifySettingsApplied(unittest.TestCase):
    def test_finds_matching_key_value_in_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.3mf"
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("Metadata/Project_Settings.config", '"wall_loops": 4, "layer_height": 0.16')

            ok, unverified = verify_settings_applied(path, {"wall_loops": 4})
            self.assertTrue(ok)
            self.assertEqual(unverified, [])

    def test_flags_missing_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.3mf"
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("Metadata/Project_Settings.config", '"wall_loops": 2')

            ok, unverified = verify_settings_applied(path, {"wall_loops": 4})
            self.assertFalse(ok)
            self.assertEqual(unverified, ["wall_loops"])


if __name__ == "__main__":
    unittest.main()
