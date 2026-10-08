"""Slice check — finds out, on the real machine, which way of calling
the Bambu Studio command line actually works.

Headless slicing failed on 2026-09-20 with "Nothing to be sliced" and
the cause was never pinned down (see `studio_runner.py`). Rather than
guess again, this runs each *documented* invocation against a small
model, records exactly what Bambu Studio answered — exit code, its own
`result.json`, the tail of its debug log — and remembers the first one
that works so `studio_runner` can use it from then on.

Run it:

    python -m bambu_companion.bridge.slice_check
    python -m bambu_companion.bridge.slice_check path\\to\\project.3mf
    python -m bambu_companion.bridge.slice_check --exe "G:\\Bambu Studio\\bambu-studio.exe"

With no model it slices a generated 20 mm cube. Everything is written
to a temporary folder that is removed afterwards; the only things kept
are the report (`%APPDATA%\\BambuCompanion\\slice_check_report.json` and
`.txt`) and, on success, the name of the working strategy in
`studio_config.json`. Nothing is sent to a printer — the Bambu Studio
CLI can't do that.
"""

from __future__ import annotations

import argparse
import json
import struct
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from ..profiles import project_3mf
from ..profiles.preset_library import PresetError, PresetLibrary, discover_roots
from . import studio_runner as sr
from .studio_config import config_dir, load_studio_config, update_studio_config

REPORT_STEM = "slice_check_report"


