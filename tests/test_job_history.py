import tempfile
import unittest
from pathlib import Path

from bambu_companion.bridge.job_history import JobHistoryStore, JobRecord, new_job_id, now_iso


def make_job(**overrides) -> JobRecord:
    defaults = dict(
        id=new_job_id(),
        model_file="card_divider.stl",
        started_at=now_iso(),
        printer="Bambu Lab A1",
        nozzle_diameter_mm=0.4,
        material="PETG",
        ams_slot=None,
        goal="dimensional_accuracy",
        starting_settings={"wall_loops": 2},
        recommended_changes=[],
        approved_keys=[],
        applied_settings={},
    )
    defaults.update(overrides)
    return JobRecord(**defaults)


class TestJobHistoryStore(unittest.TestCase):
    def test_record_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobHistoryStore(Path(tmp) / "history.json")
            job = make_job()
            store.record_job(job)

            loaded = store.load_jobs()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["id"], job.id)
            self.assertEqual(loaded[0]["model_file"], "card_divider.stl")

    def test_update_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobHistoryStore(Path(tmp) / "history.json")
            job = make_job()
            store.record_job(job)

            store.update_job(job.id, slice_result="success", outcome_note="came out perfectly")

            loaded = store.load_jobs()[0]
            self.assertEqual(loaded["slice_result"], "success")
            self.assertEqual(loaded["outcome_note"], "came out perfectly")

    def test_update_unknown_job_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobHistoryStore(Path(tmp) / "history.json")
            with self.assertRaises(KeyError):
                store.update_job("nonexistent", slice_result="success")

    def test_find_reference_jobs_only_returns_successful_with_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobHistoryStore(Path(tmp) / "history.json")

            good = make_job(model_file="a.stl")
            store.record_job(good)
            store.update_job(good.id, slice_result="success", outcome_note="perfect")

            bad = make_job(model_file="b.stl")
            store.record_job(bad)
            store.update_job(bad.id, slice_result="failed")

            no_note = make_job(model_file="c.stl")
            store.record_job(no_note)
            store.update_job(no_note.id, slice_result="success")

            refs = store.find_reference_jobs()
            self.assertEqual([r["model_file"] for r in refs], ["a.stl"])

    def test_persists_across_store_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            JobHistoryStore(path).record_job(make_job())
            second = JobHistoryStore(path)
            self.assertEqual(len(second.load_jobs()), 1)


if __name__ == "__main__":
    unittest.main()
