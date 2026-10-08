import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bambu_companion.bridge import studio_runner as sr
from bambu_companion.bridge.studio_runner import (
    SliceJobSpec,
    StudioRunnerError,
    build_cli_args,
    candidate_executables,
    find_bambu_studio_executable,
    place_exclusive,
    run_slice_job,
    slice_project,
    slice_with_presets,
    verify_settings_applied,
    write_full_preset_files,
)
from bambu_companion.profiles.bambu_values import SettingValueError
from bambu_companion.profiles.preset_library import PresetLibrary
from bambu_companion.profiles.project_3mf import read_project_settings, read_slice_result

from .mesh_fixtures import cube_soup, write_stl_binary
from .project_fixtures import FakeStudio, write_preset_tree, write_project_3mf


class TestBuildCliArgs(unittest.TestCase):
    def test_machine_and_process_go_in_one_semicolon_separated_argument(self):
        # Bambu's documented form: --load-settings "machine.json;process.json"
        spec = SliceJobSpec(
            model_path=Path("model.stl"),
            output_3mf_path=Path("outdir") / "out.3mf",
            machine_settings_path=Path("machine.json"),
            process_settings_path=Path("process.json"),
            filament_settings_paths=[Path("f1.json"), Path("f2.json")],
            orient=True,
            arrange=True,
        )
        self.assertEqual(
            build_cli_args(spec),
            [
                "--load-settings", "machine.json;process.json",
                "--load-filaments", "f1.json;f2.json",
                "--orient", "1",
                "--arrange", "1",
                "--slice", "0",
                "--debug", "2",
                "--outputdir", "outdir",
                "--export-3mf", "out.3mf",
                "model.stl",
            ],
        )  # fmt: skip

    def test_project_needs_no_settings_files(self):
        # Docs example 1: a project 3MF carries its own settings.
        spec = SliceJobSpec(model_path=Path("project.3mf"), output_3mf_path=Path("o") / "out.3mf")
        args = build_cli_args(spec)
        self.assertNotIn("--load-settings", args)
        self.assertNotIn("--load-filaments", args)
        self.assertNotIn("--orient", args)
        self.assertNotIn("--arrange", args)
        self.assertEqual(args[-1], "project.3mf")  # input file last, as in the docs

    def test_full_output_path_variant_and_bed_type_and_plate(self):
        spec = SliceJobSpec(
            model_path=Path("m.stl"),
            output_3mf_path=Path("o") / "out.3mf",
            use_outputdir=False,
            bed_type="Textured PEI Plate",
            plate=2,
        )
        args = build_cli_args(spec)
        self.assertNotIn("--outputdir", args)
        self.assertEqual(args[args.index("--export-3mf") + 1], str(Path("o") / "out.3mf"))
        self.assertEqual(args[:2], ["--curr-bed-type", "Textured PEI Plate"])
        self.assertEqual(args[args.index("--slice") + 1], "2")

    def test_extra_args_come_before_the_input_file(self):
        spec = SliceJobSpec(model_path=Path("m.stl"), output_3mf_path=Path("o.3mf"), extra_args=["--uptodate"])
        self.assertEqual(build_cli_args(spec)[-2:], ["--uptodate", "m.stl"])


