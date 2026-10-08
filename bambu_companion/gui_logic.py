"""Pure logic behind the desktop GUI (gui.py), kept separate and free of
any Tkinter import so it can be unit tested without a display — this
project's dev sandbox has no display server, so gui.py itself has only
been syntax-checked, not run; everything importable from here has real
tests in tests/test_gui_logic.py.

The GUI's job: pick a model file, pick a goal/material, let the user
confirm what their CURRENT Bambu Studio settings actually are (we can't
read Bambu Studio's live profile from here), run the same
analyzer+optimizer as the CLI, and let the user check off which
recommended changes to "approve" — mirroring the memory bank's Approval
Workflow. It does NOT write into Bambu Studio automatically (see
bridge/studio_runner.py's status) — it exports the approved values so
you can enter them into Bambu Studio's Process panel yourself, or, once
studio_runner is verified against a real install, feed them to it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model_analyzer import analyze
from .model_analyzer.mesh_io import MeshLoadError
from .optimizer import optimize
from .profiles.temp_profile import apply_changes
from .schemas import AnalysisResult, OptimizationResult, PrintContext, PrintGoal

# Typical Bambu Studio defaults for a 0.4mm nozzle / PLA system preset.
# These are a STARTING POINT for the "current settings" fields, not a
# read of your actual profile (this tool has no way to read Bambu
# Studio's live config) — the GUI pre-fills them so there's something
# sensible to edit rather than a blank form, but you should check them
# against your real process panel before trusting the diff.
DEFAULT_CURRENT_SETTINGS: dict[str, Any] = {
    "wall_loops": 2,
    "layer_height": 0.2,
    "sparse_infill_density": 15,
    "sparse_infill_pattern": "grid",
    "top_shell_layers": 4,
    "bottom_shell_layers": 4,
    "outer_wall_speed": 200,
    "brim_width": 0,
    "enable_support": False,
    "support_threshold_angle": 30,
    "elefant_foot_compensation": 0.0,
    "xy_hole_compensation": 0.0,
}

GOAL_LABELS: dict[PrintGoal, str] = {
    PrintGoal.VISUAL_QUALITY: "Visual quality",
    PrintGoal.STRENGTH: "Strength",
    PrintGoal.FAST_PRINT: "Fast print",
    PrintGoal.BALANCED: "Balanced",
    PrintGoal.DIMENSIONAL_ACCURACY: "Dimensional accuracy / fit",
}

MATERIALS = ["PLA", "PETG"]


class AnalysisError(RuntimeError):
    """Wraps any failure from loading/analyzing the model into one
    message the GUI can just show in a label, instead of a raw
    traceback."""


@dataclass
class AnalysisRun:
    model_path: str
    analysis: AnalysisResult
    optimization: OptimizationResult
    context: PrintContext


def run_analysis(
    model_path: str,
    *,
    goal: PrintGoal,
    material: str,
    nozzle_diameter_mm: float,
    current_settings: dict[str, Any],
) -> AnalysisRun:
    path = Path(model_path)
    if not path.exists():
        raise AnalysisError(f"File not found: {model_path}")
    if path.is_dir():
        raise AnalysisError(
            f"{model_path} is a folder, not a file — pick the .stl or .3mf file itself."
        )

    try:
        analysis = analyze(path, nozzle_diameter_mm=nozzle_diameter_mm)
    except MeshLoadError as exc:
        raise AnalysisError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - surface anything unexpected to the GUI, not a crash
        raise AnalysisError(f"Couldn't analyze this file: {exc}") from exc

    ctx = PrintContext(
        printer="Bambu Lab A1",
        nozzle_diameter_mm=nozzle_diameter_mm,
        material=material,
        goal=goal,
        current_settings=dict(current_settings),
    )
    result = optimize(analysis, ctx)
    return AnalysisRun(model_path=str(path), analysis=analysis, optimization=result, context=ctx)


def change_rows(run: AnalysisRun) -> list[dict[str, Any]]:
    """One row per recommended change, in the shape the GUI's table
    widget wants. `approved` defaults to True — the GUI is expected to
    let the user uncheck ones they don't want, matching the "approve
    before applying" workflow rather than an opt-in one."""
    return [
        {
            "key": c.key,
            "label": c.label,
            "current": c.current_value,
            "recommended": c.recommended_value,
            "reason": c.reason,
            "approved": True,
        }
        for c in run.optimization.changes
    ]


def summary_lines(run: AnalysisRun) -> list[str]:
    """Top-of-results banner lines: bed fit and any geometry warnings."""
    lines = []
    bed_fit = run.analysis.bed_fit
    if not bed_fit.fits:
        lines.append(f"⚠ Does not fit the A1 build volume ({bed_fit.model_size_mm} vs {bed_fit.build_volume_mm}).")
        lines.extend(f"  {note}" for note in bed_fit.notes)
    for w in run.analysis.warnings:
        lines.append(f"⚠ {w.detail}")
    if not lines:
        lines.append("✓ Fits the A1 build volume; no geometry warnings.")
    return lines


def build_export_settings(run: AnalysisRun, approved_keys: set[str]) -> dict[str, Any]:
    """The merged temp-profile settings for just the approved changes —
    same guarantee as the CLI/optimizer: base settings untouched, only
    approved keys change. This is what the "Export" button writes out.
    """
    return apply_changes(run.context.current_settings, run.optimization.changes, approved_keys)


