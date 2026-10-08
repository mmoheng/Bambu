import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from bambu_companion.profiles.bambu_values import SettingValueError
from bambu_companion.profiles.project_3mf import (
    ProjectFileError,
    describe_project,
    has_project_settings,
    plate_object_ids,
    read_project_settings,
    read_slice_result,
    write_project_copy,
)

from .project_fixtures import REAL_STYLE_SETTINGS, write_project_3mf, write_two_plate_project


class ProjectCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.project = write_project_3mf(self.dir / "box.3mf")


class TestReading(ProjectCase):
    def test_reads_settings_unmodified(self):
        self.assertEqual(read_project_settings(self.project), REAL_STYLE_SETTINGS)

    def test_describe_project_summary(self):
        info = describe_project(self.project)
        self.assertEqual(info["printer_model"], "Bambu Lab A1")
        self.assertEqual(info["process_preset"], "0.28mm Extra Draft @BBL A1")
        self.assertEqual(info["filament_types"], ["PLA"])
        self.assertEqual(info["nozzle_diameter_mm"], 0.4)
        self.assertEqual(info["bed_type"], "Textured PEI Plate")
        self.assertEqual(info["changed_from_preset"], ["enable_support", "support_type"])
        self.assertEqual(info["settings"]["wall_loops"], 2)
        self.assertEqual(info["settings"]["sparse_infill_density"], 15)
        self.assertEqual(info["settings"]["outer_wall_speed"], 200)
        self.assertIs(info["settings"]["enable_support"], True)
        self.assertEqual(info["slice_result"], [])
        self.assertEqual(info["contains_gcode_for_plates"], [])

    def test_geometry_only_3mf_has_no_settings(self):
        plain = write_project_3mf(self.dir / "plain.3mf", settings=None)
        self.assertFalse(has_project_settings(plain))
        with self.assertRaises(ProjectFileError) as ctx:
            read_project_settings(plain)
        self.assertIn("Save Project", str(ctx.exception))

    def test_missing_and_non_zip_files(self):
        with self.assertRaises(ProjectFileError):
            read_project_settings(self.dir / "nope.3mf")
        bad = self.dir / "bad.3mf"
        bad.write_text("not a zip")
        with self.assertRaises(ProjectFileError):
            read_project_settings(bad)
        self.assertFalse(has_project_settings(bad))

    def test_part_name_lookup_is_case_insensitive(self):
        odd = self.dir / "odd.3mf"
        with zipfile.ZipFile(odd, "w") as zf:
            zf.writestr("Metadata/Project_Settings.config", json.dumps({"wall_loops": "3"}))
        self.assertEqual(read_project_settings(odd), {"wall_loops": "3"})


class TestSliceResult(ProjectCase):
    def test_unsliced_project_has_no_result(self):
        self.assertEqual(read_slice_result(self.project), [])

    def test_sliced_file_reports_time_weight_and_support(self):
        sliced = write_project_3mf(self.dir / "box.gcode.3mf", sliced=True)
        (plate,) = read_slice_result(sliced)
        self.assertEqual(plate["plate"], 1)
        self.assertEqual(plate["print_time_s"], 8354)
        self.assertEqual(plate["print_time"], "2h 19m")
        self.assertEqual(plate["filament_g"], 91.71)
        self.assertIs(plate["support_used"], True)
        self.assertEqual(plate["layers"], 346)
        self.assertEqual(plate["objects"], ["cube.stl"])
        self.assertEqual(plate["filaments"][0]["type"], "PLA")
        self.assertEqual(plate["warnings"], ["not_support_traditional_timelapse"])
        self.assertEqual(describe_project(sliced)["contains_gcode_for_plates"], [1])


