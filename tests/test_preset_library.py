import json
import tempfile
import unittest
from pathlib import Path

from bambu_companion.profiles.preset_library import PresetError, PresetLibrary, discover_roots

from .project_fixtures import write_preset_tree


class LibraryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = write_preset_tree(Path(self.tmp.name) / "BBL")
        self.lib = PresetLibrary([self.root])


class TestResolution(LibraryCase):
    def test_nearest_definition_wins_through_the_whole_chain(self):
        full = self.lib.load_full("process", "0.20mm Standard @BBL A1")
        self.assertEqual(full["outer_wall_speed"], ["200"])  # leaf overrides common's 120
        self.assertEqual(full["top_shell_layers"], "5")  # from the middle preset
        self.assertEqual(full["wall_loops"], "2")  # from the common base
        self.assertEqual(full["sparse_infill_density"], "15%")

    def test_result_is_a_standalone_full_config(self):
        full = self.lib.load_full("process", "0.20mm Standard @BBL A1")
        self.assertEqual(full["name"], "0.20mm Standard @BBL A1")
        self.assertEqual(full["type"], "process")
        self.assertEqual(full["inherits"], "")
        self.assertNotIn("instantiation", full)
        self.assertEqual(full["setting_id"], "GP004")  # the leaf's own id, not a parent's

    def test_machine_and_filament_chains(self):
        machine = self.lib.load_full("machine", "Bambu Lab A1 0.4 nozzle")
        self.assertEqual(machine["printable_height"], "256")
        self.assertEqual(machine["nozzle_diameter"], ["0.4"])
        self.assertEqual(machine["printable_area"][2], "256x256")
        filament = self.lib.load_full("filament", "Bambu PLA Basic @BBL A1")
        self.assertEqual(filament["nozzle_temperature"], ["220"])
        self.assertEqual(filament["filament_id"], "GFA00")

    def test_ready_made_full_file_is_used_as_is(self):
        ready = self.root / "process_full" / "0.20mm Standard @BBL A1.json"
        ready.parent.mkdir()
        ready.write_text(json.dumps({"name": "0.20mm Standard @BBL A1", "marker": "shipped"}))
        self.assertEqual(self.lib.load_full("process", "0.20mm Standard @BBL A1")["marker"], "shipped")

    def test_include_templates_are_merged(self):
        (self.root / "filament" / "template_x.json").write_text(
            json.dumps({"name": "template_x", "instantiation": "false", "filament_flow_ratio": ["0.98"]})
        )
        (self.root / "filament" / "With Include.json").write_text(
            json.dumps({"name": "With Include", "inherits": "Bambu PLA Basic @base", "include": ["template_x"]})
        )
        full = PresetLibrary([self.root]).load_full("filament", "With Include")
        self.assertEqual(full["filament_flow_ratio"], ["0.98"])
        self.assertEqual(full["nozzle_temperature"], ["220"])
        self.assertNotIn("include", full)

    def test_missing_preset_and_missing_parent_raise(self):
        with self.assertRaises(PresetError):
            self.lib.load_full("process", "No Such Preset")
        (self.root / "process" / "Orphan.json").write_text(json.dumps({"name": "Orphan", "inherits": "gone"}))
        with self.assertRaises(PresetError):
            PresetLibrary([self.root]).load_full("process", "Orphan")

    def test_inheritance_loop_is_detected(self):
        (self.root / "process" / "A.json").write_text(json.dumps({"name": "A", "inherits": "B"}))
        (self.root / "process" / "B.json").write_text(json.dumps({"name": "B", "inherits": "A"}))
        with self.assertRaises(PresetError) as ctx:
            PresetLibrary([self.root]).load_full("process", "A")
        self.assertIn("loop", str(ctx.exception))

    def test_unknown_type_raises(self):
        with self.assertRaises(PresetError):
            self.lib.load_full("gcode", "x")


