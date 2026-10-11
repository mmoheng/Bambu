import json
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest import mock
import os

import numpy as np

from bambu_companion.bridge import studio_runner
from bambu_companion.bridge.studio_config import update_studio_config
from bambu_companion.mcp_server import TOOLS, call_tool
from bambu_companion.model_analyzer.mesh_io import load_mesh
from bambu_companion.profiles import project_3mf
from bambu_companion.profiles.multicolour_3mf import MulticolourError, build_multicolour_3mf
from bambu_companion.profiles.preset_library import PresetLibrary
from bambu_companion.service import CompanionService, ServiceError

from .mesh_fixtures import cube_soup, write_stl_binary
from .project_fixtures import FakeStudio, write_preset_tree, write_project_3mf


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        # a 20 mm body (z 0..3) and a small "inlay" sitting on its face at x 5..9
        self.body = self.dir / "body.stl"
        write_stl_binary(cube_soup(20.0, origin=(100.0, 50.0, 0.0)) * np.array([1, 1, 0.15]), self.body)
        self.inlay = self.dir / "inlay.stl"
        write_stl_binary(cube_soup(4.0, origin=(105.0, 58.0, 0.0)) * np.array([1, 1, 0.1]), self.inlay)

    def objects(self, copies=3):
        return [{"name": "Counter", "copies": copies,
                 "parts": [{"path": str(self.body), "filament": 1}, {"path": str(self.inlay), "filament": 2}]}]


class TestBuilder(Case):
    def test_parts_get_their_slots_and_copies_their_instances(self):
        out = self.dir / "counters.3mf"
        summary = build_multicolour_3mf(self.objects(), out)
        self.assertEqual(summary["filament_slots"], 2)
        self.assertIs(summary["fits_one_plate"], True)   # a real bool, so JSON says true
        self.assertEqual([p["filament"] for p in summary["objects"][0]["parts"]], [1, 2])
        with zipfile.ZipFile(out) as zf:
            cfg = ET.fromstring(zf.read("Metadata/model_settings.config"))
            model = ET.fromstring(zf.read("3D/3dmodel.model"))
            names = zf.namelist()
        self.assertNotIn("Metadata/project_settings.config", names)   # geometry + colours only
        parts = cfg.find("object").findall("part")
        slots = [m.get("value") for p in parts for m in p.findall("metadata") if m.get("key") == "extruder"]
        self.assertEqual(slots, ["1", "2"])
        ns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
        self.assertEqual(len(model.findall("m:build/m:item", ns)), 3)
        self.assertEqual(len(cfg.find("plate").findall("model_instance")), 3)
        self.assertEqual(project_3mf.filament_slots_used(out), 2)

    def test_parts_stay_aligned_and_the_object_sits_on_the_plate(self):
        out = self.dir / "one.3mf"
        build_multicolour_3mf(self.objects(copies=1), out)
        mesh = load_mesh(out)            # our own reader follows the component paths
        v = mesh.vertices
        self.assertAlmostEqual(v[:, 2].min(), 0.0, places=4)
        self.assertEqual(mesh.face_count, 24)
        # inlay is 5 mm in from the body's left edge in the source files
        body_x0 = v[:, 0].min()
        inlay = v[(v[:, 2] <= 0.4 + 1e-6) & (v[:, 0] > body_x0 + 1)]
        self.assertAlmostEqual(inlay[:, 0].min() - body_x0, 5.0, places=3)

    def test_refuses_bad_input_and_never_overwrites(self):
        out = self.dir / "x.3mf"
        out.write_bytes(b"keep me")
        with self.assertRaises(MulticolourError):
            build_multicolour_3mf(self.objects(), out)
        self.assertEqual(out.read_bytes(), b"keep me")
        bad_slot = [{"parts": [{"path": str(self.body), "filament": 0}]}]
        with self.assertRaises(MulticolourError):
            build_multicolour_3mf(bad_slot, self.dir / "y.3mf")
        with self.assertRaises(MulticolourError):
            build_multicolour_3mf([{"parts": [{"path": str(self.dir / "nope.stl"), "filament": 1}]}], self.dir / "z.3mf")
        with self.assertRaises(MulticolourError):
            build_multicolour_3mf([{"copies": 0, "parts": [{"path": str(self.body)}]}], self.dir / "w.3mf")
        self.assertFalse((self.dir / "y.3mf").exists())

    def test_too_many_copies_is_reported(self):
        big = self.dir / "big.stl"
        write_stl_binary(cube_soup(120.0), big)
        summary = build_multicolour_3mf([{"copies": 5, "parts": [{"path": str(big), "filament": 1}]}], self.dir / "b.3mf")
        self.assertFalse(summary["fits_one_plate"])


class TestBedType(unittest.TestCase):
    def test_names_and_aliases(self):
        self.assertEqual(studio_runner.normalize_bed_type("textured pei plate"), "Textured PEI Plate")
        self.assertEqual(studio_runner.normalize_bed_type("Smooth PEI Plate"), "High Temp Plate")
        with self.assertRaises(studio_runner.StudioRunnerError):
            studio_runner.normalize_bed_type("Glass")

    def test_plate_refusal_gets_a_plain_hint(self):
        text = studio_runner._studio_error_text(
            {"result_json": {"error_string": "Filaments are not compatible with the plate type", "return_code": -61}}
        )
        self.assertIn("bed_type", text)