class TestFindExecutable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _make(self, relative: str) -> Path:
        path = self.dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
        return path

    def test_candidates_cover_every_given_drive_and_the_no_program_files_layout(self):
        candidates = candidate_executables([Path("C:/"), Path("G:/")])
        self.assertIn(Path("G:/") / "Bambu Studio" / "bambu-studio.exe", candidates)
        self.assertIn(Path("C:/") / "Program Files/Bambu Studio" / "bambu-studio.exe", candidates)

    def test_finds_an_install_on_a_non_default_drive(self):
        # The real machine had it at G:\Bambu Studio\bambu-studio.exe.
        exe = self._make("G/Bambu Studio/bambu-studio.exe")
        (self.dir / "C").mkdir()
        found = find_bambu_studio_executable(
            drive_roots=[self.dir / "C", self.dir / "G"], env={}, saved_path=""
        )
        self.assertEqual(found, exe)

    def test_explicit_path_wins_and_a_bad_one_is_an_error_not_a_fallback(self):
        good = self._make("G/Bambu Studio/bambu-studio.exe")
        explicit = self._make("elsewhere/bambu-studio.exe")
        self.assertEqual(find_bambu_studio_executable(explicit, drive_roots=[self.dir / "G"], env={}), explicit)
        with self.assertRaises(StudioRunnerError):
            find_bambu_studio_executable(self.dir / "missing.exe", drive_roots=[self.dir / "G"], env={})
        self.assertTrue(good.exists())

    def test_environment_variable_then_saved_path(self):
        env_exe = self._make("env/bambu-studio.exe")
        saved_exe = self._make("saved/bambu-studio.exe")
        self.assertEqual(
            find_bambu_studio_executable(
                drive_roots=[], env={sr.ENV_EXECUTABLE: str(env_exe)}, saved_path=str(saved_exe)
            ),
            env_exe,
        )
        self.assertEqual(find_bambu_studio_executable(drive_roots=[], env={}, saved_path=str(saved_exe)), saved_exe)

    def test_stale_saved_path_falls_through_to_the_drive_search(self):
        exe = self._make("G/Bambu Studio/bambu-studio.exe")
        found = find_bambu_studio_executable(
            drive_roots=[self.dir / "G"], env={}, saved_path=str(self.dir / "uninstalled.exe")
        )
        self.assertEqual(found, exe)


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.exe = self.dir / "bambu-studio.exe"
        self.exe.write_text("")
        self.project = write_project_3mf(self.dir / "box.3mf")


class TestVerifySettingsApplied(RunnerCase):
    def test_compares_values_across_representations(self):
        ok, missing = verify_settings_applied(
            self.project,
            {"wall_loops": 2, "sparse_infill_density": 15, "outer_wall_speed": 200, "enable_support": True},
        )
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_flags_a_different_value_and_a_missing_key(self):
        ok, missing = verify_settings_applied(self.project, {"wall_loops": 4, "support_interface_spacing": 0.5})
        self.assertFalse(ok)
        self.assertEqual(missing, ["wall_loops", "support_interface_spacing"])

    def test_unreadable_output_is_an_error_not_a_pass(self):
        plain = write_project_3mf(self.dir / "plain.3mf", settings=None)
        with self.assertRaises(StudioRunnerError):
            verify_settings_applied(plain, {"wall_loops": 4})