class TestLookup(LibraryCase):
    def test_list_names_hides_internal_base_presets(self):
        self.assertEqual(self.lib.list_names("process"), ["0.20mm Standard @BBL A1"])
        self.assertEqual(self.lib.list_names("filament"), ["Bambu PLA Basic @BBL A1"])
        self.assertEqual(self.lib.list_names("machine", contains="a1"), ["Bambu Lab A1 0.4 nozzle"])
        self.assertEqual(self.lib.list_names("machine", contains="x1"), [])

    def test_finds_a_preset_whose_file_name_differs_from_its_name(self):
        (self.root / "process" / "renamed_file.json").write_text(
            json.dumps({"name": "My Tuned Profile", "inherits": "0.20mm Standard @BBL A1", "wall_loops": "4"})
        )
        full = PresetLibrary([self.root]).load_full("process", "My Tuned Profile")
        self.assertEqual(full["wall_loops"], "4")
        self.assertEqual(full["top_shell_layers"], "5")

    def test_user_root_overrides_system_root(self):
        user = Path(self.tmp.name) / "user" / "1234"
        (user / "process").mkdir(parents=True)
        (user / "process" / "0.20mm Standard @BBL A1.json").write_text(
            json.dumps({"name": "0.20mm Standard @BBL A1", "inherits": "fdm_process_single_0.20", "wall_loops": "6"})
        )
        full = PresetLibrary([user, self.root]).load_full("process", "0.20mm Standard @BBL A1")
        self.assertEqual(full["wall_loops"], "6")
        self.assertEqual(full["top_shell_layers"], "5")

    def test_describe_counts(self):
        info = self.lib.describe()
        self.assertEqual(info["counts"], {"machine": 2, "process": 3, "filament": 3})
        self.assertEqual(info["roots"], [str(self.root)])


class TestDiscoverRoots(unittest.TestCase):
    def test_finds_install_system_and_user_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            install = tmp / "Bambu Studio"
            write_preset_tree(install / "resources" / "profiles" / "BBL")
            exe = install / "bambu-studio.exe"
            exe.write_text("")
            appdata = tmp / "AppData"
            write_preset_tree(appdata / "BambuStudio" / "system" / "BBL")
            (appdata / "BambuStudio" / "user" / "1234" / "process").mkdir(parents=True)

            roots = discover_roots(exe, appdata)
            self.assertEqual(
                roots,
                [
                    appdata / "BambuStudio" / "user" / "1234",
                    appdata / "BambuStudio" / "system" / "BBL",
                    install / "resources" / "profiles" / "BBL",
                ],
            )

    def test_nothing_found_is_an_empty_list_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(discover_roots(Path(tmp) / "nope.exe", Path(tmp)), [])



class TestNamesAreNotPaths(LibraryCase):
    """A preset name comes from a tool argument or from inside a project
    file. It must never be usable to read an arbitrary JSON file."""

    def setUp(self):
        super().setUp()
        # A file that is NOT a preset, somewhere else on disk — in real
        # life: printer_config.json with the access code in it.
        self.secret = Path(self.tmp.name) / "elsewhere" / "printer_config.json"
        self.secret.parent.mkdir()
        self.secret.write_text(json.dumps({"access_code": "SECRET", "machine_start_gcode": "M104 S300"}))
        (self.root / "machine_full").mkdir()

    def test_absolute_path_is_rejected(self):
        name = str(self.secret.with_suffix(""))
        for preset_type in ("machine", "process", "filament"):
            with self.assertRaises(PresetError) as ctx:
                self.lib.load_full(preset_type, name)
            self.assertIn("not a preset name", str(ctx.exception))

    def test_relative_traversal_is_rejected(self):
        for name in ("../../elsewhere/printer_config", "..\\..\\elsewhere\\printer_config", "..", ".", "C:x"):
            with self.assertRaises(PresetError, msg=name):
                self.lib.load_full("machine", name)
            with self.assertRaises(PresetError, msg=name):
                self.lib.find("machine", name)

    def test_non_string_and_empty_names_are_rejected(self):
        for name in (None, "", "   ", 42, ["x"]):
            with self.assertRaises(PresetError, msg=repr(name)):
                self.lib.load_full("process", name)

    def test_real_names_with_dots_spaces_and_at_signs_still_work(self):
        self.assertEqual(self.lib.load_full("process", "0.20mm Standard @BBL A1")["name"], "0.20mm Standard @BBL A1")


if __name__ == "__main__":
    unittest.main()
