import json
import tempfile
import unittest
from pathlib import Path

from bambu_companion.gui_logic import (
    DEFAULT_CURRENT_SETTINGS,
    GOAL_LABELS,
    MATERIALS,
    AnalysisError,
    build_export_settings,
    build_export_text,
    build_full_export_profile,
    change_rows,
    current_settings_from_full_profile,
    export_files,
    load_full_profile,
    run_analysis,
    summary_lines,
)
from bambu_companion.schemas import PrintGoal

from .mesh_fixtures import cube_soup, write_stl_binary


class TestRunAnalysis(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stl_path = Path(self.tmp.name) / "cube.stl"
        write_stl_binary(cube_soup(20.0), self.stl_path)

    def test_run_analysis_on_real_file(self):
        run = run_analysis(
            str(self.stl_path),
            goal=PrintGoal.BALANCED,
            material="PLA",
            nozzle_diameter_mm=0.4,
            current_settings=DEFAULT_CURRENT_SETTINGS,
        )
        self.assertEqual(run.model_path, str(self.stl_path))
        self.assertTrue(run.analysis.bed_fit.fits)
        self.assertEqual(run.context.material, "PLA")
        self.assertEqual(run.context.goal, PrintGoal.BALANCED)

    def test_missing_file_raises_analysis_error(self):
        missing = Path(self.tmp.name) / "does_not_exist.stl"
        with self.assertRaises(AnalysisError) as ctx:
            run_analysis(
                str(missing),
                goal=PrintGoal.BALANCED,
                material="PLA",
                nozzle_diameter_mm=0.4,
                current_settings=DEFAULT_CURRENT_SETTINGS,
            )
        self.assertIn("not found", str(ctx.exception))

    def test_folder_path_raises_helpful_analysis_error(self):
        with self.assertRaises(AnalysisError) as ctx:
            run_analysis(
                str(self.tmp.name),
                goal=PrintGoal.BALANCED,
                material="PLA",
                nozzle_diameter_mm=0.4,
                current_settings=DEFAULT_CURRENT_SETTINGS,
            )
        self.assertIn("folder, not a file", str(ctx.exception))

    def test_unsupported_extension_raises_analysis_error(self):
        bad_path = Path(self.tmp.name) / "model.obj"
        bad_path.write_text("not a real mesh")
        with self.assertRaises(AnalysisError):
            run_analysis(
                str(bad_path),
                goal=PrintGoal.BALANCED,
                material="PLA",
                nozzle_diameter_mm=0.4,
                current_settings=DEFAULT_CURRENT_SETTINGS,
            )


class TestGuiHelpers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stl_path = Path(self.tmp.name) / "cube.stl"
        write_stl_binary(cube_soup(20.0), self.stl_path)
        self.run = run_analysis(
            str(self.stl_path),
            goal=PrintGoal.DIMENSIONAL_ACCURACY,
            material="PETG",
            nozzle_diameter_mm=0.4,
            current_settings=DEFAULT_CURRENT_SETTINGS,
        )

    def test_change_rows_shape_and_default_approval(self):
        rows = change_rows(self.run)
        self.assertEqual(len(rows), len(self.run.optimization.changes))
        for row in rows:
            self.assertEqual(
                set(row.keys()), {"key", "label", "current", "recommended", "reason", "approved"}
            )
            self.assertTrue(row["approved"])

    def test_summary_lines_nonempty_and_notes_fit(self):
        lines = summary_lines(self.run)
        self.assertGreaterEqual(len(lines), 1)
        joined = " ".join(lines)
        self.assertTrue("fit" in joined.lower() or "warning" in joined.lower() or "⚠" in joined)

    def test_build_export_settings_only_touches_approved_keys(self):
        all_keys = {c.key for c in self.run.optimization.changes}
        if not all_keys:
            self.skipTest("optimizer produced no changes for this fixture/goal")
        approved = {next(iter(all_keys))}
        merged = build_export_settings(self.run, approved)
        base = self.run.context.current_settings
        for key, value in merged.items():
            if key in approved:
                continue
            self.assertEqual(value, base.get(key), f"unapproved key {key} should be unchanged")

    def test_build_export_text_mentions_model_name(self):
        text = build_export_text(self.run, set())
        self.assertIn(self.stl_path.name, text)
        self.assertIn("does not write into Bambu Studio automatically", text)

    def test_export_files_writes_txt_and_json(self):
        approved = {c.key for c in self.run.optimization.changes}
        out_dir = Path(self.tmp.name) / "out"
        paths = export_files(self.run, approved, out_dir)
        self.assertEqual(len(paths), 2)
        txt_path, json_path = paths
        self.assertTrue(txt_path.exists())
        self.assertTrue(json_path.exists())
        self.assertIn("cube", txt_path.name)
        self.assertIn("cube", json_path.name)

    def test_export_files_with_full_profile_writes_third_file(self):
        approved = {c.key for c in self.run.optimization.changes}
        out_dir = Path(self.tmp.name) / "out_full"
        full_profile = {
            "name": "My PLA Profile",
            "from": "system",
            "version": "02.08.03.66",
            **DEFAULT_CURRENT_SETTINGS,
        }
        paths = export_files(self.run, approved, out_dir, full_profile=full_profile)
        self.assertEqual(len(paths), 3)
        txt_path, json_path, full_path = paths
        self.assertTrue(full_path.exists())
        self.assertIn("full_profile", full_path.name)
        written = json.loads(full_path.read_text(encoding="utf-8"))
        # Metadata Bambu Studio wrote (and that this project doesn't
        # otherwise track) survives untouched.
        self.assertEqual(written["name"], "My PLA Profile")
        self.assertEqual(written["from"], "system")


class TestLoadFullProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_missing_file_raises_analysis_error(self):
        missing = Path(self.tmp.name) / "does_not_exist.json"
        with self.assertRaises(AnalysisError) as ctx:
            load_full_profile(missing)
        self.assertIn("not found", str(ctx.exception))

    def test_invalid_json_raises_analysis_error(self):
        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text("{not valid json", encoding="utf-8")
        with self.assertRaises(AnalysisError) as ctx:
            load_full_profile(bad)
        self.assertIn("valid JSON", str(ctx.exception))

    def test_non_dict_json_raises_analysis_error(self):
        arr = Path(self.tmp.name) / "array.json"
        arr.write_text("[1, 2, 3]", encoding="utf-8")
        with self.assertRaises(AnalysisError) as ctx:
            load_full_profile(arr)
        self.assertIn("JSON object", str(ctx.exception))

    def test_real_profile_loaded_unmodified(self):
        path = Path(self.tmp.name) / "profile.json"
        payload = {
            "name": "0.20mm Standard @BBL A1",
            "inherits": "0.20mm Standard",
            "instantiation": "true",
            "wall_loops": "3",
            "some_key_this_project_never_heard_of": "42",
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        loaded = load_full_profile(path)
        self.assertEqual(loaded, payload)


class TestCurrentSettingsFromFullProfile(unittest.TestCase):
    def test_pulls_known_keys_and_fills_defaults_for_missing_ones(self):
        full_profile = {
            "wall_loops": "3",
            "layer_height": "0.16",
            "name": "Custom Profile",
            "some_unrelated_key": "value",
        }
        pulled = current_settings_from_full_profile(full_profile)
        self.assertEqual(set(pulled.keys()), set(DEFAULT_CURRENT_SETTINGS.keys()))
        self.assertEqual(pulled["wall_loops"], "3")
        self.assertEqual(pulled["layer_height"], "0.16")
        # Not present in the loaded profile -> falls back to this
        # project's own default rather than erroring or dropping the key.
        self.assertEqual(
            pulled["sparse_infill_density"], DEFAULT_CURRENT_SETTINGS["sparse_infill_density"]
        )

    def test_empty_profile_falls_back_to_all_defaults(self):
        pulled = current_settings_from_full_profile({})
        self.assertEqual(pulled, DEFAULT_CURRENT_SETTINGS)


class TestBuildFullExportProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stl_path = Path(self.tmp.name) / "cube.stl"
        write_stl_binary(cube_soup(20.0), self.stl_path)
        self.run = run_analysis(
            str(self.stl_path),
            goal=PrintGoal.DIMENSIONAL_ACCURACY,
            material="PETG",
            nozzle_diameter_mm=0.4,
            current_settings=DEFAULT_CURRENT_SETTINGS,
        )

    def test_full_profile_keys_outside_the_tracked_set_survive_untouched(self):
        all_keys = {c.key for c in self.run.optimization.changes}
        if not all_keys:
            self.skipTest("optimizer produced no changes for this fixture/goal")
        approved = {next(iter(all_keys))}
        full_profile = {
            "name": "My PETG Profile",
            "inherits": "0.20mm Standard",
            "some_key_this_project_never_heard_of": "keep-me",
            **DEFAULT_CURRENT_SETTINGS,
        }
        merged = build_full_export_profile(self.run, approved, full_profile)
        # Untracked metadata keys from the real profile pass through.
        self.assertEqual(merged["name"], "My PETG Profile")
        self.assertEqual(merged["some_key_this_project_never_heard_of"], "keep-me")
        # Only the approved key actually changed.
        for key, value in merged.items():
            if key in approved or key not in DEFAULT_CURRENT_SETTINGS:
                continue
            self.assertEqual(value, DEFAULT_CURRENT_SETTINGS.get(key))

    def test_real_bambu_profile_gets_values_in_bambu_format(self):
        # A profile exported from Bambu Studio stores strings, "15%",
        # and one-element lists; the export must stay in that format.
        full_profile = {
            "name": "0.20mm Standard @BBL A1",
            "wall_loops": "2",
            "layer_height": "0.2",
            "sparse_infill_density": "15%",
            "outer_wall_speed": ["200"],
            "elefant_foot_compensation": "0",
            "some_key_this_project_never_heard_of": "keep-me",
        }
        approved = {c.key for c in self.run.optimization.changes}
        merged = build_full_export_profile(self.run, approved, full_profile)
        recommended = {c.key: c.recommended_value for c in self.run.optimization.changes}
        self.assertEqual(merged["wall_loops"], str(recommended["wall_loops"]))
        self.assertEqual(merged["sparse_infill_density"], f"{recommended['sparse_infill_density']}%")
        self.assertEqual(merged["outer_wall_speed"], [str(recommended["outer_wall_speed"])])
        self.assertEqual(merged["some_key_this_project_never_heard_of"], "keep-me")
        for value in merged.values():
            self.assertIsInstance(value, (str, list))

    def test_none_full_profile_falls_back_to_context_current_settings(self):
        merged = build_full_export_profile(self.run, set(), None)
        self.assertEqual(set(merged.keys()), set(self.run.context.current_settings.keys()))


class TestConstants(unittest.TestCase):
    def test_goal_labels_cover_every_goal(self):
        self.assertEqual(set(GOAL_LABELS.keys()), set(PrintGoal))

    def test_materials_list(self):
        self.assertEqual(MATERIALS, ["PLA", "PETG"])


if __name__ == "__main__":
    unittest.main()
