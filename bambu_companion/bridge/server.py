"""ChatGPT-facing HTTP layer — the tool endpoints ChatGPT's Apps/Connector
layer calls, reached through the tunnel/relay described in the memory
bank's "ChatGPT Connectivity" section.

STATUS: skeleton, not yet runnable/tested. This needs `fastapi` +
`uvicorn` installed on the bridge machine (see requirements-bridge.txt)
and, separately, the tunnel/relay component from ChatGPT Connectivity —
neither is available in the environment this project was developed in
(no package-index access, no OpenAI developer setup here), so this file
has not been executed. What IS real and tested: everything it calls into
(model_analyzer.analyze, optimizer.optimize, profiles.temp_profile,
bridge.job_history) — this file is deliberately thin glue on top of
already-verified logic, so bringing it to life on your machine should
mostly be "does FastAPI serve this correctly", not "is the underlying
logic right".

Endpoints map directly to the memory bank's V1 feature list:
- GET  /health                   -> unauthenticated liveness check
- GET  /printer/status           -> read-only printer/AMS status
- POST /model/analyze            -> run the model analyzer on an uploaded file
- POST /optimize                 -> get setting recommendations for a goal
- POST /jobs/{job_id}/approve    -> apply approved changes to a temp profile + slice
- GET  /jobs                     -> job history

No endpoint here starts a print, sends G-code, or changes firmware/safety
settings — see Safety Rules. Every mutating endpoint (approve) requires
an explicit approved-keys list from the caller; nothing is auto-applied.

AUTH: every endpoint except /health requires an `X-API-Key` header
matching the key `api_auth.get_or_create_api_key()` generates on first
run (see that module) — this is what actually controls who can use this
server once it's reachable from the internet through a tunnel; the
tunnel itself is not an access-control boundary.

RUNNING IT: `python -m bambu_companion.bridge.server` starts it on
127.0.0.1:8420 (loopback only — a Cloudflare Tunnel or similar is what
should expose it beyond your own PC, not binding to 0.0.0.0) and prints
the API key you'll paste into your Custom GPT's Action auth config.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ..model_analyzer import analyze
from ..optimizer import optimize, to_dict
from ..profiles.temp_profile import ApprovalError, apply_changes
from ..schemas import PrintContext, PrintGoal
from .api_auth import get_or_create_api_key, matches
from .job_history import JobHistoryStore, JobRecord, new_job_id, now_iso
from .printer_config import load_printer_config
from .printer_status import PrinterConnectionError, PrinterStatus, PrinterStatusClient

try:
    from fastapi import Depends, FastAPI, Header, HTTPException
    from pydantic import BaseModel
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "server.py needs fastapi + pydantic installed on the bridge machine: "
        "pip install -r requirements-bridge.txt"
    ) from exc


class AnalyzeRequest(BaseModel):
    model_path: str


class OptimizeRequest(BaseModel):
    model_path: str
    printer: str = "Bambu Lab A1"
    nozzle_diameter_mm: float = 0.4
    material: str
    goal: PrintGoal
    current_settings: dict[str, Any]
    ams_slot: Optional[str] = None


class ApproveRequest(BaseModel):
    approved_keys: list[str]


def create_app(
    history_path: str | Path = "job_history.json",
    api_key: Optional[str] = None,
    printer_config_path: Optional[Path] = None,
) -> "FastAPI":
    api_key = api_key or get_or_create_api_key()

    def _verify_api_key(x_api_key: str = Header(default="")) -> None:
        if not matches(x_api_key, api_key):
            raise HTTPException(
                status_code=401,
                detail="Missing or invalid X-API-Key header.",
            )

    require_key = Depends(_verify_api_key)

    history = JobHistoryStore(history_path)
    # Populated by /optimize, consumed by /jobs/{id}/approve — an in-memory
    # cache of proposals awaiting approval within this process's lifetime.
    pending_proposals: dict[str, dict[str, Any]] = {}

    # Background MQTT connection to the printer, started once on app
    # startup and kept running for the life of the process — a fresh
    # per-request connection would be slow and fragile, and Bambu's
    # push_status messages are partial deltas (see printer_status.py)
    # that only make sense accumulated over time, not read once.
    printer_state: dict[str, Any] = {"client": None, "latest": None, "error": None}

    def _on_printer_status(status: PrinterStatus) -> None:
        printer_state["latest"] = status

    @asynccontextmanager
    async def _lifespan(_app: "FastAPI") -> AsyncIterator[None]:
        # Startup (runs once, before the app accepts requests).
        cfg = load_printer_config(printer_config_path)
        if not (cfg["host"] and cfg["serial"] and cfg["access_code"]):
            printer_state["error"] = (
                "No printer configured yet — connect once from the desktop GUI (or run "
                "the MQTT setup) so host/serial/access code get saved locally, then "
                "restart the bridge."
            )
        else:
            try:
                client = PrinterStatusClient(
                    host=cfg["host"],
                    serial=cfg["serial"],
                    access_code=cfg["access_code"],
                    on_status=_on_printer_status,
                )
                client.connect()
                client.loop_start()
                printer_state["client"] = client
            except PrinterConnectionError as exc:
                printer_state["error"] = str(exc)

        yield  # server runs here

        # Shutdown (runs once, after the app stops accepting requests).
        client = printer_state.get("client")
        if client is not None:
            client.disconnect()

    app = FastAPI(title="Bambu Companion Bridge", lifespan=_lifespan)

    @app.get("/health")
    def health():
        # Deliberately unauthenticated — a quick way to confirm the
        # server + tunnel are up without needing the API key on hand.
        return {"status": "ok", "service": "bambu-companion-bridge"}

    @app.get("/printer/status", dependencies=[require_key])
    def printer_status():
        latest = printer_state.get("latest")
        if latest is None:
            raise HTTPException(
                status_code=503,
                detail=printer_state.get("error")
                or "Connected, but no status received yet — give it a few seconds.",
            )
        return _printer_status_to_dict(latest)

    @app.post("/model/analyze", dependencies=[require_key])
    def model_analyze(req: AnalyzeRequest):
        try:
            result = analyze(req.model_path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _analysis_to_dict(result)

    @app.post("/optimize", dependencies=[require_key])
    def do_optimize(req: OptimizeRequest):
        analysis = analyze(req.model_path)
        ctx = PrintContext(
            printer=req.printer,
            nozzle_diameter_mm=req.nozzle_diameter_mm,
            material=req.material,
            goal=req.goal,
            current_settings=req.current_settings,
            ams_slot=req.ams_slot,
        )
        result = optimize(analysis, ctx)

        job_id = new_job_id()
        pending_proposals[job_id] = {
            "analysis": analysis,
            "ctx": ctx,
            "result": result,
        }
        history.record_job(
            JobRecord(
                id=job_id,
                model_file=req.model_path,
                started_at=now_iso(),
                printer=req.printer,
                nozzle_diameter_mm=req.nozzle_diameter_mm,
                material=req.material,
                ams_slot=req.ams_slot,
                goal=req.goal.value,
                starting_settings=req.current_settings,
                recommended_changes=to_dict(result)["changes"],
                approved_keys=[],
                applied_settings={},
                geometry_warnings=[w.detail for w in analysis.warnings],
            )
        )

        return {"job_id": job_id, **to_dict(result)}

    @app.post("/jobs/{job_id}/approve", dependencies=[require_key])
    def approve(job_id: str, req: ApproveRequest):
        proposal = pending_proposals.get(job_id)
        if proposal is None:
            raise HTTPException(
                status_code=404,
                detail=f"No pending proposal for job {job_id} (expired or already applied?).",
            )
        try:
            temp_settings = apply_changes(
                proposal["ctx"].current_settings,
                proposal["result"].changes,
                set(req.approved_keys),
            )
        except ApprovalError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        history.update_job(job_id, approved_keys=req.approved_keys, applied_settings=temp_settings)

        # Slicing (bridge.studio_runner) intentionally NOT wired in here yet
        # — it needs a real Bambu Studio install + confirmed CLI flags on
        # this machine (see studio_runner.py's module docstring) before
        # it's safe to call from a live endpoint.
        return {
            "job_id": job_id,
            "applied_settings": temp_settings,
            "next_step": "slicing not yet wired up — see bridge/studio_runner.py",
        }

    @app.get("/jobs", dependencies=[require_key])
    def list_jobs():
        return history.load_jobs()

    return app


def _printer_status_to_dict(status: PrinterStatus) -> dict[str, Any]:
    return {
        "connected": status.connected,
        "print_name": status.print_name,
        "progress_percent": status.progress_percent,
        "current_layer": status.current_layer,
        "total_layers": status.total_layers,
        "remaining_time_min": status.remaining_time_min,
        "nozzle_temp_c": status.nozzle_temp_c,
        "bed_temp_c": status.bed_temp_c,
        "warnings": status.warnings,
        "ams_slots": [
            {
                "slot_index": s.slot_index,
                "filament_type": s.filament_type,
                "color": s.color,
                "humidity": s.humidity,
            }
            for s in status.ams_slots
        ],
        # `raw` deliberately left out here — it's the full accumulated
        # MQTT state dict, useful for debugging but noisy for a caller
        # (ChatGPT included) that just wants the parsed fields above.
    }


def _analysis_to_dict(result) -> dict[str, Any]:
    # Deliberately hand-rolled (not a pydantic model on AnalysisResult
    # itself) so model_analyzer stays framework-free; this is the one
    # place that adapts it to JSON for the HTTP layer.
    return {
        "source_file": result.source_file,
        "bounding_box": {"min": result.bounding_box.min, "max": result.bounding_box.max},
        "bed_fit": {
            "fits": result.bed_fit.fits,
            "build_volume_mm": result.bed_fit.build_volume_mm,
            "model_size_mm": result.bed_fit.model_size_mm,
            "margin_mm": result.bed_fit.margin_mm,
            "notes": result.bed_fit.notes,
        },
        "bed_contact_area_mm2": result.bed_contact_area_mm2,
        "overhangs": vars(result.overhangs) if hasattr(result.overhangs, "__dict__") else result.overhangs.__dict__,
        "bridges": result.bridges.__dict__,
        "thin_walls": {
            "checked_samples": result.thin_walls.checked_samples,
            "thin_sample_count": result.thin_walls.thin_sample_count,
            "min_thickness_mm": result.thin_walls.min_thickness_mm,
        },
        "small_features": [f.__dict__ for f in result.small_features],
        "fit_sensitive_features": [f.__dict__ for f in result.fit_sensitive_features],
        "orientation_candidates": [
            {
                "description": c.description,
                "overhang_area_mm2": c.overhang_area_mm2,
                "bed_contact_area_mm2": c.bed_contact_area_mm2,
                "score": c.score,
            }
            for c in result.orientation_candidates
        ],
        "warnings": [w.__dict__ for w in result.warnings],
        "body_count": result.body_count,
        "is_watertight": result.is_watertight,
        "face_count": result.face_count,
        "vertex_count": result.vertex_count,
    }


def main() -> None:
    """Runs the bridge server directly: `python -m bambu_companion.bridge.server`.
    Binds to loopback only (127.0.0.1) — a Cloudflare Tunnel (or similar)
    is what should make this reachable beyond your own PC; this process
    itself should never listen on 0.0.0.0."""
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "Running the bridge needs uvicorn installed: pip install -r requirements-bridge.txt"
        ) from exc

    api_key = get_or_create_api_key()
    print("=" * 70)
    print("Bambu Companion Bridge")
    print()
    print("API key (put this in your Custom GPT's Action auth config as a")
    print("Bearer/API key, header name X-API-Key):")
    print(f"  {api_key}")
    print()
    print("Listening on http://127.0.0.1:8420 (loopback only).")
    print("=" * 70)

    app = create_app(api_key=api_key)
    uvicorn.run(app, host="127.0.0.1", port=8420)


if __name__ == "__main__":
    main()