class TestRunSliceJob(RunnerCase):
    def _spec(self) -> SliceJobSpec:
        return SliceJobSpec(model_path=self.project, output_3mf_path=self.dir / "out" / "sliced.3mf")

    def test_success_returns_the_slice_result(self):
        result = run_slice_job(self._spec(), executable_path=self.exe, runner=FakeStudio())
        self.assertTrue(result["success"])
        self.assertEqual(result["slice_result"][0]["print_time_s"], 8354)
        self.assertTrue(result["settings_verified"])

    def test_nonzero_exit_raises_with_the_real_message_and_evidence(self):
        fake = FakeStudio(returncode=206, stderr="plate 1: Nothing to be sliced, Either the print is empty")
        with self.assertRaises(StudioRunnerError) as ctx:
            run_slice_job(self._spec(), executable_path=self.exe, runner=fake)
        self.assertIn("code 206", str(ctx.exception))
        self.assertIn("Nothing to be sliced", str(ctx.exception))
        self.assertEqual(ctx.exception.details["returncode"], 206)
        self.assertEqual(ctx.exception.details["command"][0], str(self.exe))

    def test_exit_zero_without_an_output_file_is_a_failure(self):
        with self.assertRaises(StudioRunnerError) as ctx:
            run_slice_job(self._spec(), executable_path=self.exe, runner=FakeStudio(write_output=False))
        self.assertIn("did not create the output file", str(ctx.exception))

    def test_bambu_studios_result_json_is_included_in_the_error(self):
        def runner(executable, args, *, timeout_s, cwd):
            (Path(cwd) / "result.json").write_text(json.dumps({"return_code": -50, "error_string": "no object"}))
            return {"command": [str(executable), *args], "returncode": 206, "duration_s": 0.1, "stdout_tail": "", "stderr_tail": ""}

        with self.assertRaises(StudioRunnerError) as ctx:
            run_slice_job(self._spec(), executable_path=self.exe, runner=runner)
        self.assertIn("no object", str(ctx.exception))
        self.assertEqual(ctx.exception.details["result_json"]["return_code"], -50)

    def test_silently_ignored_settings_are_reported_not_hidden(self):
        result = run_slice_job(
            self._spec(),
            executable_path=self.exe,
            expected_settings={"wall_loops": 4},
            runner=FakeStudio(ignore_settings=True),
        )
        self.assertFalse(result["settings_verified"])
        self.assertEqual(result["unverified_settings"], ["wall_loops"])

    def test_refuses_to_overwrite_an_existing_output(self):
        spec = self._spec()
        spec.output_3mf_path.parent.mkdir(parents=True)
        spec.output_3mf_path.write_bytes(b"precious")
        fake = FakeStudio()
        with self.assertRaises(StudioRunnerError):
            run_slice_job(spec, executable_path=self.exe, runner=fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(spec.output_3mf_path.read_bytes(), b"precious")


class TestSliceProject(RunnerCase):
    def test_changes_reach_the_slicer_and_the_source_is_untouched(self):
        before = self.project.read_bytes()
        fake = FakeStudio()
        out = self.dir / "result.3mf"
        result = slice_project(
            self.project, out, changes={"wall_loops": 4, "layer_height": 0.2}, executable_path=self.exe, runner=fake
        )
        self.assertEqual(result["output_path"], str(out))
        self.assertEqual(result["strategy"], sr.STRATEGY_PROJECT)
        self.assertTrue(result["settings_verified"])
        written = read_project_settings(out)
        self.assertEqual(written["wall_loops"], "4")
        self.assertEqual(written["layer_height"], "0.2")
        self.assertEqual(self.project.read_bytes(), before)
        # The slicer was handed a temporary copy, never the user's file.
        self.assertNotEqual(fake.calls[0][-1], str(self.project))

    def test_slicer_that_drops_the_settings_is_flagged(self):
        result = slice_project(
            self.project,
            self.dir / "result.3mf",
            changes={"wall_loops": 4},
            executable_path=self.exe,
            runner=FakeStudio(ignore_settings=True),
        )
        self.assertFalse(result["settings_verified"])

    def test_invalid_change_never_launches_the_slicer(self):
        fake = FakeStudio()
        with self.assertRaises(SettingValueError):
            slice_project(
                self.project, self.dir / "r.3mf", changes={"machine_start_gcode": "M109 S300"},
                executable_path=self.exe, runner=fake,
            )  # fmt: skip
        self.assertEqual(fake.calls, [])

    def test_failure_leaves_no_output_behind(self):
        out = self.dir / "result.3mf"
        with self.assertRaises(StudioRunnerError):
            slice_project(self.project, out, executable_path=self.exe, runner=FakeStudio(returncode=1))
        self.assertFalse(out.exists())


class TestSliceWithPresets(RunnerCase):
    def setUp(self):
        super().setUp()
        self.library = PresetLibrary([write_preset_tree(self.dir / "BBL")])
        self.stl = self.dir / "cube.stl"
        write_stl_binary(cube_soup(20.0), self.stl)
        self.presets = dict(
            machine="Bambu Lab A1 0.4 nozzle", process="0.20mm Standard @BBL A1", filaments=["Bambu PLA Basic @BBL A1"]
        )

    def test_writes_full_configs_with_changes_on_the_process_only(self):
        machine, process, filaments = write_full_preset_files(
            self.library, self.dir / "s", changes={"wall_loops": 4, "outer_wall_speed": 120}, **self.presets
        )
        process_cfg = json.loads(process.read_text())
        self.assertEqual(process_cfg["wall_loops"], "4")
        self.assertEqual(process_cfg["outer_wall_speed"], ["120"])
        self.assertEqual(process_cfg["top_shell_layers"], "5")  # inherited, so the config is full
        self.assertEqual(process_cfg["type"], "process")
        self.assertEqual(json.loads(machine.read_text())["printable_height"], "256")
        self.assertEqual(len(filaments), 1)
        # The preset on disk was only read.
        on_disk = json.loads((self.dir / "BBL" / "process" / "0.20mm Standard @BBL A1.json").read_text())
        self.assertNotIn("wall_loops", on_disk)

    def test_stl_is_sliced_with_the_documented_command_line(self):
        fake = FakeStudio()
        out = self.dir / "cube_sliced.3mf"
        result = slice_with_presets(
            self.stl, out, library=self.library, changes={"wall_loops": 4},
            bed_type="Textured PEI Plate", executable_path=self.exe, runner=fake, **self.presets,
        )  # fmt: skip
        args = fake.calls[0]
        machine_file, process_file = args[args.index("--load-settings") + 1].split(";")
        self.assertTrue(machine_file.endswith("machine.json"))
        self.assertTrue(process_file.endswith("process.json"))
        self.assertIn("--load-filaments", args)
        self.assertNotIn("--orient", args)  # the file's orientation is kept
        self.assertEqual(args[args.index("--arrange") + 1], "1")
        self.assertEqual(args[-1], str(self.stl))
        self.assertTrue(result["settings_verified"])
        self.assertEqual(result["presets"]["process"], "0.20mm Standard @BBL A1")
        self.assertTrue(out.exists())

    def test_missing_model_never_launches_the_slicer(self):
        fake = FakeStudio()
        with self.assertRaises(StudioRunnerError):
            slice_with_presets(
                self.dir / "nope.stl", self.dir / "o.3mf", library=self.library,
                executable_path=self.exe, runner=fake, **self.presets,
            )  # fmt: skip
        self.assertEqual(fake.calls, [])



class TestSliceResultIsRequired(RunnerCase):
    def _spec(self) -> SliceJobSpec:
        return SliceJobSpec(model_path=self.project, output_3mf_path=self.dir / "out" / "sliced.3mf")

    def test_exit_zero_with_an_unsliced_output_is_a_failure(self):
        with self.assertRaises(StudioRunnerError) as ctx:
            run_slice_job(self._spec(), executable_path=self.exe, runner=FakeStudio(unsliced_output=True))
        self.assertIn("no slice result", str(ctx.exception))

    def test_exit_zero_with_an_unreadable_output_is_a_runner_error_not_a_crash(self):
        with self.assertRaises(StudioRunnerError) as ctx:
            run_slice_job(self._spec(), executable_path=self.exe, runner=FakeStudio(garbage_output=True))
        self.assertIn("can't be read", str(ctx.exception))


class TestPlaceExclusive(RunnerCase):
    def setUp(self):
        super().setUp()
        self.source = self.dir / "scratch" / "sliced.3mf"
        self.source.parent.mkdir()
        self.source.write_bytes(b"result")

    def test_free_name_is_used(self):
        target = self.dir / "final.3mf"
        self.assertEqual(place_exclusive(self.source, target), target)
        self.assertEqual(target.read_bytes(), b"result")

    def test_taken_name_is_never_replaced_the_result_goes_next_to_it(self):
        target = self.dir / "final.3mf"
        target.write_bytes(b"precious")
        (self.dir / "final_2.3mf").write_bytes(b"also precious")
        placed = place_exclusive(self.source, target)
        self.assertEqual(placed, self.dir / "final_3.3mf")
        self.assertEqual(target.read_bytes(), b"precious")
        self.assertEqual((self.dir / "final_2.3mf").read_bytes(), b"also precious")
        self.assertEqual(placed.read_bytes(), b"result")

    def test_failed_copy_leaves_nothing_behind(self):
        target = self.dir / "final.3mf"
        with mock.patch("shutil.copyfileobj", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                place_exclusive(self.source, target)
        self.assertFalse(target.exists())


class AppearsWhileSlicing(FakeStudio):
    """A slicer during whose run somebody saves a file at the final
    destination — the long-slice race the up-front checks can't see."""

    def __init__(self, final: Path):
        super().__init__()
        self.final = final

    def __call__(self, *args, **kwargs):
        self.final.write_bytes(b"saved by the user meanwhile")
        return super().__call__(*args, **kwargs)


class TestDestinationIsNeverReplaced(RunnerCase):
    def test_slice_project_refuses_an_existing_destination_up_front(self):
        out = self.dir / "result.3mf"
        out.write_bytes(b"precious")
        fake = FakeStudio()
        with self.assertRaises(StudioRunnerError):
            slice_project(self.project, out, executable_path=self.exe, runner=fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(out.read_bytes(), b"precious")

    def test_slice_with_presets_refuses_an_existing_destination_up_front(self):
        library = PresetLibrary([write_preset_tree(self.dir / "BBL")])
        stl = self.dir / "cube.stl"
        write_stl_binary(cube_soup(20.0), stl)
        out = self.dir / "result.3mf"
        out.write_bytes(b"precious")
        fake = FakeStudio()
        with self.assertRaises(StudioRunnerError):
            slice_with_presets(
                stl, out, library=library, machine="Bambu Lab A1 0.4 nozzle",
                process="0.20mm Standard @BBL A1", filaments=["Bambu PLA Basic @BBL A1"],
                executable_path=self.exe, runner=fake,
            )  # fmt: skip
        self.assertEqual(fake.calls, [])
        self.assertEqual(out.read_bytes(), b"precious")

    def test_file_that_appears_during_the_slice_survives_and_the_result_is_kept_too(self):
        out = self.dir / "result.3mf"
        result = slice_project(self.project, out, executable_path=self.exe, runner=AppearsWhileSlicing(out))
        self.assertEqual(out.read_bytes(), b"saved by the user meanwhile")
        self.assertEqual(result["output_path"], str(self.dir / "result_2.3mf"))
        self.assertIn("appeared while slicing", result["output_renamed"])
        self.assertEqual(len(read_slice_result(result["output_path"])), 1)


class TestPresetsRouteVerification(RunnerCase):
    def test_settings_dropped_on_the_presets_route_are_flagged_too(self):
        library = PresetLibrary([write_preset_tree(self.dir / "BBL")])
        stl = self.dir / "cube.stl"
        write_stl_binary(cube_soup(20.0), stl)
        result = slice_with_presets(
            stl, self.dir / "o.3mf", library=library, changes={"wall_loops": 4},
            machine="Bambu Lab A1 0.4 nozzle", process="0.20mm Standard @BBL A1",
            filaments=["Bambu PLA Basic @BBL A1"], executable_path=self.exe,
            runner=FakeStudio(ignore_settings=True),
        )  # fmt: skip
        self.assertFalse(result["settings_verified"])
        self.assertEqual(result["unverified_settings"], ["wall_loops"])


class TestRelativePaths(RunnerCase):
    def test_relative_model_path_is_made_absolute_before_the_slicer_sees_it(self):
        # Bambu Studio is started in a scratch folder; a relative path
        # would point at nothing there.
        library = PresetLibrary([write_preset_tree(self.dir / "BBL")])
        write_stl_binary(cube_soup(20.0), self.dir / "cube.stl")
        fake = FakeStudio()
        previous = os.getcwd()
        os.chdir(self.dir)
        try:
            slice_with_presets(
                "cube.stl", "out.3mf", library=library, machine="Bambu Lab A1 0.4 nozzle",
                process="0.20mm Standard @BBL A1", filaments=["Bambu PLA Basic @BBL A1"],
                executable_path=self.exe, runner=fake,
            )  # fmt: skip
        finally:
            os.chdir(previous)
        self.assertTrue(Path(fake.calls[0][-1]).is_absolute())
        self.assertTrue((self.dir / "out.3mf").exists())


class TestRunCli(unittest.TestCase):
    def test_child_gets_no_stdin_and_output_is_captured(self):
        # Inside the connector our stdin is the protocol stream; a child
        # that inherited it could swallow messages.
        code = "import sys; data = sys.stdin.read(); print('stdin bytes:', len(data)); sys.exit(3)"
        run = sr.run_cli(Path(sys.executable), ["-c", code], timeout_s=60)
        self.assertEqual(run["returncode"], 3)
        self.assertIn("stdin bytes: 0", run["stdout_tail"])
        self.assertEqual(run["command"][0], sys.executable)

    def test_timeout_and_missing_program_are_runner_errors(self):
        with self.assertRaises(StudioRunnerError) as ctx:
            sr.run_cli(Path(sys.executable), ["-c", "import time; time.sleep(30)"], timeout_s=0.5)
        self.assertIn("did not finish", str(ctx.exception))
        with self.assertRaises(StudioRunnerError):
            sr.run_cli(Path("/definitely/not/a/program"), [], timeout_s=5)


class TestWhichIsNotUsedOnWindows(unittest.TestCase):
    def test_path_lookup_is_skipped_on_windows(self):
        with mock.patch.object(sr.os, "name", "nt"), mock.patch.object(
            sr.shutil, "which", return_value="/cwd/bambu-studio.exe"
        ) as which:
            with self.assertRaises(StudioRunnerError):
                find_bambu_studio_executable(drive_roots=[], env={}, saved_path="")
        which.assert_not_called()


if __name__ == "__main__":
    unittest.main()