def load_full_profile(path: str | Path) -> dict[str, Any]:
    """Reads a real Bambu Studio process-settings JSON you exported from
    Bambu Studio itself (Process panel -> right-click your preset ->
    Export) and returns it as a plain dict, completely unmodified —
    including whatever metadata fields Bambu Studio itself wrote
    ("name", "inherits", "from", "instantiation", "version", etc.) that
    this project doesn't otherwise know about.

    This is the base for a FULL-profile export (see
    `build_full_export_profile`): loading your real ~150-key profile
    here means the export can hand back a complete, directly-importable
    Bambu Studio config with just the approved changes overlaid, instead
    of only ever containing the dozen or so keys this project's
    optimizer specifically reasons about.
    """
    path = Path(path)
    if not path.exists():
        raise AnalysisError(f"File not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise AnalysisError(f"Couldn't read that settings file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AnalysisError(f"That file isn't valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AnalysisError(
            "That file doesn't look like a Bambu Studio settings export "
            "(expected a JSON object at the top level)."
        )
    return data


def current_settings_from_full_profile(full_profile: dict[str, Any]) -> dict[str, Any]:
    """Pulls just the keys this project's optimizer actually reasons
    about (DEFAULT_CURRENT_SETTINGS' keys) out of a full loaded profile,
    so the GUI's editable grid stays focused on the handful of settings
    that matter to a recommendation instead of showing all ~150 raw
    Bambu Studio keys. Falls back to this project's own default for any
    key the loaded profile doesn't have — normal, since Bambu Studio's
    export can omit a key that's sitting at its preset's inherited
    default rather than an explicit override.
    """
    return {
        key: full_profile[key] if key in full_profile else default
        for key, default in DEFAULT_CURRENT_SETTINGS.items()
    }


def build_full_export_profile(
    run: AnalysisRun, approved_keys: set[str], full_profile: dict[str, Any] | None
) -> dict[str, Any]:
    """The complete settings file to hand back to Bambu Studio: every key
    from your loaded real profile (or, if you never loaded one, just
    this project's own tracked keys) with ONLY the approved changes
    overlaid — the same "touch only what's approved, leave everything
    else exactly as it was" guarantee as `build_export_settings`, just
    starting from a full real profile instead of a dozen-key subset, so
    the result is something you can import straight back into Bambu
    Studio rather than a partial diff.
    """
    base = full_profile if full_profile is not None else run.context.current_settings
    return apply_changes(base, run.optimization.changes, approved_keys)


def build_export_text(run: AnalysisRun, approved_keys: set[str]) -> str:
    lines = [
        f"Bambu Companion recommendations for: {Path(run.model_path).name}",
        f"Goal: {GOAL_LABELS[run.context.goal]}    Material: {run.context.material}    "
        f"Nozzle: {run.context.nozzle_diameter_mm}mm",
        "",
        "Enter these into Bambu Studio's Process panel yourself (as a copy of your profile,",
        "not overwriting it) — this tool does not write into Bambu Studio automatically yet.",
        "",
    ]
    approved = [c for c in run.optimization.changes if c.key in approved_keys]
    skipped = [c for c in run.optimization.changes if c.key not in approved_keys]

    if approved:
        lines.append("Apply these:")
        for c in approved:
            lines.append(f"  {c.label}: {c.current_value} -> {c.recommended_value}")
            lines.append(f"    because {c.reason}")
        lines.append("")
    if skipped:
        lines.append("Not approved (left as-is):")
        for c in skipped:
            lines.append(f"  {c.label} (would be {c.current_value} -> {c.recommended_value})")
        lines.append("")
    if run.optimization.uncertainties:
        lines.append("Flagged for your review — not auto-applied:")
        for note in run.optimization.uncertainties:
            lines.append(f"  - {note}")
        lines.append("")

    return "\n".join(lines)


def export_files(
    run: AnalysisRun,
    approved_keys: set[str],
    out_dir: str | Path,
    full_profile: dict[str, Any] | None = None,
) -> tuple[Path, ...]:
    """Writes a human-readable .txt and a machine-readable .json next to
    (or into) `out_dir`. If `full_profile` is given (from
    `load_full_profile`), also writes a third file — a complete Bambu
    Studio profile with just the approved changes overlaid, named so it's
    obvious it's the one to import back into Bambu Studio (Process panel
    -> right-click your preset -> Import) — and returns
    (txt_path, json_path, full_profile_path). Without it, returns just
    (txt_path, json_path), same as before.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(run.model_path).stem

    txt_path = out_dir / f"{stem}_bambu_companion_recommendations.txt"
    json_path = out_dir / f"{stem}_bambu_companion_settings.json"

    txt_path.write_text(build_export_text(run, approved_keys), encoding="utf-8")
    json_path.write_text(
        json.dumps(build_export_settings(run, approved_keys), indent=2), encoding="utf-8"
    )

    if full_profile is None:
        return txt_path, json_path

    full_path = out_dir / f"{stem}_bambu_studio_full_profile.json"
    full_path.write_text(
        json.dumps(build_full_export_profile(run, approved_keys, full_profile), indent=2),
        encoding="utf-8",
    )
    return txt_path, json_path, full_path