class ServiceCase(Case):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"APPDATA": str(self.dir / "AppData")})
        patcher.start()
        self.addCleanup(patcher.stop)
        exe = self.dir / "bambu-studio.exe"
        exe.write_text("")
        update_studio_config(studio_executable=str(exe))
        self.library = PresetLibrary([write_preset_tree(self.dir / "BBL")])
        self.seen_filaments = []
        fake = FakeStudio()

        def runner(executable, args, **kw):
            args = [str(a) for a in args]
            if "--load-filaments" in args:
                files = args[args.index("--load-filaments") + 1].split(";")
                self.seen_filaments = [json.loads(Path(f).read_text(encoding="utf-8")) for f in files]
            return fake(executable, args, **kw)

        self.fake = fake
        self.service = CompanionService(runner=runner, library=self.library)
        self.addCleanup(self.service.close)

    def finished(self, task):
        deadline = time.monotonic() + 10
        while task["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.02)
            task = self.service.task_status(task["task_id"])
        return task


class TestServiceMulticolour(ServiceCase):
    def build(self, **kw):
        task = self.finished(self.service.build_multicolour_project(self.objects(), **kw))
        self.assertEqual(task["state"], "done", task)
        return task["result"]

    def test_build_embeds_settings_bambu_studio_wrote_for_those_presets(self):
        built = self.build(filaments=["Bambu PLA Basic @BBL A1", "Bambu PLA Basic @BBL A1"],
                           filament_colours=["#c12e1f", "#FFFFFF"])
        self.assertTrue(built["output_path"].endswith("body_multicolour.3mf"))
        # the settings come from a real (here: fake) slice of a test cube
        cube_call = self.fake.calls[0]
        self.assertTrue(cube_call[-1].endswith("settings_cube.stl"))
        self.assertEqual(cube_call[:2], ["--curr-bed-type", "Textured PEI Plate"])
        self.assertEqual([f["filament_colour"] for f in self.seen_filaments], [["#C12E1F"], ["#FFFFFF"]])
        self.assertTrue(project_3mf.has_project_settings(built["output_path"]))
        self.assertEqual(project_3mf.filament_slots_used(built["output_path"]), 2)
        self.assertEqual(built["bed_type"], "Textured PEI Plate")
        # and slicing it uses the project's own settings
        task = self.finished(self.service.slice_model(model_path=built["output_path"]))
        self.assertEqual(task["state"], "done", task)
        self.assertEqual(task["result"]["strategy"], "project")

    def test_slots_need_a_filament_each(self):
        with self.assertRaises(ServiceError):
            self.service.build_multicolour_project(self.objects(), filaments=["Bambu PLA Basic @BBL A1"])
        built = self.build()                     # no filaments: the default for every slot
        self.assertEqual(built["presets"]["filaments"], ["Bambu PLA Basic @BBL A1"] * 2)

    def test_presets_slice_of_a_multislot_file_needs_enough_filaments(self):
        built = self.build()
        with self.assertRaises(ServiceError):
            self.service.slice_model(model_path=built["output_path"], filaments=["Bambu PLA Basic @BBL A1"])
        with self.assertRaises(ServiceError):
            self.service.slice_model(model_path=built["output_path"], filament="a", filaments=["b"])
        task = self.finished(self.service.slice_model(
            model_path=built["output_path"], filaments=["Bambu PLA Basic @BBL A1"] * 2,
            process="0.20mm Standard @BBL A1", machine="Bambu Lab A1 0.4 nozzle",
            filament_colours=["#000000", "#FFFFFF"]))
        self.assertEqual(task["state"], "done", task)
        self.assertEqual(task["result"]["strategy"], "presets")
        self.assertEqual(len(task["result"]["presets"]["filaments"]), 2)

    def test_bad_colour_and_plate_are_readable_errors(self):
        task = self.finished(self.service.slice_model(model_path=str(self.body), filament_colours=["red"]))
        self.assertEqual(task["state"], "failed")
        self.assertIn("#RRGGBB", task["error"])
        with self.assertRaises(studio_runner.StudioRunnerError):
            self.service.slice_model(model_path=str(self.body), bed_type="Glass")

    def test_explicit_plate_overrides_the_default(self):
        task = self.finished(self.service.slice_model(model_path=str(self.body), bed_type="Cool Plate"))
        self.assertEqual(task["state"], "done", task)
        self.assertEqual(self.fake.calls[0][:2], ["--curr-bed-type", "Cool Plate"])

    def test_project_keeps_its_plate_unless_one_is_given(self):
        project = write_project_3mf(self.dir / "box.3mf")
        task = self.finished(self.service.slice_model(model_path=str(project)))
        self.assertEqual(task["result"]["strategy"], "project")
        self.assertNotIn("--curr-bed-type", self.fake.calls[0])
        self.assertEqual(task["result"]["bed_type"], "as saved in the project")
        task = self.finished(self.service.slice_model(model_path=str(project), bed_type="Textured PEI Plate"))
        self.assertEqual(task["result"]["strategy"], "project")
        self.assertEqual(self.fake.calls[1][:2], ["--curr-bed-type", "Textured PEI Plate"])

    def test_mcp_tool(self):
        self.assertIn("build_multicolour_project", {t["name"] for t in TOOLS})
        res = call_tool(self.service, "build_multicolour_project", {"objects": self.objects(copies=2), "wait_s": 10})
        self.assertFalse(res["isError"], res)
        payload = json.loads(res["content"][0]["text"])
        self.assertEqual(payload["state"], "done", payload)
        self.assertEqual(payload["result"]["objects"][0]["copies"], 2)
        bad = call_tool(self.service, "build_multicolour_project", {"objects": []})
        self.assertTrue(bad["isError"])


if __name__ == "__main__":
    unittest.main()
