import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from bambu_companion.bridge.printer_config import save_printer_config
from bambu_companion.bridge.printer_status import parse_report_payload
from bambu_companion.bridge.studio_config import update_studio_config
from bambu_companion.profiles.bambu_values import SettingValueError
from bambu_companion.profiles.preset_library import PresetLibrary
from bambu_companion.profiles.project_3mf import ProjectFileError, read_project_settings
from bambu_companion.profiles.temp_profile import ApprovalError
from bambu_companion.bridge.job_history import JobRecord
from bambu_companion.service import (
    MAX_WAIT_S,
    PRINTER_STALE_AFTER_S,
    CompanionService,
    ServiceError,
    _model_height,
    _orientation_check,
    _wait_seconds,
)

from .mesh_fixtures import bridge_soup, cube_soup, write_stl_binary
from .project_fixtures import FakeStudio, write_preset_tree, write_project_3mf, write_two_plate_project


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.appdata = self.dir / "AppData"
        patcher = mock.patch.dict(os.environ, {"APPDATA": str(self.appdata)})
        patcher.start()
        self.addCleanup(patcher.stop)

        self.exe = self.dir / "bambu-studio.exe"
        self.exe.write_text("")
        update_studio_config(studio_executable=str(self.exe))
        self.library = PresetLibrary([write_preset_tree(self.dir / "BBL")])
        self.fake = FakeStudio()
        self.service = CompanionService(runner=self.fake, library=self.library)
        self.addCleanup(self.service.close)

        self.project = write_project_3mf(self.dir / "box.3mf")
        self.stl = self.dir / "cube.stl"
        write_stl_binary(cube_soup(20.0), self.stl)

    def finished(self, task: dict) -> dict:
        deadline = time.monotonic() + 10
        while task["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.02)
            task = self.service.task_status(task["task_id"])
        return task


class TestReading(ServiceCase):
    def test_analyze_model(self):
        result = self.service.analyze_model(str(self.stl))
        self.assertEqual(result["size_mm"], [20.0, 20.0, 20.0])
        self.assertTrue(result["fits_a1_bed"])
        self.assertTrue(result["watertight"])
        self.assertEqual(result["overhangs"]["regions"], 0)

    def test_analyze_reports_overhang_regions(self):
        path = self.dir / "bridge.stl"
        write_stl_binary(bridge_soup(), path)
        result = self.service.analyze_model(str(path))
        self.assertGreaterEqual(result["overhangs"]["regions"], 1)
        self.assertGreater(result["overhangs"]["largest_region_mm2"], 0)

    def test_bad_paths_give_readable_errors(self):
        for bad, expected in [
            (str(self.dir / "nope.stl"), "File not found"),
            (str(self.dir), "folder"),
            ("", "required"),
            (None, "required"),
        ]:
            with self.assertRaises(ServiceError) as ctx:
                self.service.analyze_model(bad)
            self.assertIn(expected, str(ctx.exception))
        other = self.dir / "model.obj"
        other.write_text("x")
        with self.assertRaises(ServiceError):
            self.service.analyze_model(str(other))

    def test_read_settings(self):
        info = self.service.read_settings(str(self.project))
        self.assertEqual(info["settings"]["wall_loops"], 2)
        self.assertEqual(info["printer_model"], "Bambu Lab A1")

    def test_list_settings_matches_the_registry(self):
        keys = {s["key"] for s in self.service.list_settings()}
        self.assertIn("wall_loops", keys)
        self.assertNotIn("machine_start_gcode", keys)

    def test_list_presets(self):
        result = self.service.list_presets("process")
        self.assertEqual(result["names"], ["0.20mm Standard @BBL A1"])
        with self.assertRaises(ServiceError):
            self.service.list_presets("gcode")

    def test_studio_info_says_slicing_is_unconfirmed_until_checked(self):
        info = self.service.studio_info()
        self.assertEqual(info["executable"], str(self.exe))
        self.assertIn("NOT yet confirmed", info["headless_slicing"])
        update_studio_config(working_strategy="project")
        self.assertIn("confirmed working", self.service.studio_info()["headless_slicing"])


class TestRecommend(ServiceCase):
    def test_project_supplies_its_own_current_settings(self):
        r = self.service.recommend(str(self.project), "strength")
        self.assertIn("read from box.3mf", r["current_settings_source"])
        self.assertEqual(r["material"], "PLA")
        self.assertEqual(r["nozzle_diameter_mm"], 0.4)
        changes = {c["key"]: c for c in r["changes"]}
        self.assertEqual(changes["wall_loops"]["current_value"], 2)
        self.assertEqual(changes["wall_loops"]["recommended_value"], 4)
        self.assertEqual(r["notes"], [])

    def test_setting_already_at_the_recommended_value_is_not_listed(self):
        project = write_project_3mf(self.dir / "strong.3mf", extra_settings={"wall_loops": "4"})
        r = self.service.recommend(str(project), "strength")
        self.assertNotIn("wall_loops", {c["key"] for c in r["changes"]})

    def test_bare_stl_says_its_settings_are_defaults_not_the_users(self):
        r = self.service.recommend(str(self.stl), "balanced")
        self.assertIn("NOT read from your profile", r["current_settings_source"])

    def test_settings_from_another_project(self):
        r = self.service.recommend(str(self.stl), "balanced", settings_from=str(self.project))
        self.assertIn("read from box.3mf", r["current_settings_source"])

    def test_project_for_another_printer_is_flagged(self):
        project = write_project_3mf(self.dir / "p1s.3mf", extra_settings={"printer_model": "Bambu Lab P1S"})
        r = self.service.recommend(str(project), "balanced")
        self.assertTrue(any("Bambu Lab P1S" in n for n in r["notes"]))

    def test_unknown_goal_and_bad_override(self):
        with self.assertRaises(ServiceError) as ctx:
            self.service.recommend(str(self.stl), "pretty")
        self.assertIn("visual_quality", str(ctx.exception))
        with self.assertRaises(SettingValueError):
            self.service.recommend(str(self.stl), "balanced", current_settings={"machine_start_gcode": "x"})

    def test_job_is_logged(self):
        r = self.service.recommend(str(self.project), "strength")
        (job,) = self.service.job_history()
        self.assertEqual(job["id"], r["job_id"])
        self.assertEqual(job["goal"], "strength")
        self.assertEqual(job["approved_keys"], [])
        self.assertTrue((self.appdata / "BambuCompanion" / "job_history.json").exists())


class TestApplySettings(ServiceCase):
    def test_writes_a_new_project_with_only_the_approved_keys(self):
        r = self.service.recommend(str(self.project), "strength")
        before = self.project.read_bytes()
        result = self.service.apply_settings(job_id=r["job_id"], approved_keys=["wall_loops"])

        written = Path(result["written"])
        self.assertEqual(written, self.dir / "box_companion.3mf")
        settings = read_project_settings(written)
        self.assertEqual(settings["wall_loops"], "4")
        self.assertEqual(settings["layer_height"], "0.28")  # recommended, but not approved
        self.assertEqual(self.project.read_bytes(), before)
        (job,) = self.service.job_history()
        self.assertEqual(job["approved_keys"], ["wall_loops"])
        self.assertEqual(job["output_file"], str(written))

    def test_second_output_gets_a_new_name_instead_of_overwriting(self):
        r = self.service.recommend(str(self.project), "strength")
        first = self.service.apply_settings(job_id=r["job_id"], approved_keys=["wall_loops"])
        second = self.service.apply_settings(job_id=r["job_id"], approved_keys=["layer_height"])
        self.assertNotEqual(first["written"], second["written"])
        self.assertTrue(second["written"].endswith("box_companion_2.3mf"))

    def test_approving_a_key_that_was_not_proposed_is_refused(self):
        r = self.service.recommend(str(self.project), "strength")
        with self.assertRaises(ApprovalError):
            self.service.apply_settings(job_id=r["job_id"], approved_keys=["wall_loops", "seam_position"])
        self.assertFalse((self.dir / "box_companion.3mf").exists())

    def test_explicit_changes_are_validated_against_the_registry(self):
        result = self.service.apply_settings(
            source_project=str(self.project), changes={"support_top_z_distance": 0.25}
        )
        self.assertEqual(read_project_settings(result["written"])["support_top_z_distance"], "0.25")
        with self.assertRaises(SettingValueError):
            self.service.apply_settings(source_project=str(self.project), changes={"machine_start_gcode": "M109"})
        with self.assertRaises(SettingValueError):
            self.service.apply_settings(source_project=str(self.project), changes={"wall_loops": 99})

    def test_nothing_to_apply_and_unknown_job(self):
        with self.assertRaises(ServiceError):
            self.service.apply_settings(source_project=str(self.project))
        with self.assertRaises(ServiceError):
            self.service.apply_settings(job_id="nope", approved_keys=["wall_loops"])
        with self.assertRaises(ServiceError):
            self.service.apply_settings(approved_keys=["wall_loops"])

    def test_stl_job_cannot_be_written_as_a_copy_of_some_other_project(self):
        # settings_from supplied the numbers; it must not become the
        # file that gets copied and handed back as "the model".
        r = self.service.recommend(str(self.stl), "strength", settings_from=str(self.project))
        with self.assertRaises(ServiceError) as ctx:
            self.service.apply_settings(job_id=r["job_id"], approved_keys=["wall_loops"])
        self.assertIn("Save Project", str(ctx.exception))

    def test_geometry_only_3mf_is_refused(self):
        plain = write_project_3mf(self.dir / "plain.3mf", settings=None)
        with self.assertRaises(ProjectFileError):
            self.service.apply_settings(source_project=str(plain), changes={"wall_loops": 4})


class TestSliceModel(ServiceCase):
    def test_project_job_is_sliced_with_the_approved_changes(self):
        r = self.service.recommend(str(self.project), "strength")
        task = self.finished(self.service.slice_model(job_id=r["job_id"], approved_keys=["wall_loops"]))
        self.assertEqual(task["state"], "done", task)
        result = task["result"]
        self.assertEqual(result["strategy"], "project")
        self.assertTrue(result["settings_verified_in_output"])
        self.assertEqual(result["plates"][0]["print_time"], "2h 19m")
        self.assertEqual(Path(result["output_path"]), self.dir / "box_companion_sliced.3mf")
        self.assertEqual(read_project_settings(result["output_path"])["wall_loops"], "4")
        self.assertNotIn("warning", result)
        (job,) = self.service.job_history()
        self.assertEqual(job["slice_result"], "success")

    def test_stl_is_sliced_from_presets_arranged_but_not_reoriented(self):
        task = self.finished(self.service.slice_model(model_path=str(self.stl), changes={"wall_loops": 3}))
        self.assertEqual(task["state"], "done", task)
        self.assertEqual(task["result"]["strategy"], "presets")
        self.assertEqual(task["result"]["presets"]["machine"], "Bambu Lab A1 0.4 nozzle")
        args = self.fake.calls[0]
        self.assertIn("--load-settings", args)
        self.assertIn("--arrange", args)
        # Auto-orient once stood a deck-box lid on its side (89 mm part
        # sliced 118.8 mm tall); the file's orientation is kept.
        self.assertNotIn("--orient", args)

    def test_saved_strategy_modifiers_are_applied(self):
        update_studio_config(working_strategy="presets+bed+fullpath")
        self.finished(self.service.slice_model(model_path=str(self.stl)))
        args = self.fake.calls[0]
        self.assertEqual(args[:2], ["--curr-bed-type", "Textured PEI Plate"])
        self.assertNotIn("--outputdir", args)

    def test_settings_dropped_by_the_slicer_produce_a_warning(self):
        service = CompanionService(runner=FakeStudio(ignore_settings=True), library=self.library)
        task = service.slice_model(model_path=str(self.project), changes={"wall_loops": 4}, wait_s=10)
        self.assertEqual(task["state"], "done", task)
        self.assertFalse(task["result"]["settings_verified_in_output"])
        self.assertEqual(task["result"]["settings_not_found_in_output"], ["wall_loops"])
        self.assertIn("did NOT keep", task["result"]["warning"])

    def test_slicer_failure_is_a_failed_task_with_the_real_error(self):
        service = CompanionService(
            runner=FakeStudio(returncode=206, stderr="plate 1: Nothing to be sliced"), library=self.library
        )
        r = service.recommend(str(self.project), "strength")
        task = service.slice_model(job_id=r["job_id"], approved_keys=["wall_loops"], wait_s=10)
        self.assertEqual(task["state"], "failed")
        self.assertIn("code 206", task["error"])
        self.assertEqual(task["details"]["returncode"], 206)
        self.assertEqual(service.job_history()[0]["slice_result"], "failed")
        self.assertFalse((self.dir / "box_companion_sliced.3mf").exists())

    def test_long_slice_returns_a_running_task_that_can_be_polled(self):
        class Slow(FakeStudio):
            def __call__(self, *args, **kwargs):
                time.sleep(0.3)
                return super().__call__(*args, **kwargs)

        service = CompanionService(runner=Slow(), library=self.library)
        task = service.slice_model(model_path=str(self.project), wait_s=0)
        self.assertEqual(task["state"], "running")
        self.assertIn("task_status", task["next_step"])
        deadline = time.monotonic() + 10
        while task["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
            task = service.task_status(task["task_id"])
        self.assertEqual(task["state"], "done", task)

    def test_refuses_existing_output_and_missing_inputs(self):
        existing = self.dir / "taken.3mf"
        existing.write_bytes(b"precious")
        with self.assertRaises(ServiceError):
            self.service.slice_model(model_path=str(self.project), output_path=str(existing))
        with self.assertRaises(ServiceError):
            self.service.slice_model()
        with self.assertRaises(ServiceError):
            self.service.task_status("nope")
        self.assertEqual(self.fake.calls, [])

    def test_slice_check_through_the_service(self):
        task = self.finished(self.service.slice_check())
        self.assertEqual(task["state"], "done", task)
        self.assertEqual(task["result"]["working_strategy"], "presets")
        self.assertIn("RESULT", task["result"]["summary"])


class TestOutcome(ServiceCase):
    def test_record_outcome(self):
        r = self.service.recommend(str(self.project), "strength")
        self.service.record_outcome(r["job_id"], "  came out clean  ")
        self.assertEqual(self.service.job_history()[0]["outcome_note"], "came out clean")
        with self.assertRaises(ServiceError):
            self.service.record_outcome(r["job_id"], " ")
        with self.assertRaises(ServiceError):
            self.service.record_outcome("nope", "x")


class FakePrinterClient:
    """Feeds the service one accumulated status, like the real client
    does after merging the printer's partial reports."""

    instances: list["FakePrinterClient"] = []

    def __init__(self, *, host, serial, access_code, on_status):
        self.args = (host, serial, access_code)
        self.on_status = on_status
        self.disconnected = False
        FakePrinterClient.instances.append(self)

    def connect(self):
        pass

    def loop_start(self):
        self.on_status(
            parse_report_payload(
                {
                    "bed_temper": 24.5,
                    "sn": "SECRET-SERIAL",
                    "ams": {"ams": [{"id": "0", "humidity": "1", "tray": [
                        {"id": "0", "tray_type": "PLA", "tray_color": "F7D959FF", "tray_uuid": "SECRET-UUID"},
                        {"id": "1", "tray_type": "PETG", "tray_color": "FFFFFF80"},
                    ]}]},
                }
            )
        )  # fmt: skip

    def disconnect(self):
        self.disconnected = True


class TestPrinterStatus(ServiceCase):
    def setUp(self):
        super().setUp()
        FakePrinterClient.instances.clear()
        self.cfg = self.dir / "printer_config.json"
        self.printer_service = CompanionService(
            printer_client_factory=FakePrinterClient, printer_config_path=self.cfg
        )
        self.addCleanup(self.printer_service.close)

    def test_unconfigured_printer_points_at_the_local_setup_and_asks_for_no_secret(self):
        with self.assertRaises(ServiceError) as ctx:
            self.printer_service.printer_status(wait_s=0)
        self.assertIn("printer_setup", str(ctx.exception))
        self.assertEqual(FakePrinterClient.instances, [])

    def test_reports_ams_contents_without_leaking_identifiers_or_credentials(self):
        save_printer_config("192.168.1.50", "SECRET-SERIAL", "SECRET-CODE", self.cfg)
        status = self.printer_service.printer_status(wait_s=2)
        self.assertTrue(status["connected"])
        self.assertEqual(status["bed_temp_c"], 24.5)
        self.assertEqual(
            [(s["slot_index"], s["filament_type"]) for s in status["ams_slots"]], [(0, "PLA"), (1, "PETG")]
        )
        self.assertIn("bed_temper", status["fields_received"])
        flat = repr(status)
        for secret in ("SECRET-SERIAL", "SECRET-CODE", "SECRET-UUID", "192.168.1.50"):
            self.assertNotIn(secret, flat)

    def test_connection_is_reused_and_closed(self):
        save_printer_config("192.168.1.50", "S", "C", self.cfg)
        self.printer_service.printer_status(wait_s=1)
        self.printer_service.printer_status(wait_s=1)
        self.assertEqual(len(FakePrinterClient.instances), 1)
        self.printer_service.close()
        self.assertTrue(FakePrinterClient.instances[0].disconnected)



class TestOutputsAreNeverReplaced(ServiceCase):
    def test_two_slices_at_once_get_two_different_files(self):
        gate = threading.Event()

        class Held(FakeStudio):
            def __call__(self, *args, **kwargs):
                gate.wait(10)
                return super().__call__(*args, **kwargs)

        service = CompanionService(runner=Held(), library=self.library)
        first = service.slice_model(model_path=str(self.project), changes={"wall_loops": 3}, wait_s=0)
        second = service.slice_model(model_path=str(self.project), changes={"wall_loops": 5}, wait_s=0)
        gate.set()
        results = []
        for task in (first, second):
            deadline = time.monotonic() + 10
            while task["state"] == "running" and time.monotonic() < deadline:
                time.sleep(0.02)
                task = service.task_status(task["task_id"])
            self.assertEqual(task["state"], "done", task)
            results.append(task["result"])
        paths = {r["output_path"] for r in results}
        self.assertEqual(len(paths), 2)
        loops = sorted(read_project_settings(p)["wall_loops"] for p in paths)
        self.assertEqual(loops, ["3", "5"])  # neither result was lost

    def test_file_saved_during_a_slice_is_kept_and_the_result_goes_beside_it(self):
        final = self.dir / "box_companion_sliced.3mf"

        class UserSavesMeanwhile(FakeStudio):
            def __call__(self, *args, **kwargs):
                final.write_bytes(b"saved by the user meanwhile")
                return super().__call__(*args, **kwargs)

        service = CompanionService(runner=UserSavesMeanwhile(), library=self.library)
        task = service.slice_model(model_path=str(self.project), wait_s=10)
        self.assertEqual(task["state"], "done", task)
        self.assertEqual(final.read_bytes(), b"saved by the user meanwhile")
        self.assertTrue(task["result"]["output_path"].endswith("box_companion_sliced_2.3mf"))
        self.assertIn("appeared while slicing", task["result"]["output_renamed"])

    def test_parallel_apply_settings_never_report_a_file_that_is_not_there(self):
        barrier = threading.Barrier(6)
        outcomes = []

        def work(n):
            barrier.wait(10)
            try:
                outcomes.append(
                    self.service.apply_settings(source_project=str(self.project), changes={"wall_loops": 3 + n})
                )
            except Exception as exc:  # noqa: BLE001
                outcomes.append(exc)

        threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual([o for o in outcomes if isinstance(o, Exception)], [])
        written = [o["written"] for o in outcomes]
        self.assertEqual(len(set(written)), 6)
        for path in written:
            self.assertTrue(Path(path).exists(), path)
        loops = sorted(read_project_settings(p)["wall_loops"] for p in written)
        self.assertEqual(loops, ["3", "4", "5", "6", "7", "8"])

    def test_output_path_must_be_a_full_path_ending_in_3mf(self):
        for bad in ("relative.3mf", str(self.dir / "out.gcode"), str(self.dir / "out"), 7):
            with self.assertRaises(ServiceError, msg=repr(bad)):
                self.service.slice_model(model_path=str(self.project), output_path=bad)
            with self.assertRaises(ServiceError, msg=repr(bad)):
                self.service.apply_settings(
                    source_project=str(self.project), changes={"wall_loops": 4}, output_path=bad
                )
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(sorted(p.name for p in self.dir.glob("*.3mf")), ["box.3mf"])

    def test_explicit_output_path_is_honoured(self):
        out = self.dir / "sub" / "mine.3mf"
        result = self.service.apply_settings(
            source_project=str(self.project), changes={"wall_loops": 4}, output_path=str(out)
        )
        self.assertEqual(result["written"], str(out))
        with self.assertRaises(ServiceError):
            self.service.apply_settings(
                source_project=str(self.project), changes={"wall_loops": 5}, output_path=str(out)
            )
        self.assertEqual(read_project_settings(out)["wall_loops"], "4")


class TestArgumentChecking(ServiceCase):
    def test_bad_wait_s_is_rejected_before_any_slice_starts(self):
        for bad in ("40s", [1], {"a": 1}, True, float("nan")):
            with self.assertRaises(ServiceError, msg=repr(bad)):
                self.service.slice_model(model_path=str(self.project), wait_s=bad)
            with self.assertRaises(ServiceError, msg=repr(bad)):
                self.service.slice_check(wait_s=bad)
        self.assertEqual(self.fake.calls, [])
        self.assertFalse((self.dir / "box_companion_sliced.3mf").exists())

    def test_wait_is_capped(self):
        self.assertEqual(_wait_seconds(10_000, 40.0), MAX_WAIT_S)
        self.assertEqual(_wait_seconds(-5, 40.0), 0.0)
        self.assertEqual(_wait_seconds(None, 40.0), 40.0)

    def test_wrongly_typed_arguments_give_readable_errors(self):
        r = self.service.recommend(str(self.project), "strength")
        cases = [
            lambda: self.service.apply_settings(source_project=str(self.project), changes=["wall_loops", 4]),
            lambda: self.service.apply_settings(job_id=r["job_id"], approved_keys="wall_loops"),
            lambda: self.service.apply_settings(job_id=r["job_id"], approved_keys=[["wall_loops"]]),
            lambda: self.service.apply_settings(job_id=["x"], approved_keys=["wall_loops"]),
            lambda: self.service.recommend(str(self.stl), "balanced", nozzle_diameter_mm="0.4mm"),
            lambda: self.service.recommend(str(self.stl), "balanced", current_settings=["wall_loops"]),
            lambda: self.service.recommend(str(self.stl), "balanced", material=5),
            lambda: self.service.slice_model(model_path=str(self.stl), machine=["x"]),
            lambda: self.service.analyze_model(str(self.stl), plate="1"),
        ]
        for i, case in enumerate(cases):
            with self.assertRaises(ServiceError, msg=f"case {i}"):
                case()

    def test_relative_model_path_is_resolved(self):
        previous = os.getcwd()
        os.chdir(self.dir)
        try:
            result = self.service.analyze_model("cube.stl")
        finally:
            os.chdir(previous)
        self.assertTrue(Path(result["file"]).is_absolute())

    def test_preset_argument_cannot_be_used_to_read_another_file(self):
        secret = self.dir / "printer_config.json"
        secret.write_text('{"access_code": "SECRET", "machine_start_gcode": "M104 S300"}')
        task = self.service.slice_model(
            model_path=str(self.stl), machine=str(secret.with_suffix("")), wait_s=10
        )
        self.assertEqual(task["state"], "failed")
        self.assertIn("not a preset name", task["error"])
        self.assertNotIn("SECRET", repr(task))
        self.assertEqual(self.fake.calls, [])


class TestSliceHonesty(ServiceCase):
    def test_unverified_settings_are_not_logged_as_applied(self):
        service = CompanionService(runner=FakeStudio(ignore_settings=True), library=self.library)
        r = service.recommend(str(self.project), "strength")
        task = service.slice_model(job_id=r["job_id"], approved_keys=["wall_loops"], wait_s=10)
        self.assertEqual(task["state"], "done", task)
        job = service.job_history()[0]
        self.assertEqual(job["slice_result"], "sliced_but_settings_unverified")
        self.assertEqual(job["applied_settings"], {})
        self.assertIn("wall_loops", job["errors"][0])

    def test_route_not_confirmed_by_slice_check_is_said_so(self):
        task = self.finished(self.service.slice_model(model_path=str(self.project)))
        self.assertFalse(task["result"]["route_confirmed_by_slice_check"])
        self.assertIn("has not confirmed", task["result"]["note"])

        update_studio_config(working_strategy="presets")
        task = self.finished(self.service.slice_model(model_path=str(self.project)))
        self.assertFalse(task["result"]["route_confirmed_by_slice_check"])  # project route != presets
        self.assertIn("it confirmed 'presets'", task["result"]["note"])

        update_studio_config(working_strategy="project+fullpath")
        task = self.finished(self.service.slice_model(model_path=str(self.project)))
        self.assertTrue(task["result"]["route_confirmed_by_slice_check"])
        self.assertNotIn("note", task["result"])

    def test_naming_one_preset_on_a_project_keeps_the_projects_other_presets(self):
        project = write_project_3mf(
            self.dir / "mine.3mf",
            extra_settings={
                "print_settings_id": "0.20mm Standard @BBL A1",
                "filament_settings_id": ["Bambu PLA Basic @BBL A1"],
            },
        )
        update_studio_config(default_process="Some Other Default", default_machine="Some Other Machine")
        task = self.finished(self.service.slice_model(model_path=str(project), filament="Bambu PLA Basic @BBL A1"))
        self.assertEqual(task["state"], "done", task)
        self.assertEqual(
            task["result"]["presets"],
            {
                "machine": "Bambu Lab A1 0.4 nozzle",
                "process": "0.20mm Standard @BBL A1",
                "filaments": ["Bambu PLA Basic @BBL A1"],
            },
        )
        self.assertNotIn("--orient", self.fake.calls[0])  # a project keeps its plate layout

    def test_unsliced_output_is_a_failed_task(self):
        service = CompanionService(runner=FakeStudio(unsliced_output=True), library=self.library)
        task = service.slice_model(model_path=str(self.project), wait_s=10)
        self.assertEqual(task["state"], "failed")
        self.assertIn("no slice result", task["error"])
        self.assertFalse((self.dir / "box_companion_sliced.3mf").exists())


class TestHistoryRobustness(ServiceCase):
    def test_damaged_history_file_does_not_block_a_recommendation(self):
        self.service.recommend(str(self.project), "strength")
        history = self.appdata / "BambuCompanion" / "job_history.json"
        for damaged in ("", "[{"):
            history.write_text(damaged)
            r = self.service.recommend(str(self.project), "strength")
            self.assertTrue(r["changes"])
            self.assertTrue(any("job history" in n for n in r["notes"]), damaged)
            # ...and the job can still be applied from memory.
            result = self.service.apply_settings(job_id=r["job_id"], approved_keys=["wall_loops"])
            self.assertTrue(Path(result["written"]).exists())

    def test_parallel_history_writes_lose_nothing(self):
        store = self.service.history
        errors = []

        def work(n):
            try:
                for i in range(20):
                    store.record_job(
                        JobRecord(
                            id=f"{n}-{i}", model_file="m.stl", started_at="t", printer="A1",
                            nozzle_diameter_mm=0.4, material="PLA", ams_slot=None, goal="balanced",
                            starting_settings={}, recommended_changes=[], approved_keys=[], applied_settings={},
                        )
                    )  # fmt: skip
                    store.update_job(f"{n}-{i}", outcome_note="ok")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        jobs = store.load_jobs()
        self.assertEqual(len(jobs), 80)
        self.assertTrue(all(j["outcome_note"] == "ok" for j in jobs))
        self.assertEqual([p.name for p in store.path.parent.glob("*.tmp")], [])


class TestPlateSelection(ServiceCase):
    def setUp(self):
        super().setUp()
        self.two = write_two_plate_project(self.dir / "two.3mf")

    def test_multi_plate_project_is_analysed_one_plate_at_a_time(self):
        first = self.service.analyze_model(str(self.two))
        self.assertEqual(first["size_mm"], [10.0, 10.0, 10.0])
        self.assertTrue(first["fits_a1_bed"])
        self.assertEqual(first["bodies"], 1)
        self.assertIn("2 plates", first["notes"][0])
        second = self.service.analyze_model(str(self.two), plate=2)
        self.assertEqual(second["size_mm"], [10.0, 10.0, 10.0])
        with self.assertRaises(ServiceError):
            self.service.analyze_model(str(self.two), plate=3)

    def test_recommend_carries_the_plate_note(self):
        r = self.service.recommend(str(self.two), "balanced")
        self.assertTrue(any("plate 1 only" in n for n in r["notes"]))
        self.assertEqual(r["analysis"]["size_mm"], [10.0, 10.0, 10.0])

    def test_single_plate_project_has_no_note(self):
        self.assertEqual(self.service.analyze_model(str(self.project))["notes"], [])


class UnreachablePrinter:
    def __init__(self, *, host, serial, access_code, on_status):
        self.host = host

    def connect(self):
        from bambu_companion.bridge.printer_status import PrinterConnectionError

        # What an older/lower layer might put in its message.
        raise PrinterConnectionError(f"Could not reach printer at {self.host}:8883 (code SECRET-CODE)")


class SilentPrinter(FakePrinterClient):
    def loop_start(self):
        pass  # connected, but the printer never reports


class TestPrinterStatusFailures(ServiceCase):
    def setUp(self):
        super().setUp()
        FakePrinterClient.instances.clear()
        self.cfg = self.dir / "printer_config.json"
        save_printer_config("192.168.1.50", "SECRET-SERIAL", "SECRET-CODE", self.cfg)

    def test_connection_failure_does_not_reveal_the_address_or_code(self):
        service = CompanionService(printer_client_factory=UnreachablePrinter, printer_config_path=self.cfg)
        with self.assertRaises(ServiceError) as ctx:
            service.printer_status(wait_s=0)
        message = str(ctx.exception)
        self.assertIn("Could not connect to the printer", message)
        for secret in ("192.168.1.50", "SECRET-CODE", "SECRET-SERIAL", "8883"):
            self.assertNotIn(secret, message)

    def test_real_client_error_message_carries_no_address(self):
        import types

        from bambu_companion.bridge import printer_status as ps

        class Refusing:
            def __init__(self, *a):
                pass

            username_pw_set = tls_set = tls_insecure_set = lambda self, *a, **k: None

            def connect(self, host, port, keepalive=30):
                raise OSError(f"[Errno 111] Connection refused to {host}")

        with mock.patch.object(ps, "mqtt", types.SimpleNamespace(Client=Refusing)):
            client = ps.PrinterStatusClient(host="192.168.1.50", serial="S", access_code="C", on_status=print)
            with self.assertRaises(ps.PrinterConnectionError) as ctx:
                client.connect()
        self.assertNotIn("192.168.1.50", str(ctx.exception))
        self.assertIn("OSError", str(ctx.exception))  # the cause is named, not echoed

    def test_no_report_yet_is_not_reported_as_connected(self):
        service = CompanionService(printer_client_factory=SilentPrinter, printer_config_path=self.cfg)
        self.addCleanup(service.close)
        status = service.printer_status(wait_s=0.2)
        self.assertFalse(status["connected"])
        self.assertIn("access code", status["message"])
        self.assertNotIn("192.168.1.50", repr(status))

    def test_old_data_is_shown_as_stale_not_live(self):
        service = CompanionService(printer_client_factory=FakePrinterClient, printer_config_path=self.cfg)
        self.addCleanup(service.close)
        self.assertTrue(service.printer_status(wait_s=2)["connected"])
        with service._lock:
            service._printer["updated"] -= PRINTER_STALE_AFTER_S + 5
        status = service.printer_status(wait_s=0)
        self.assertFalse(status["connected"])
        self.assertIn("last known values", status["message"])
        self.assertEqual(status["bed_temp_c"], 24.5)


class TestOrientationCheck(ServiceCase):
    def test_matching_height_gives_no_warning(self):
        self.assertIsNone(_orientation_check(87.0, [{"max_z_mm": 87.0}]))
        self.assertIsNone(_orientation_check(87.0, [{"max_z_mm": 87.08}]))
        self.assertIsNone(_orientation_check(None, [{"max_z_mm": 5.0}]))
        self.assertIsNone(_orientation_check(87.0, [{}]))

    def test_turned_over_part_is_flagged(self):
        # The real case: an 89.05 mm lid sliced 118.76 mm tall.
        warning = _orientation_check(89.05, [{"max_z_mm": 118.76}])
        self.assertIn("89.0 mm tall", warning)
        self.assertIn("118.8 mm tall", warning)

    def test_slice_result_carries_the_warning(self):
        class TurnsItOver(FakeStudio):
            def __call__(self, executable, args, **kwargs):
                run = super().__call__(executable, args, **kwargs)
                return run  # the fixture's G-code header always says 88.96 mm

        # 20 mm cube vs. the fixture's 88.96 mm sliced height -> flagged.
        task = self.finished(
            CompanionService(runner=TurnsItOver(), library=self.library).slice_model(
                model_path=str(self.stl), wait_s=10
            )
        )
        self.assertEqual(task["state"], "done", task)
        self.assertIn("different orientation", task["result"]["orientation_warning"])

    def test_model_height(self):
        self.assertAlmostEqual(_model_height(self.stl), 20.0)


if __name__ == "__main__":
    unittest.main()
