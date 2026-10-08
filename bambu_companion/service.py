"""The operations Bambu Companion offers, as plain Python — no web
framework, no MCP, no GUI. `mcp_server.py` (the Claude connector) is a
thin wrapper around this; anything else that wants the same behaviour
can call it the same way.

Every method returns JSON-friendly data and raises `ServiceError` (or
one of the lower-level errors listed in `USER_ERRORS`) with a message
meant to be shown to a person.

What it will and won't do, per the project's Safety Rules:

- Reads: model geometry, the settings inside a project file, preset
  folders, printer/AMS status (never the printer's credentials).
- Writes: only NEW files — a project copy with approved settings, or a
  sliced export — plus its own job history and config. Output files are
  created exclusively (the write fails, or picks the next free name, if
  the name is taken), so nothing here replaces an existing file. It
  never edits a saved Bambu Studio preset, and only changes settings
  listed in `profiles/bambu_settings.py`.
- Never: starts a print, sends G-code, or talks to the printer in any
  way other than listening to its status reports. There is no code path
  here that could.
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from .bridge import slice_check, studio_runner
from .bridge.job_history import JobHistoryStore, JobRecord, new_job_id, now_iso
from .bridge.printer_config import load_printer_config
from .bridge.studio_config import config_dir, load_studio_config
from .gui_logic import DEFAULT_CURRENT_SETTINGS
from .model_analyzer import analyze
from .model_analyzer.mesh_io import MeshLoadError
from .optimizer import optimize, to_dict
from .profiles import project_3mf
from .profiles.bambu_settings import SETTINGS
from .profiles.bambu_values import SettingValueError, validate
from .profiles.preset_library import PRESET_TYPES, PresetError, PresetLibrary, discover_roots
from .profiles.project_3mf import ProjectFileError
from .profiles.temp_profile import ApprovalError
from .schemas import AnalysisResult, PrintContext, PrintGoal

MODEL_SUFFIXES = (".stl", ".3mf")
MAX_WAIT_S = 50.0
PRINTER_STALE_AFTER_S = 90.0
A1_PRINTER_MODEL = "Bambu Lab A1"

# Used only when no real settings are available (a bare STL and no
# project file to read them from): the same typical-defaults table the
# desktop GUI pre-fills its form with.
FALLBACK_CURRENT_SETTINGS: dict[str, Any] = dict(DEFAULT_CURRENT_SETTINGS)


class ServiceError(Exception):
    """A request can't be carried out; the message says why and what to
    do instead."""


# Errors whose message is safe and useful to show as-is.
USER_ERRORS = (
    ServiceError,
    ProjectFileError,
    PresetError,
    SettingValueError,
    ApprovalError,
    MeshLoadError,
    studio_runner.StudioRunnerError,
)


def _model_file(path: Any, *, what: str = "model") -> Path:
    if not path or not isinstance(path, str):
        raise ServiceError(f"A {what} file path is required.")
    p = Path(path).expanduser()
    if p.is_dir():
        raise ServiceError(f"{p} is a folder — give the path of the .stl or .3mf file itself.")
    if not p.is_file():
        raise ServiceError(f"File not found: {p}")
    if p.suffix.lower() not in MODEL_SUFFIXES:
        raise ServiceError(f"{p.name}: only .stl and .3mf files are supported.")
    return p.resolve()


def _output_file(path: Any) -> Path:
    """A caller-chosen output path: must be a full path ending in .3mf.
    A relative one would land wherever the connector happened to be
    started from, which is nowhere the user would look."""
    if not isinstance(path, str) or not path.strip():
        raise ServiceError("output_path must be a file path.")
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ServiceError(f"output_path must be a full path (got '{path}').")
    if p.suffix.lower() != ".3mf":
        raise ServiceError("The output file must end in .3mf.")
    return p


def _wait_seconds(value: Any, default: float) -> float:
    """How long a tool call may block before handing back a running
    task. Checked BEFORE any work starts, and capped so the call returns
    well inside the host's own request timeout."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise ServiceError("wait_s must be a number of seconds.")
    return min(max(float(value), 0.0), MAX_WAIT_S)