def write_test_cube(path: Path, size_mm: float = 20.0) -> Path:
    """Writes a binary STL of a cube sitting on z=0 — the smallest
    honest test model: closed, flat-bottomed, trivially inside any bed."""
    s = float(size_mm)
    v = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0), (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    # Two outward-facing triangles per face.
    faces = [
        (0, 2, 1), (0, 3, 2),  # bottom
        (4, 5, 6), (4, 6, 7),  # top
        (0, 1, 5), (0, 5, 4),  # front
        (1, 2, 6), (1, 6, 5),  # right
        (2, 3, 7), (2, 7, 6),  # back
        (3, 0, 4), (3, 4, 7),  # left
    ]
    data = bytearray(b"Bambu Companion slice check cube".ljust(80, b" "))
    data += struct.pack("<I", len(faces))
    for a, b, c in faces:
        data += struct.pack("<12fH", 0.0, 0.0, 0.0, *v[a], *v[b], *v[c], 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(data))
    return path


def _preset_names(model: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Preset names to test with: the ones a project file names for
    itself, otherwise the configured defaults."""
    names = {
        "machine": config["default_machine"],
        "process": config["default_process"],
        "filaments": [config["default_filament"]],
        "bed_type": config["default_bed_type"],
        "source": "studio_config defaults",
    }
    if model.suffix.lower() == ".3mf" and project_3mf.has_project_settings(model):
        info = project_3mf.describe_project(model)
        if info.get("printer") and info.get("process_preset") and info.get("filament_presets"):
            names.update(
                machine=info["printer"],
                process=info["process_preset"],
                filaments=[info["filament_presets"][0]],
                bed_type=info.get("bed_type") or names["bed_type"],
                source="the project file",
            )
    return names


def _attempt(
    name: str,
    description: str,
    spec: sr.SliceJobSpec,
    *,
    executable: Path,
    timeout_s: float,
    runner: Callable[..., dict[str, Any]] | None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": name, "description": description, "ok": False}
    try:
        result = sr.run_slice_job(spec, executable_path=executable, timeout_s=timeout_s, runner=runner)
    except sr.StudioRunnerError as exc:
        entry["error"] = str(exc).splitlines()[0]
        entry.update({k: exc.details.get(k) for k in ("command", "returncode", "duration_s", "result_json")})
        entry["output_tail"] = (exc.details.get("stderr_tail") or exc.details.get("stdout_tail") or "")[-3000:]
        return entry
    entry["ok"] = True
    entry["command"] = result["run"]["command"]
    entry["duration_s"] = result["run"]["duration_s"]
    entry["slice_result"] = result["slice_result"]
    return entry


def run_slice_check(
    model_path: str | Path | None = None,
    *,
    executable_path: str | None = None,
    timeout_s: float = 300.0,
    save: bool = True,
    report_dir: Path | None = None,
    runner: Callable[..., dict[str, Any]] | None = None,
    library: PresetLibrary | None = None,
) -> dict[str, Any]:
    """Runs the check and returns the report dict (also written to disk
    when `save` is true). Never raises for a Bambu Studio failure — a
    failed attempt is a result, recorded with its evidence."""
    config = load_studio_config()
    report: dict[str, Any] = {"attempts": [], "working_strategy": None}

    try:
        exe = sr.find_bambu_studio_executable(executable_path)
    except sr.StudioRunnerError as exc:
        report["error"] = str(exc)
        return _save(report, save, report_dir)
    report["executable"] = str(exe)

    library = library or PresetLibrary(discover_roots(exe))
    report["presets_found"] = library.describe()

    with tempfile.TemporaryDirectory(prefix="bambu_companion_check_") as tmp:
        work = Path(tmp)
        if model_path:
            # Absolute: Bambu Studio runs in a scratch folder, where a
            # relative path would not resolve and every attempt would
            # fail for a reason that has nothing to do with slicing.
            model = Path(model_path).resolve()
            if not model.is_file():
                report["error"] = f"Model file not found: {model}"
                return _save(report, save, report_dir)
        else:
            model = write_test_cube(work / "test_cube_20mm.stl")
        report["model"] = str(model) if model_path else "generated 20 mm cube"

        counter = 0

        def out_path() -> Path:
            nonlocal counter
            counter += 1
            d = work / f"attempt_{counter}"
            d.mkdir()
            return d / "sliced.3mf"

        attempts: list[tuple[str, str, sr.SliceJobSpec]] = []

        is_project = model.suffix.lower() == ".3mf" and project_3mf.has_project_settings(model)
        if is_project:
            base = sr.SliceJobSpec(model_path=model, output_3mf_path=out_path())
            attempts.append(
                (sr.STRATEGY_PROJECT, "Project 3MF with its own embedded settings (docs example 1)", base)
            )
            attempts.append(
                (
                    f"{sr.STRATEGY_PROJECT}+fullpath",
                    "Same, but --export-3mf given a full path instead of --outputdir + file name",
                    replace(base, output_3mf_path=out_path(), use_outputdir=False),
                )
            )

        names = _preset_names(model, config)
        report["presets_used"] = names
        try:
            machine, process, filaments = sr.write_full_preset_files(
                library,
                work / "settings",
                machine=names["machine"],
                process=names["process"],
                filaments=names["filaments"],
            )
        except PresetError as exc:
            report["preset_error"] = str(exc)
        else:
            base = sr.SliceJobSpec(
                model_path=model,
                output_3mf_path=out_path(),
                machine_settings_path=machine,
                process_settings_path=process,
                filament_settings_paths=filaments,
                orient=not is_project,
                arrange=not is_project,
            )
            attempts.append(
                (
                    sr.STRATEGY_PRESETS,
                    "Full machine + process + filament configs via --load-settings/--load-filaments "
                    "(docs examples 2 and 3)",
                    base,
                )
            )
            attempts.append(
                (
                    f"{sr.STRATEGY_PRESETS}+bed",
                    f"Same, plus --curr-bed-type \"{names['bed_type']}\"",
                    replace(base, output_3mf_path=out_path(), bed_type=names["bed_type"]),
                )
            )
            attempts.append(
                (
                    f"{sr.STRATEGY_PRESETS}+fullpath",
                    "Same as the first presets attempt, but --export-3mf given a full path",
                    replace(base, output_3mf_path=out_path(), use_outputdir=False),
                )
            )

        for name, description, spec in attempts:
            report["attempts"].append(
                _attempt(name, description, spec, executable=exe, timeout_s=timeout_s, runner=runner)
            )

    working = next((a["name"] for a in report["attempts"] if a["ok"]), None)
    report["working_strategy"] = working
    if save:
        # The executable is worth remembering whether or not slicing
        # worked. The strategy is written either way too: a check that
        # fails must CLEAR an earlier "confirmed working", or the
        # connector would keep saying so.
        update_studio_config(working_strategy=working or "", studio_executable=str(exe))
    return _save(report, save, report_dir)


def _save(report: dict[str, Any], save: bool, report_dir: Path | None) -> dict[str, Any]:
    report["summary"] = format_report(report)
    if save:
        directory = report_dir or config_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{REPORT_STEM}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            (directory / f"{REPORT_STEM}.txt").write_text(report["summary"], encoding="utf-8")
            report["report_path"] = str(directory / f"{REPORT_STEM}.txt")
        except OSError as exc:
            report["report_error"] = f"Could not save the report: {exc}"
    return report


def format_report(report: dict[str, Any]) -> str:
    lines = ["Bambu Companion slice check", ""]
    if report.get("error"):
        return "\n".join([*lines, f"STOPPED: {report['error']}"])
    lines.append(f"Bambu Studio: {report.get('executable')}")
    lines.append(f"Model:        {report.get('model')}")
    found = report.get("presets_found", {})
    lines.append(f"Preset folders found: {', '.join(found.get('roots', [])) or 'NONE'}")
    lines.append(f"Preset counts: {found.get('counts')}")
    if report.get("presets_used"):
        used = report["presets_used"]
        lines.append(
            f"Presets used (from {used['source']}): {used['machine']} / {used['process']} / "
            f"{', '.join(used['filaments'])}"
        )
    if report.get("preset_error"):
        lines.append(f"PRESETS COULD NOT BE BUILT: {report['preset_error']}")
    lines.append("")

    for a in report.get("attempts", []):
        lines.append(f"[{'OK  ' if a['ok'] else 'FAIL'}] {a['name']}: {a['description']}")
        if a.get("command"):
            # Quoted so the line can be pasted into a terminal: paths
            # with spaces, and the ";"-joined settings list (";" ends a
            # command in PowerShell).
            lines.append("       " + " ".join(f'"{c}"' if (" " in c or ";" in c) else c for c in a["command"]))
        if a["ok"]:
            for plate in a.get("slice_result", []):
                lines.append(
                    f"       plate {plate['plate']}: {plate.get('print_time')}, "
                    f"{plate.get('filament_g')} g, support used: {plate.get('support_used')}"
                )
        else:
            lines.append(f"       {a.get('error')}")
            if a.get("result_json"):
                lines.append(f"       result.json: {json.dumps(a['result_json'])[:400]}")
            for row in (a.get("output_tail") or "").splitlines()[-12:]:
                lines.append(f"       | {row}")
        lines.append("")

    if not report.get("attempts"):
        lines.append("No attempt could be set up — see the messages above.")
    elif report.get("working_strategy"):
        lines.append(f"RESULT: headless slicing works with strategy '{report['working_strategy']}'.")
    else:
        lines.append(
            "RESULT: no invocation worked. The evidence above (exit codes, result.json, log "
            "tails) is what to look at next — do not assume slicing works."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check which Bambu Studio CLI invocation works here.")
    parser.add_argument("model", nargs="?", help="Optional .stl or project .3mf (default: a generated cube)")
    parser.add_argument("--exe", default=None, help="Path to bambu-studio.exe if it isn't found automatically")
    parser.add_argument("--timeout", type=float, default=300.0, help="Seconds allowed per attempt")
    args = parser.parse_args(argv)

    report = run_slice_check(args.model, executable_path=args.exe, timeout_s=args.timeout)
    print(report["summary"])
    if report.get("report_path"):
        print(f"\nReport saved to: {report['report_path']}")
    return 0 if report.get("working_strategy") else 1


if __name__ == "__main__":
    raise SystemExit(main())
