"""Job history storage — memory bank section "Job History".

This is real, working, dependency-free (stdlib only) storage: a single
JSON file acting as an append-only log, one record per optimization job.
No printer or Bambu Studio needed to use or test this module.

Not a database because the expected volume (one record per print job) is
tiny; if this ever needs querying at scale, swap the storage backend
without changing the public functions below (`record_job`, `load_jobs`,
`find_reference_jobs`).
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


@dataclass
class JobRecord:
    id: str
    model_file: str
    started_at: str
    printer: str
    nozzle_diameter_mm: float
    material: str
    ams_slot: Optional[str]
    goal: str
    starting_settings: dict[str, Any]
    recommended_changes: list[dict[str, Any]]
    approved_keys: list[str]
    applied_settings: dict[str, Any]
    geometry_warnings: list[str] = field(default_factory=list)
    slice_warnings: list[str] = field(default_factory=list)
    slice_result: Optional[str] = None  # "success" | "failed" | None (not yet sliced)
    output_file: Optional[str] = None
    errors: list[str] = field(default_factory=list)
    outcome_note: Optional[str] = None  # e.g. "came out perfectly" — user-supplied later


class JobHistoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The connector handles tool calls on separate threads and a
        # slice finishes on yet another; every read-modify-write below
        # holds this lock.
        self._lock = threading.RLock()
        if not self.path.exists():
            self.path.write_text("[]", encoding="utf-8")

    def _read_all(self) -> list[dict[str, Any]]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Job history file at {self.path} is corrupted (invalid JSON): {exc}"
            ) from exc

    def _write_all(self, records: list[dict[str, Any]]) -> None:
        # Write a sibling temp file, then swap it in: a crash or a
        # concurrent reader never sees a half-written history.
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(records, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def record_job(self, job: JobRecord) -> None:
        with self._lock:
            records = self._read_all()
            records.append(asdict(job))
            self._write_all(records)

    def update_job(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            records = self._read_all()
            for r in records:
                if r["id"] == job_id:
                    r.update(fields)
                    self._write_all(records)
                    return
        raise KeyError(f"No job with id {job_id!r} in {self.path}")

    def load_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._read_all()

    def find_reference_jobs(self, model_file: Optional[str] = None) -> list[dict[str, Any]]:
        """Jobs the user marked as good outcomes — the memory bank's
        "a successful job can later become a reference" idea. Filters to
        slice_result == 'success' with a non-empty outcome_note, optionally
        narrowed to a specific model file name.
        """
        jobs = self._read_all()
        results = [
            j
            for j in jobs
            if j.get("slice_result") == "success" and j.get("outcome_note")
        ]
        if model_file is not None:
            results = [j for j in results if j.get("model_file") == model_file]
        return results


def new_job_id() -> str:
    return uuid.uuid4().hex


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
