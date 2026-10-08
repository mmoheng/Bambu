"""Tests for bridge/server.py.

This whole module is skipped when fastapi/uvicorn aren't installed —
which is the normal case in this project's dev sandbox (no package-index
access there; see bridge/README.md). They ARE installed on the bridge
machine (`pip install -r requirements-bridge.txt`), so run this file
there for real coverage of the HTTP layer — everything it wraps
(model_analyzer, optimizer, temp_profile) already has its own tests that
don't need fastapi at all.
"""

import tempfile
import unittest
from pathlib import Path

try:
    from fastapi.testclient import TestClient

    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False

from bambu_companion.gui_logic import DEFAULT_CURRENT_SETTINGS

from .mesh_fixtures import cube_soup, write_stl_binary


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi/uvicorn not installed in this environment")
class TestServerAuth(unittest.TestCase):
    def setUp(self):
        from bambu_companion.bridge.server import create_app

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        history_path = Path(self.tmp.name) / "job_history.json"
        # An isolated, guaranteed-empty printer config path — without
        # this, this test would behave differently on a machine that
        # already has a real saved printer_config.json (e.g. the bridge
        # machine itself) than it does here, which defeats the point of
        # a test that's supposed to be about the "not configured" path.
        printer_config_path = Path(self.tmp.name) / "printer_config.json"
        self.api_key = "test-key-for-unit-tests"
        app = create_app(
            history_path=str(history_path),
            api_key=self.api_key,
            printer_config_path=printer_config_path,
        )
        self.client = TestClient(app)

    def test_health_needs_no_auth(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")

    def test_protected_endpoint_without_key_is_rejected(self):
        resp = self.client.get("/jobs")
        self.assertEqual(resp.status_code, 401)

    def test_protected_endpoint_with_wrong_key_is_rejected(self):
        resp = self.client.get("/jobs", headers={"X-API-Key": "wrong-key"})
        self.assertEqual(resp.status_code, 401)

    def test_protected_endpoint_with_correct_key_succeeds(self):
        resp = self.client.get("/jobs", headers={"X-API-Key": self.api_key})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_printer_status_without_config_returns_503_not_501(self):
        # No printer_config.json exists for this test's isolated APPDATA,
        # so startup should record "not configured" rather than crash —
        # and the endpoint should say so with a real status code, not the
        # old hardcoded 501 "not wired up" placeholder.
        resp = self.client.get("/printer/status", headers={"X-API-Key": self.api_key})
        self.assertIn(resp.status_code, (503,))


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi/uvicorn not installed in this environment")
class TestServerAnalyzeAndOptimize(unittest.TestCase):
    def setUp(self):
        from bambu_companion.bridge.server import create_app

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        history_path = Path(self.tmp.name) / "job_history.json"
        printer_config_path = Path(self.tmp.name) / "printer_config.json"
        self.api_key = "test-key-for-unit-tests"
        app = create_app(
            history_path=str(history_path),
            api_key=self.api_key,
            printer_config_path=printer_config_path,
        )
        self.client = TestClient(app)
        self.headers = {"X-API-Key": self.api_key}

        self.stl_path = Path(self.tmp.name) / "cube.stl"
        write_stl_binary(cube_soup(20.0), self.stl_path)

    def test_model_analyze_real_file(self):
        resp = self.client.post(
            "/model/analyze", json={"model_path": str(self.stl_path)}, headers=self.headers
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["bed_fit"]["fits"])

    def test_model_analyze_missing_file_is_404(self):
        resp = self.client.post(
            "/model/analyze", json={"model_path": str(self.tmp.name) + "/nope.stl"}, headers=self.headers
        )
        self.assertEqual(resp.status_code, 404)

    def test_optimize_then_approve_roundtrip(self):
        resp = self.client.post(
            "/optimize",
            json={
                "model_path": str(self.stl_path),
                "material": "PLA",
                "goal": "balanced",
                "current_settings": DEFAULT_CURRENT_SETTINGS,
            },
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        job_id = resp.json()["job_id"]

        approve_resp = self.client.post(
            f"/jobs/{job_id}/approve", json={"approved_keys": []}, headers=self.headers
        )
        self.assertEqual(approve_resp.status_code, 200)

        jobs_resp = self.client.get("/jobs", headers=self.headers)
        self.assertEqual(jobs_resp.status_code, 200)
        self.assertEqual(len(jobs_resp.json()), 1)

    def test_approve_unknown_job_is_404(self):
        resp = self.client.post(
            "/jobs/does-not-exist/approve", json={"approved_keys": []}, headers=self.headers
        )
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