def analysis_to_dict(result: AnalysisResult) -> dict[str, Any]:
    r3 = lambda values: [round(float(v), 2) for v in values]  # noqa: E731
    oh, br, tw = result.overhangs, result.bridges, result.thin_walls
    return {
        "file": result.source_file,
        "size_mm": r3(result.bounding_box.size),
        "fits_a1_bed": result.bed_fit.fits,
        "bed_fit_notes": list(result.bed_fit.notes),
        "bed_contact_area_mm2": round(result.bed_contact_area_mm2, 1),
        "bodies": result.body_count,
        "watertight": result.is_watertight,
        "triangles": result.face_count,
        "overhangs": {
            "area_mm2": round(oh.overhang_area_mm2, 1),
            "regions": oh.island_count,
            "largest_region_mm2": round(oh.largest_island_area_mm2, 1),
            "flattest_face_deg_from_horizontal": oh.worst_face_tilt_deg,
            "checked_below_deg": oh.threshold_deg,
        },
        "flat_ceilings": {
            "area_mm2": round(br.bridge_area_mm2, 1),
            "longest_span_mm": round(br.longest_span_mm, 1),
        },
        "thin_walls": {
            "thinnest_sampled_mm": None if tw.min_thickness_mm is None else round(tw.min_thickness_mm, 2),
            "thin_samples": tw.thin_sample_count,
            "samples": tw.checked_samples,
        },
        "small_features": [f.description for f in result.small_features][:10],
        "holes_mm": [round(f.diameter_mm, 2) for f in result.fit_sensitive_features][:20],
        "orientation_options": [
            {
                "description": c.description,
                "overhang_area_mm2": round(c.overhang_area_mm2, 1),
                "bed_contact_area_mm2": round(c.bed_contact_area_mm2, 1),
            }
            for c in result.orientation_candidates
        ],
        "warnings": [w.detail for w in result.warnings],
    }