class TestWriteProjectCopy(ProjectCase):
    def test_applies_only_the_requested_changes_in_bambu_format(self):
        out = self.dir / "out.3mf"
        result = write_project_copy(
            self.project, out, {"wall_loops": 4, "sparse_infill_density": 20, "outer_wall_speed": 120}
        )
        self.assertEqual(result["written"], str(out))
        self.assertEqual(result["applied"]["wall_loops"], {"from": 2, "to": 4})

        written = read_project_settings(out)
        self.assertEqual(written["wall_loops"], "4")
        self.assertEqual(written["sparse_infill_density"], "20%")
        self.assertEqual(written["outer_wall_speed"], ["120"])
        for key, value in REAL_STYLE_SETTINGS.items():
            if key not in ("wall_loops", "sparse_infill_density", "outer_wall_speed", "different_settings_to_system"):
                self.assertEqual(written[key], value, key)

    def test_changed_keys_are_recorded_as_modified_from_the_preset(self):
        out = self.dir / "out.3mf"
        write_project_copy(self.project, out, {"wall_loops": 4})
        diff = read_project_settings(out)["different_settings_to_system"]
        self.assertEqual(diff[0], "enable_support;support_type;wall_loops")
        self.assertEqual(diff[1:], ["", ""])  # filament / printer entries untouched

    def test_source_is_never_modified(self):
        before = self.project.read_bytes()
        write_project_copy(self.project, self.dir / "out.3mf", {"wall_loops": 4})
        self.assertEqual(self.project.read_bytes(), before)

    def test_every_other_part_is_copied_byte_for_byte(self):
        out = self.dir / "out.3mf"
        write_project_copy(self.project, out, {"wall_loops": 4})
        with zipfile.ZipFile(self.project) as a, zipfile.ZipFile(out) as b:
            self.assertEqual(a.namelist(), b.namelist())
            for name in a.namelist():
                if name != "Metadata/project_settings.config":
                    self.assertEqual(a.read(name), b.read(name), name)

    def test_entries_sharing_a_name_are_each_copied_as_they_were(self):
        import warnings

        odd = self.dir / "dup.3mf"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # zipfile warns about the duplicate name
            with zipfile.ZipFile(odd, "w") as zf:
                zf.writestr("Metadata/project_settings.config", json.dumps(REAL_STYLE_SETTINGS))
                zf.writestr("Metadata/custom.txt", b"FIRST")
                zf.writestr("Metadata/custom.txt", b"SECOND")
            out = self.dir / "dup_out.3mf"
            write_project_copy(odd, out, {"wall_loops": 4})
        with zipfile.ZipFile(out) as zf:
            copies = [zf.read(i) for i in zf.infolist() if i.filename == "Metadata/custom.txt"]
        self.assertEqual(copies, [b"FIRST", b"SECOND"])

    def test_value_already_set_is_reported_unchanged(self):
        result = write_project_copy(self.project, self.dir / "out.3mf", {"wall_loops": 2, "layer_height": 0.2})
        self.assertEqual(result["unchanged"], ["wall_loops"])
        self.assertEqual(list(result["applied"]), ["layer_height"])

    def test_refuses_to_overwrite(self):
        out = self.dir / "out.3mf"
        out.write_bytes(b"precious")
        with self.assertRaises(ProjectFileError):
            write_project_copy(self.project, out, {"wall_loops": 4})
        self.assertEqual(out.read_bytes(), b"precious")
        with self.assertRaises(ProjectFileError):
            write_project_copy(self.project, self.project, {"wall_loops": 4})

    def test_rejects_keys_outside_the_registry_and_writes_nothing(self):
        out = self.dir / "out.3mf"
        with self.assertRaises(SettingValueError):
            write_project_copy(self.project, out, {"wall_loops": 4, "machine_start_gcode": "M109 S300"})
        self.assertFalse(out.exists())

    def test_refuses_a_sliced_export(self):
        sliced = write_project_3mf(self.dir / "box.gcode.3mf", sliced=True)
        out = self.dir / "out.3mf"
        with self.assertRaises(ProjectFileError) as ctx:
            write_project_copy(sliced, out, {"wall_loops": 4})
        self.assertIn("sliced export", str(ctx.exception))
        self.assertFalse(out.exists())

    def test_output_must_be_a_3mf(self):
        with self.assertRaises(ProjectFileError):
            write_project_copy(self.project, self.dir / "out.json", {"wall_loops": 4})



