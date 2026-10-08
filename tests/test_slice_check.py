import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bambu_companion.bridge import slice_check
from bambu_companion.bridge.studio_config import load_studio_config
from bambu_companion.model_analyzer import analyze
from bambu_companion.profiles.preset_library import PresetLibrary

from .project_fixtures import FakeStudio, write_preset_tree, write_project_3mf


class SliceCheckCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.exe = self.dir / "bambu-studio.exe"
        self.exe.write_text("")
        self.library = PresetLibrary([write_preset_tree(self.dir / "BBL")])
        # Keep config + reports inside the temp folder.
        patcher = mock.patch.dict(os.environ, {"APPDATA": str(self.dir / "AppData")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_check(self, fake, model=None, **kwargs):
        return slice_check.run_slice_check(
            model, executable_path=str(self.exe), runner=fake, library=self.library, **kwargs
        )


class TestTestCube(unittest.TestCase):
    def test_generated_cube_is_a_valid_closed_20mm_solid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = slice_check.write_test_cube(Path(tmp) / "cube.stl")
            result = analyze(path)
            self.assertEqual(tuple(round(v, 3) for v in result.bounding_box.size), (20.0, 20.0, 20.0))
            self.assertTrue(result.is_watertight)
            self.assertEqual(result.face_count, 12)
            self.assertAlmostEqual(result.bed_contact_area_mm2, 400.0)
            self.assertEqual(result.overhangs.overhang_face_count, 0)


class TestRunSliceCheck(SliceCheckCase):
    def test_stl_tries_the_presets_invocations_and_remembers_the_first_that_works(self):
        fake = FakeStudio()
        report = self.run_check(fake)
        names = [a["name"] for a in report["attempts"]]
        self.assertEqual(names, ["presets", "presets+bed", "presets+fullpath"])
        self.assertTrue(all(a["ok"] for a in report["attempts"]))
        self.assertEqual(report["working_strategy"], "presets")
        self.assertIn("RESULT: headless slicing works", report["summary"])

        saved = load_studio_config()
        self.assertEqual(saved["working_strategy"], "presets")
        self.assertEqual(saved["studio_executable"], str(self.exe))
        self.assertTrue(Path(report["report_path"]).exists())
        # A bare STL is arranged onto the plate but keeps its orientation.
        self.assertIn("--arrange", fake.calls[0])
        self.assertNotIn("--orient", fake.calls[0])
        self.assertIn("--curr-bed-type", fake.calls[1])
        self.assertNotIn("--outputdir", fake.calls[2])

    def test_project_file_is_tried_with_its_own_settings_first(self):
        project = write_project_3mf(
            self.dir / "box.3mf",
            extra_settings={
                "print_settings_id": "0.20mm Standard @BBL A1",
                "filament_settings_id": ["Bambu PLA Basic @BBL A1"],
            },
        )
        fake = FakeStudio()
        report = self.run_check(fake, str(project))
        names = [a["name"] for a in report["attempts"]]
        self.assertEqual(
            names, ["project", "project+fullpath", "presets", "presets+bed", "presets+fullpath"]
        )
        self.assertNotIn("--load-settings", fake.calls[0])
        self.assertNotIn("--orient", fake.calls[2])  # a project already has a plate layout
        self.assertEqual(report["presets_used"]["source"], "the project file")
        self.assertEqual(report["working_strategy"], "project")

    def test_project_naming_presets_this_machine_lacks_still_gets_the_project_attempts(self):
        # The fixture project names "0.28mm Extra Draft @BBL A1", which
        # the miniature preset tree doesn't have.
        project = write_project_3mf(self.dir / "box.3mf")
        report = self.run_check(FakeStudio(), str(project))
        self.assertEqual([a["name"] for a in report["attempts"]], ["project", "project+fullpath"])
        self.assertIn("0.28mm Extra Draft @BBL A1", report["preset_error"])
        self.assertEqual(report["working_strategy"], "project")

    def test_total_failure_is_reported_with_evidence_and_saves_no_strategy(self):
        fake = FakeStudio(returncode=206, stderr="plate 1: Nothing to be sliced")
        report = self.run_check(fake)
        self.assertIsNone(report["working_strategy"])
        self.assertTrue(all(not a["ok"] for a in report["attempts"]))
        self.assertEqual(report["attempts"][0]["returncode"], 206)
        self.assertIn("Nothing to be sliced", report["attempts"][0]["output_tail"])
        self.assertIn("no invocation worked", report["summary"])
        self.assertEqual(load_studio_config()["working_strategy"], "")

    def test_only_a_later_variant_working_is_what_gets_remembered(self):
        class OnlyWithFullPath(FakeStudio):
            def __call__(self, executable, args, **kwargs):
                self.returncode = 1 if "--outputdir" in args else 0
                return super().__call__(executable, args, **kwargs)

        report = self.run_check(OnlyWithFullPath())
        self.assertEqual(report["working_strategy"], "presets+fullpath")

    def test_missing_executable_stops_with_a_clear_message(self):
        report = slice_check.run_slice_check(executable_path=str(self.dir / "gone.exe"), save=False)
        self.assertIn("does not exist", report["error"])
        self.assertIn("STOPPED", report["summary"])
        self.assertEqual(report["attempts"], [])

    def test_unresolvable_presets_are_reported_not_raised(self):
        report = slice_check.run_slice_check(
            executable_path=str(self.exe), runner=FakeStudio(), library=PresetLibrary([]), save=False
        )
        self.assertIn("preset_error", report)
        self.assertEqual(report["attempts"], [])
        self.assertIn("PRESETS COULD NOT BE BUILT", report["summary"])

    def test_save_false_writes_nothing(self):
        report = self.run_check(FakeStudio(), save=False)
        self.assertNotIn("report_path", report)
        self.assertEqual(load_studio_config()["working_strategy"], "")

    def test_report_json_on_disk_matches_the_returned_report(self):
        report = self.run_check(FakeStudio())
        on_disk = json.loads(Path(report["report_path"]).with_suffix(".json").read_text())
        self.assertEqual(on_disk["working_strategy"], report["working_strategy"])



class TestCheckKeepsConfigHonest(SliceCheckCase):
    def test_failed_check_clears_an_earlier_confirmed_strategy(self):
        self.run_check(FakeStudio())
        self.assertEqual(load_studio_config()["working_strategy"], "presets")
        self.run_check(FakeStudio(returncode=1))
        self.assertEqual(load_studio_config()["working_strategy"], "")

    def test_executable_is_remembered_even_when_slicing_fails(self):
        self.run_check(FakeStudio(returncode=1))
        self.assertEqual(load_studio_config()["studio_executable"], str(self.exe))

    def test_unreadable_output_is_one_failed_attempt_not_an_aborted_check(self):
        report = self.run_check(FakeStudio(garbage_output=True))
        self.assertEqual(len(report["attempts"]), 3)
        self.assertTrue(all(not a["ok"] for a in report["attempts"]))
        self.assertIn("can't be read", report["attempts"][0]["error"])
        self.assertTrue(Path(report["report_path"]).exists())

    def test_relative_model_path_is_resolved(self):
        write_project_3mf(self.dir / "box.3mf")
        fake = FakeStudio()
        previous = os.getcwd()
        os.chdir(self.dir)
        try:
            report = self.run_check(fake, "box.3mf")
        finally:
            os.chdir(previous)
        self.assertEqual(report["working_strategy"], "project")
        self.assertTrue(Path(fake.calls[0][-1]).is_absolute())


if __name__ == "__main__":
    unittest.main()