class CompanionService:
    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        runner: Callable[..., dict[str, Any]] | None = None,
        printer_client_factory: Callable[..., Any] | None = None,
        printer_config_path: Path | None = None,
        library: PresetLibrary | None = None,
    ):
        self.data_dir = Path(data_dir) if data_dir else config_dir()
        self._runner = runner  # test seam: replaces studio_runner.run_cli
        self._printer_client_factory = printer_client_factory
        self._printer_config_path = printer_config_path
        self._library = library
        self._history: JobHistoryStore | None = None
        self._proposals: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, dict[str, Any]] = {}
        self._printer: dict[str, Any] = {"client": None, "latest": None, "error": None}
        self._reserved_outputs: set[Path] = set()
        self._exe: Path | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @property
    def history(self) -> JobHistoryStore:
        if self._history is None:
            self._history = JobHistoryStore(self.data_dir / "job_history.json")
        return self._history

    def _studio_executable(self) -> Path:
        # Cached once found: discovery probes every drive letter.
        if self._exe is None or not self._exe.is_file():
            self._exe = studio_runner.find_bambu_studio_executable()
        return self._exe

    def _reserve_output(self, explicit: str | None, directory: Path, stem: str) -> Path:
        """Chooses the output file and reserves the name for this call,
        so two calls running at once can't both pick it. Reservation is
        in-memory bookkeeping; the write itself is what guarantees
        nothing is replaced (exclusive create)."""
        with self._lock:
            if explicit:
                path = _output_file(explicit)
                if path.exists() or path in self._reserved_outputs:
                    raise ServiceError(f"{path} already exists. Refusing to overwrite it.")
            else:
                path = directory / f"{stem}.3mf"
                n = 2
                while path.exists() or path in self._reserved_outputs:
                    path = directory / f"{stem}_{n}.3mf"
                    n += 1
            self._reserved_outputs.add(path)
            return path

    def _release_output(self, path: Path) -> None:
        with self._lock:
            self._reserved_outputs.discard(path)

    def _preset_library(self) -> PresetLibrary:
        if self._library is None:
            try:
                exe: Path | None = self._studio_executable()
            except studio_runner.StudioRunnerError:
                exe = None
            self._library = PresetLibrary(discover_roots(exe))
        return self._library

    def _proposal(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            proposal = self._proposals.get(job_id)
        if proposal is None:
            raise ServiceError(
                f"No pending recommendation with job_id '{job_id}'. Recommendations only live "
                "while the connector is running — call recommend_settings again."
            )
        return proposal

    def _resolve_changes(
        self,
        job_id: str | None,
        approved_keys: list[str] | None,
        changes: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Turns "the approved keys of job X" plus any explicit
        key -> value changes into one validated dict."""
        if approved_keys is not None and not (
            isinstance(approved_keys, list) and all(isinstance(k, str) for k in approved_keys)
        ):
            raise ServiceError("approved_keys must be a list of setting keys.")
        if changes is not None and not isinstance(changes, Mapping):
            raise ServiceError('changes must be an object like {"wall_loops": 4}.')
        if job_id is not None and not isinstance(job_id, str):
            raise ServiceError("job_id must be the id returned by recommend_settings.")
        resolved: dict[str, Any] = {}
        proposal = self._proposal(job_id) if job_id else None
        if approved_keys:
            if proposal is None:
                raise ServiceError("approved_keys needs the job_id of the recommendation they came from.")
            proposed = {c.key: c.recommended_value for c in proposal["result"].changes}
            unknown = sorted(set(approved_keys) - set(proposed))
            if unknown:
                raise ApprovalError(
                    f"Approved key(s) {unknown} were not part of that recommendation "
                    f"({sorted(proposed)}). Refusing to apply — the approval is out of sync "
                    "with what was shown."
                )
            resolved.update({k: proposed[k] for k in approved_keys})
        for key, value in (changes or {}).items():
            resolved[key] = value
        return {k: validate(k, v) for k, v in resolved.items()}, proposal

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------

    def _plate_selection(self, model: Path, plate: Any) -> tuple[list[str] | None, list[str]]:
        """For a multi-plate project, the object ids on the chosen plate
        (default: plate 1) and a note saying so. Bambu Studio lays its
        plates out side by side in one coordinate space, so loading all
        of them reads as a single object hundreds of millimetres wide
        that "doesn't fit the bed"."""
        if plate is not None and (isinstance(plate, bool) or not isinstance(plate, int) or plate < 1):
            raise ServiceError("plate must be a plate number, starting at 1.")
        if model.suffix.lower() != ".3mf":
            return None, []
        plates = {n: ids for n, ids in project_3mf.plate_object_ids(model).items() if ids}
        if len(plates) <= 1 and plate in (None, 1):
            return None, []
        chosen = plate or min(plates)
        if chosen not in plates:
            raise ServiceError(f"{model.name} has no objects on plate {chosen} (plates: {sorted(plates)}).")
        note = (
            f"{model.name} has {len(plates)} plates; this is plate {chosen} only. "
            "Pass plate=<n> for another."
        )
        return plates[chosen], [note] if len(plates) > 1 else []

    def analyze_model(self, model_path: str, plate: int | None = None) -> dict[str, Any]:
        model = _model_file(model_path)
        object_ids, notes = self._plate_selection(model, plate)
        result = analysis_to_dict(analyze(model, object_ids=object_ids))
        result["notes"] = notes
        return result

    def read_settings(self, path: str) -> dict[str, Any]:
        """Settings (and slice result, if any) stored in a Bambu Studio 3MF."""
        return project_3mf.describe_project(_model_file(path, what="project"))

    def recommend(
        self,
        model_path: str,
        goal: str,
        *,
        material: str | None = None,
        nozzle_diameter_mm: float | None = None,
        current_settings: Mapping[str, Any] | None = None,
        settings_from: str | None = None,
        plate: int | None = None,
    ) -> dict[str, Any]:
        model = _model_file(model_path)
        try:
            print_goal = PrintGoal(goal)
        except ValueError:
            raise ServiceError(f"Unknown goal '{goal}'. Choose one of: {[g.value for g in PrintGoal]}.") from None
        if current_settings is not None and not isinstance(current_settings, Mapping):
            raise ServiceError('current_settings must be an object like {"wall_loops": 2}.')
        if material is not None and not isinstance(material, str):
            raise ServiceError("material must be text, e.g. PLA or PETG.")
        if nozzle_diameter_mm is not None and (
            isinstance(nozzle_diameter_mm, bool)
            or not isinstance(nozzle_diameter_mm, (int, float))
            or not 0.1 <= nozzle_diameter_mm <= 1.2
        ):
            raise ServiceError("nozzle_diameter_mm must be a number such as 0.4.")

        object_ids, notes = self._plate_selection(model, plate)
        settings_file: Path | None = None
        if settings_from:
            settings_file = _model_file(settings_from, what="settings_from")
        elif model.suffix.lower() == ".3mf" and project_3mf.has_project_settings(model):
            settings_file = model

        detected_material = None
        detected_nozzle = None
        if settings_file is not None:
            info = project_3mf.describe_project(settings_file)
            current = dict(info["settings"])
            settings_source = f"read from {settings_file.name} ({info.get('process_preset')})"
            detected_material = (info.get("filament_types") or [None])[0]
            detected_nozzle = info.get("nozzle_diameter_mm")
            if info.get("printer_model") and info["printer_model"] != A1_PRINTER_MODEL:
                notes.append(
                    f"{settings_file.name} is set up for '{info['printer_model']}', not the "
                    f"{A1_PRINTER_MODEL}. Its settings were used as the starting point, but pick "
                    "the A1 in Bambu Studio before slicing."
                )
        else:
            current = dict(FALLBACK_CURRENT_SETTINGS)
            settings_source = (
                "built-in Bambu Studio A1 defaults — NOT read from your profile. Pass "
                "settings_from=<a project .3mf saved from Bambu Studio> to use real ones."
            )

        for key, value in (current_settings or {}).items():
            current[key] = validate(key, value)

        material = material or detected_material or "PLA"
        nozzle = float(nozzle_diameter_mm or detected_nozzle or 0.4)
        wall_loops = current.get("wall_loops")
        analysis = analyze(
            model,
            nozzle_diameter_mm=nozzle,
            current_wall_loops=wall_loops if isinstance(wall_loops, int) and wall_loops > 0 else 2,
            object_ids=object_ids,
        )
        ctx = PrintContext(
            printer=A1_PRINTER_MODEL,
            nozzle_diameter_mm=nozzle,
            material=material,
            goal=print_goal,
            current_settings=current,
        )
        result = optimize(analysis, ctx)
        result_dict = to_dict(result)

        job_id = new_job_id()
        with self._lock:
            self._proposals[job_id] = {
                "model": model,
                "settings_file": settings_file,
                "ctx": ctx,
                "result": result,
            }
        try:
            self.history.record_job(
                JobRecord(
                    id=job_id,
                    model_file=str(model),
                    started_at=now_iso(),
                    printer=A1_PRINTER_MODEL,
                    nozzle_diameter_mm=nozzle,
                    material=material,
                    ams_slot=None,
                    goal=print_goal.value,
                    starting_settings=current,
                    recommended_changes=result_dict["changes"],
                    approved_keys=[],
                    applied_settings={},
                    geometry_warnings=[w.detail for w in analysis.warnings],
                )
            )
        except (ValueError, OSError) as exc:
            # The history is a log. A damaged log file must not stop
            # the recommendation itself from being returned.
            notes.append(f"This job could not be written to the job history ({exc}).")
        return {
            "job_id": job_id,
            "model": str(model),
            "goal": print_goal.value,
            "material": material,
            "nozzle_diameter_mm": nozzle,
            "current_settings_source": settings_source,
            "notes": notes,
            "changes": result_dict["changes"],
            "flagged_for_review": result_dict["uncertainties"],
            "analysis": analysis_to_dict(analysis),
            "next_step": (
                "Show these to the user and wait for their approval. Then call apply_settings "
                "or slice_model with this job_id and the approved keys."
            ),
        }

    def list_settings(self) -> list[dict[str, Any]]:
        """The settings this project is allowed to change."""
        return [
            {
                "key": s.key,
                "label": s.label,
                "category": s.category,
                "kind": s.kind,
                **({"choices": list(s.choices)} if s.choices else {}),
                **({"min": s.minimum} if s.minimum is not None else {}),
                **({"max": s.maximum} if s.maximum is not None else {}),
            }
            for s in SETTINGS.values()
        ]

    def studio_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {"config": load_studio_config()}
        try:
            info["executable"] = str(self._studio_executable())
        except studio_runner.StudioRunnerError as exc:
            info["executable"] = None
            info["executable_error"] = str(exc)
        info["presets"] = self._preset_library().describe()
        info["headless_slicing"] = (
            f"confirmed working here with strategy '{info['config']['working_strategy']}'"
            if info["config"].get("working_strategy")
            else "NOT yet confirmed on this machine — run slice_check first"
        )
        return info

    def list_presets(self, preset_type: str, contains: str | None = None) -> dict[str, Any]:
        if preset_type not in PRESET_TYPES:
            raise ServiceError(f"preset_type must be one of {list(PRESET_TYPES)}.")
        names = self._preset_library().list_names(preset_type, contains)
        return {"preset_type": preset_type, "count": len(names), "names": names[:200]}

    def job_history(self, limit: int = 10) -> list[dict[str, Any]]:
        jobs = self.history.load_jobs()[-max(1, int(limit)) :]
        keep = (
            "id", "model_file", "started_at", "material", "goal", "approved_keys",
            "applied_settings", "slice_result", "output_file", "errors", "outcome_note",
        )  # fmt: skip
        return [{k: j.get(k) for k in keep} for j in reversed(jobs)]

    def record_outcome(self, job_id: str, note: str) -> dict[str, Any]:
        """Stores how a print actually turned out, in the user's words."""
        if not note or not note.strip():
            raise ServiceError("An outcome note is required.")
        try:
            self.history.update_job(job_id, outcome_note=note.strip())
        except KeyError:
            raise ServiceError(f"No job with id '{job_id}' in the job history.") from None
        return {"job_id": job_id, "outcome_note": note.strip()}

    # ------------------------------------------------------------------
    # writing new files
    # ------------------------------------------------------------------

    def _project_source(self, proposal: dict[str, Any] | None, explicit: str | None) -> Path:
        # Only ever the model's own project file. A recommendation made
        # for an STL using settings_from=<some other project> must not
        # silently write a copy of that *other* model.
        if explicit:
            source = _model_file(explicit, what="source_project")
        elif proposal is not None and proposal["model"].suffix.lower() == ".3mf":
            source = proposal["model"]
        else:
            raise ServiceError(
                "Settings can only be written into a Bambu Studio project file. Open the model "
                "in Bambu Studio, use File > Save Project, and pass that .3mf as source_project "
                "(or use slice_model, which can slice a bare STL from presets)."
            )
        if not project_3mf.has_project_settings(source):
            raise ProjectFileError(
                f"{source.name} has no embedded Bambu Studio settings — it is a geometry-only "
                "3MF. Open it in Bambu Studio and use File > Save Project first."
            )
        return source

    def apply_settings(
        self,
        *,
        job_id: str | None = None,
        approved_keys: list[str] | None = None,
        changes: Mapping[str, Any] | None = None,
        source_project: str | None = None,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        """Writes a NEW project 3MF: a copy of the source with only the
        approved settings changed. Open it in Bambu Studio to slice."""
        typed, proposal = self._resolve_changes(job_id, approved_keys, changes)
        if not typed:
            raise ServiceError("Nothing to apply: give approved_keys (with job_id) and/or changes.")
        source = self._project_source(proposal, source_project)
        destination = self._reserve_output(output_path, source.parent, f"{source.stem}_companion")
        try:
            result = project_3mf.write_project_copy(source, destination, typed)
        finally:
            self._release_output(destination)
        if job_id:
            self._update_history(job_id, approved_keys=sorted(typed), applied_settings=typed, output_file=result["written"])
        result["source_untouched"] = str(source)
        result["next_step"] = (
            "Open the written file in Bambu Studio and slice it there, or call slice_model on it. "
            "Nothing has been sent to the printer."
        )
        return result

    def _update_history(self, job_id: str, **fields: Any) -> None:
        try:
            self.history.update_job(job_id, **fields)
        except (KeyError, ValueError, OSError):
            pass  # history is a log, never a reason to fail the job itself

    # ------------------------------------------------------------------
    # background tasks (slicing can outlast one tool call)
    # ------------------------------------------------------------------

    def _start_task(self, kind: str, fn: Callable[[], dict[str, Any]], wait_s: float) -> dict[str, Any]:
        """Runs `fn` on a worker thread and waits up to `wait_s` for it.
        `wait_s` must already be validated (`_wait_seconds`) — nothing
        may fail between starting the work and returning its task id,
        or the caller would lose track of a job that is still running."""
        task_id = uuid.uuid4().hex[:12]
        task: dict[str, Any] = {"task_id": task_id, "kind": kind, "state": "running", "started": time.monotonic()}

        def work() -> None:
            try:
                outcome = {"state": "done", "result": fn()}
            except USER_ERRORS as exc:
                outcome = {"state": "failed", "error": str(exc)}
                details = getattr(exc, "details", None)
                if details:
                    outcome["details"] = {
                        k: details.get(k)
                        for k in ("command", "returncode", "result_json", "stderr_tail", "stdout_tail")
                        if details.get(k) is not None
                    }
            except Exception as exc:  # noqa: BLE001 - report, don't kill the worker silently
                outcome = {"state": "failed", "error": f"Unexpected {type(exc).__name__}: {exc}"}
            with self._lock:
                task.update(outcome)
                task["finished"] = time.monotonic()

        thread = threading.Thread(target=work, name=f"companion-{kind}-{task_id}", daemon=True)
        with self._lock:
            self._tasks[task_id] = task
        thread.start()
        thread.join(timeout=wait_s)
        return self.task_status(task_id)

    def task_status(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise ServiceError(f"No task with id '{task_id}' (tasks only live while the connector runs).")
            view = {k: v for k, v in task.items() if k not in ("started", "finished")}
            view["elapsed_s"] = round(task.get("finished", time.monotonic()) - task["started"], 1)
        if view["state"] == "running":
            view["next_step"] = "Still working. Call task_status with this task_id again in a little while."
        return view

    def slice_model(
        self,
        *,
        model_path: str | None = None,
        job_id: str | None = None,
        approved_keys: list[str] | None = None,
        changes: Mapping[str, Any] | None = None,
        output_path: str | None = None,
        machine: str | None = None,
        process: str | None = None,
        filament: str | None = None,
        wait_s: float | None = None,
    ) -> dict[str, Any]:
        """Slices with Bambu Studio's command line and reports time,
        filament and support use. Produces a new file; sends nothing to
        the printer."""
        wait = _wait_seconds(wait_s, 40.0)
        for label, name in (("machine", machine), ("process", process), ("filament", filament)):
            if name is not None and not isinstance(name, str):
                raise ServiceError(f"{label} must be a preset name.")
        typed, proposal = self._resolve_changes(job_id, approved_keys, changes)
        if model_path:
            model = _model_file(model_path)
        elif proposal is not None:
            model = proposal["model"]
        else:
            raise ServiceError("Give model_path, or the job_id of a recommendation.")

        config = load_studio_config()
        working = config.get("working_strategy") or ""
        confirmed_route, *modifier_list = working.split("+")
        modifiers = set(modifier_list)
        is_project = model.suffix.lower() == ".3mf" and project_3mf.has_project_settings(model)
        use_presets = bool(machine or process or filament) or not is_project
        route = studio_runner.STRATEGY_PRESETS if use_presets else studio_runner.STRATEGY_PROJECT
        runner = self._runner
        exe = self._studio_executable()

        presets: dict[str, Any] = {}
        library = None
        if use_presets:
            library = self._preset_library()
            fallback = {
                "machine": config["default_machine"],
                "process": config["default_process"],
                "filament": config["default_filament"],
            }
            if is_project:
                # Naming one preset must not silently swap the project's
                # other two for the defaults: whatever isn't named comes
                # from the project itself.
                info = project_3mf.describe_project(model)
                own_filaments = info.get("filament_presets") or []
                fallback = {
                    "machine": info.get("printer") or fallback["machine"],
                    "process": info.get("process_preset") or fallback["process"],
                    "filament": own_filaments[0] if own_filaments else fallback["filament"],
                }
            presets = {
                "machine": machine or fallback["machine"],
                "process": process or fallback["process"],
                "filaments": [filament or fallback["filament"]],
            }

        destination = self._reserve_output(output_path, model.parent, f"{model.stem}_companion_sliced")

        def run() -> dict[str, Any]:
            if not use_presets:
                return studio_runner.slice_project(
                    model,
                    destination,
                    changes=typed,
                    executable_path=exe,
                    use_outputdir="fullpath" not in modifiers,
                    runner=runner,
                )
            return studio_runner.slice_with_presets(
                model,
                destination,
                library=library,
                changes=typed,
                bed_type=config["default_bed_type"] if "bed" in modifiers else None,
                executable_path=exe,
                orient=not is_project,
                arrange=not is_project,
                use_outputdir="fullpath" not in modifiers,
                runner=runner,
                **presets,
            )

        def job() -> dict[str, Any]:
            try:
                result = run()
            except USER_ERRORS as exc:
                if job_id:
                    self._update_history(job_id, slice_result="failed", errors=[str(exc).splitlines()[0]])
                raise
            finally:
                self._release_output(destination)
            verified = bool(result["settings_verified"])
            summary = {
                "output_path": result["output_path"],
                "strategy": result["strategy"],
                "route_confirmed_by_slice_check": confirmed_route == route,
                "presets": result.get("presets"),
                "settings_requested": typed,
                "settings_verified_in_output": verified,
                "settings_not_found_in_output": result["unverified_settings"],
                "plates": result["slice_result"],
                "next_step": (
                    "Open the output in Bambu Studio to preview it and send it to the printer "
                    "yourself. Nothing has been sent to the printer."
                ),
            }
            if result.get("output_renamed"):
                summary["output_renamed"] = result["output_renamed"]
            if not verified:
                summary["warning"] = (
                    "Bambu Studio sliced the file but did NOT keep every requested setting — "
                    "see settings_not_found_in_output. Do not treat this as the approved job."
                )
            if confirmed_route != route:
                summary["note"] = (
                    f"This used the '{route}' way of calling Bambu Studio, which slice_check has "
                    "not confirmed on this PC"
                    + (f" (it confirmed '{confirmed_route}')" if confirmed_route else "")
                    + ". Compare the figures with Bambu Studio's own before relying on them."
                )
            if job_id:
                # The history must be able to tell a verified job from
                # one whose settings the slicer dropped.
                self._update_history(
                    job_id,
                    approved_keys=sorted(typed),
                    applied_settings=typed if verified else {},
                    slice_result="success" if verified else "sliced_but_settings_unverified",
                    output_file=result["output_path"],
                    slice_warnings=sorted({w for p in result["slice_result"] for w in p.get("warnings", [])}),
                    errors=[] if verified else [f"settings not found in output: {result['unverified_settings']}"],
                )
            return summary

        return self._start_task("slice", job, wait)

    def slice_check(self, model_path: str | None = None, wait_s: float | None = None) -> dict[str, Any]:
        """Runs the headless-slicing diagnostic (see bridge/slice_check.py)."""
        wait = _wait_seconds(wait_s, 40.0)
        model = str(_model_file(model_path)) if model_path else None
        runner = self._runner
        library = self._library  # None in normal use: the check discovers it from the install

        def job() -> dict[str, Any]:
            report = slice_check.run_slice_check(
                model, runner=runner, report_dir=self.data_dir, library=library
            )
            return {
                "working_strategy": report.get("working_strategy"),
                "summary": report["summary"],
                "report_path": report.get("report_path"),
            }

        return self._start_task("slice_check", job, wait)

    # ------------------------------------------------------------------
    # printer status (read-only)
    # ------------------------------------------------------------------

    def printer_status(self, wait_s: float | None = None) -> dict[str, Any]:
        """Latest printer + AMS status. Listens only; never publishes.
        The connection is opened on first use and kept, because the
        printer sends partial updates that only make sense accumulated.
        """
        from .bridge import printer_status as ps  # local: optional paho dependency

        wait_s = _wait_seconds(wait_s, 8.0)
        with self._lock:
            client = self._printer["client"]
        if client is None:
            cfg = load_printer_config(self._printer_config_path)
            if not (cfg["host"] and cfg["serial"] and cfg["access_code"]):
                raise ServiceError(
                    "No printer is configured on this PC yet. Run "
                    "`python -m bambu_companion.bridge.printer_setup` once (it asks for the "
                    "printer's IP address, serial number and LAN access code and stores them "
                    "locally — they are never sent anywhere else)."
                )
            factory = self._printer_client_factory or ps.PrinterStatusClient

            def on_status(status: Any) -> None:
                with self._lock:
                    self._printer["latest"] = status
                    self._printer["updated"] = time.monotonic()

            try:
                client = factory(
                    host=cfg["host"], serial=cfg["serial"], access_code=cfg["access_code"], on_status=on_status
                )
                client.connect()
                client.loop_start()
            except ps.PrinterConnectionError as exc:
                # A fixed message: whatever the lower layer put in its
                # error (it once included the printer's address) does
                # not leave this PC through a tool result.
                missing = "paho-mqtt is not installed" in str(exc)
                raise ServiceError(
                    "Printer status needs the paho-mqtt package on this PC: pip install paho-mqtt"
                    if missing
                    else "Could not connect to the printer at its saved address. Is it switched "
                    "on, on this network, and in LAN/Developer Mode?"
                ) from exc
            with self._lock:
                self._printer["client"] = client

        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            with self._lock:
                if self._printer["latest"] is not None:
                    break
            time.sleep(0.1)

        with self._lock:
            latest = self._printer["latest"]
            updated = self._printer.get("updated")
        if latest is None:
            return {
                "connected": False,
                "message": (
                    f"Connected to the printer's address but received no status in {wait_s:.0f}s. "
                    "Is it switched on, on this network, and in LAN/Developer Mode? "
                    "(A wrong serial number or access code looks exactly like this.)"
                ),
            }
        result = ps.status_to_dict(latest)
        age = time.monotonic() - updated if updated else None
        result["seconds_since_last_update"] = round(age, 1) if age is not None else None
        if age is not None and age > PRINTER_STALE_AFTER_S:
            # The printer reports every few seconds while it is on. Old
            # data is shown, but not as a live connection.
            result["connected"] = False
            result["message"] = (
                f"No report from the printer for {age:.0f}s — these are its last known values. "
                "It may be switched off or off the network."
            )
        return result

    def close(self) -> None:
        with self._lock:
            client, self._printer["client"] = self._printer["client"], None
        if client is not None:
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001 - shutting down
                pass