class TestNeverTouchesOtherPeoplesFiles(ProjectCase):
    def test_file_appearing_after_the_check_is_neither_replaced_nor_deleted(self):
        # The exists() check passes, then another writer takes the name
        # before the zip is created. Mode "x" must refuse — and the
        # cleanup path must not delete a file this call never made.
        out = self.dir / "out.3mf"
        real_exists = Path.exists

        def racing_exists(path):
            answer = real_exists(path)
            if path == out and not answer:
                out.write_bytes(b"someone else's file")
            return answer

        with mock.patch.object(Path, "exists", racing_exists):
            with self.assertRaises(ProjectFileError) as ctx:
                write_project_copy(self.project, out, {"wall_loops": 4})
        self.assertIn("already exists", str(ctx.exception))
        self.assertEqual(out.read_bytes(), b"someone else's file")

    def test_half_written_output_is_removed_when_writing_fails(self):
        out = self.dir / "out.3mf"
        with mock.patch("zipfile.ZipFile.writestr", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                write_project_copy(self.project, out, {"wall_loops": 4})
        self.assertFalse(out.exists())

    def test_source_and_destination_must_differ_even_through_another_spelling(self):
        same = self.dir / "sub" / ".." / "box.3mf"
        (self.dir / "sub").mkdir()
        with self.assertRaises(ProjectFileError):
            write_project_copy(self.project, same, {"wall_loops": 4})
        self.assertEqual(read_project_settings(self.project), REAL_STYLE_SETTINGS)


class TestSliceResultStrictness(ProjectCase):
    def _with_slice_info(self, xml: str) -> Path:
        path = self.dir / "odd.3mf"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("Metadata/project_settings.config", json.dumps(REAL_STYLE_SETTINGS))
            zf.writestr("Metadata/slice_info.config", xml)
        return path

    def test_plate_without_a_positive_prediction_is_not_a_slice_result(self):
        for prediction in ("", "0", "abc", "-5"):
            xml = (
                "<config><plate>"
                '<metadata key="index" value="1"/>'
                f'<metadata key="prediction" value="{prediction}"/>'
                '<metadata key="weight" value="0.00"/>'
                "</plate></config>"
            )
            self.assertEqual(read_slice_result(self._with_slice_info(xml)), [], prediction)
            (self.dir / "odd.3mf").unlink()

    def test_plate_entry_with_only_layout_metadata_is_not_a_slice_result(self):
        xml = '<config><plate><metadata key="index" value="1"/></plate></config>'
        self.assertEqual(read_slice_result(self._with_slice_info(xml)), [])

    def test_sliced_plate_says_whether_gcode_is_present(self):
        sliced = write_project_3mf(self.dir / "s.3mf", sliced=True)
        self.assertIs(read_slice_result(sliced)[0]["contains_gcode"], True)

    def test_settings_file_with_a_bom_still_loads(self):
        path = self.dir / "bom.3mf"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("Metadata/project_settings.config", b"\xef\xbb\xbf" + json.dumps({"wall_loops": "3"}).encode())
        self.assertEqual(read_project_settings(path), {"wall_loops": "3"})


class TestPlates(ProjectCase):
    def test_plate_object_ids(self):
        path = write_two_plate_project(self.dir / "two.3mf")
        self.assertEqual(plate_object_ids(path), {1: ["2"], 2: ["4"]})

    def test_file_without_plate_information(self):
        self.assertEqual(plate_object_ids(self.project), {})


if __name__ == "__main__":
    unittest.main()
