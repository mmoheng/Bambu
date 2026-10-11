import http.client
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from bambu_companion.bridge.studio_config import update_studio_config
from bambu_companion.dashboard import Dashboard
from bambu_companion.preview import encode_png, preview_png, render_mesh
from bambu_companion.profiles.preset_library import PresetLibrary
from bambu_companion.service import CompanionService

from .mesh_fixtures import cube_soup, write_stl_binary
from .project_fixtures import FakeStudio, write_preset_tree

import numpy as np


class DashboardCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"APPDATA": str(self.dir / "AppData")})
        patcher.start()
        self.addCleanup(patcher.stop)
        exe = self.dir / "bambu-studio.exe"
        exe.write_text("")
        update_studio_config(studio_executable=str(exe))
        self.library = PresetLibrary([write_preset_tree(self.dir / "BBL")])
        self.service = CompanionService(
            runner=FakeStudio(), library=self.library, printer_config_path=self.dir / "no_printer.json"
        )
        self.addCleanup(self.service.close)
        self.stl = self.dir / "cube.stl"
        write_stl_binary(cube_soup(20.0), self.stl)
        self.dash = Dashboard(self.service, port=0)
        self.assertTrue(self.dash.start())
        self.addCleanup(self.dash.stop)
        self.port = self.dash.httpd.server_address[1]

    def get(self, path, host=None, method="GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, headers={"Host": host or f"127.0.0.1:{self.port}"})
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r.status, r.getheader("Content-Type"), body

    def finished(self, task):
        deadline = time.monotonic() + 10
        while task["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.02)
            task = self.service.task_status(task["task_id"])
        return task


class TestDashboard(DashboardCase):
    def test_page_and_empty_state(self):
        status, ctype, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"Bambu Companion", body)
        status, _, body = self.get("/api/state")
        state = json.loads(body)
        self.assertEqual(state["activity"], [])
        self.assertEqual(state["printer"], {"configured": False})

    def test_a_slice_shows_up_with_its_figures_and_picture(self):
        task = self.finished(self.service.slice_model(model_path=str(self.stl)))
        self.assertEqual(task["state"], "done", task)
        state = json.loads(self.get("/api/state")[2])
        (rec,) = state["activity"]
        self.assertEqual(rec["kind"], "slice")
        self.assertEqual(rec["state"], "done")
        self.assertEqual(rec["label"], "Slice cube.stl")
        self.assertEqual(rec["plates"][0]["print_time"], "2h 19m")
        self.assertTrue(rec["output_path"].endswith("cube_companion_sliced.3mf"))
        status, ctype, body = self.get("/api/preview?file=" + self.stl.as_posix().replace(" ", "%20"))
        self.assertEqual(status, 200, body)
        self.assertEqual(ctype, "image/png")
        self.assertTrue(body.startswith(b"\x89PNG"))

    def test_activity_survives_a_restart(self):
        self.finished(self.service.slice_model(model_path=str(self.stl)))
        fresh = CompanionService(runner=FakeStudio(), library=self.library)
        self.addCleanup(fresh.close)
        (rec,) = fresh.activity()
        self.assertEqual(rec["state"], "done")
        self.assertIn("plates", rec)

    def test_failed_task_shows_its_error(self):
        svc = CompanionService(runner=FakeStudio(returncode=206, stderr="Nothing to be sliced"), library=self.library)
        self.addCleanup(svc.close)
        task = self.finished(svc.slice_model(model_path=str(self.stl), wait_s=10))
        self.assertEqual(task["state"], "failed")
        rec = svc.activity()[0]
        self.assertEqual(rec["state"], "failed")
        self.assertIn("code 206", rec["error"])

    def test_it_is_read_only_and_local_only(self):
        self.assertEqual(self.get("/api/state", method="POST")[0], 405)
        self.assertEqual(self.get("/api/state", host="evil.example")[0], 403)
        self.assertEqual(self.get("/api/state", host=f"evil.example:{self.port}")[0], 403)
        # pictures only for files a job was about
        other = self.dir / "secret.stl"
        write_stl_binary(cube_soup(5.0), other)
        self.assertEqual(self.get("/api/preview?file=" + other.as_posix())[0], 404)
        self.assertEqual(self.get("/nope")[0], 404)
        self.assertEqual(self.dash.httpd.server_address[0], "127.0.0.1")

    def test_running_task_is_listed_first(self):
        class Slow(FakeStudio):
            def __call__(self, *a, **k):
                time.sleep(0.4)
                return super().__call__(*a, **k)

        svc = CompanionService(runner=Slow(), library=self.library)
        self.addCleanup(svc.close)
        task = svc.slice_model(model_path=str(self.stl), wait_s=0)
        self.assertEqual(svc.activity()[0]["state"], "running")
        self.finished_svc(svc, task)

    def finished_svc(self, svc, task):
        deadline = time.monotonic() + 10
        while task["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
            task = svc.task_status(task["task_id"])


class TestPreview(unittest.TestCase):
    def test_render_and_png(self):
        soup = cube_soup(10.0)
        v = soup.reshape(-1, 3)
        f = np.arange(len(v)).reshape(-1, 3)
        img = render_mesh(v, f, size=64)
        self.assertEqual(img.shape, (64, 64, 3))
        self.assertLess(img.mean(), 240)          # something was drawn
        png = encode_png(img)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_embedded_picture_is_preferred(self):
        import zipfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.3mf"
            pic = encode_png(np.zeros((4, 4, 3), np.uint8))
            with zipfile.ZipFile(p, "w") as zf:
                zf.writestr("Metadata/plate_1.png", pic)
            self.assertEqual(preview_png(p), pic)


if __name__ == "__main__":
    unittest.main()
