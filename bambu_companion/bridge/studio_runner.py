"""Bambu Studio Runner — drives the Bambu Studio command line to slice a
model with a temporary, per-job set of settings and export the result.

Layout: pure, unit-tested builders (`build_cli_args`,
`candidate_executables`, `verify_settings_applied`) and thin wrappers
that actually launch Bambu Studio (`run_cli`, `run_slice_job`,
`slice_project`, `slice_with_presets`). The wrappers can only be
exercised where Bambu Studio is installed.

WHAT CHANGED ON 2026-10-07, AND WHY

The first version built `--load-settings process.json` — one file — and
every real attempt on 2026-09-20 was made with no settings files at all.
Those attempts failed with:

    plate 1: Nothing to be sliced, Either the print is empty or no
    object is fully inside the print volume before apply.

Bambu's own documentation (wiki: Command Line Usage) defines the option
as `--load-settings "machine.json;process.json"` — the printer and the
process config together, semicolon-separated in ONE argument, both as
*full* configs — and its STL example is:

    bambu-studio --orient --arrange 1 --load-settings "machine.json;
    process.json" --load-filaments "filament.json" --slice 2 --debug 2
    --export-3mf output.3mf boat.stl

Without a machine config the slicer has no bed to place a bare STL on,
which fits the error. So the builder now passes machine + process the
documented way, puts options before the input file as the docs do, and
`profiles/preset_library.py` produces the full configs.

HONEST STATUS: that reading of the failure is a well-supported theory,
NOT a confirmed fix. It has not been run against a real install from
the environment this was written in. Two things the theory does not
explain: the upstream report of the same message (bambulab/BambuStudio
#5041) *did* pass both files and was intermittent, and a GUI-saved
project 3MF failed here too. That is why `bridge/slice_check.py` exists:
it runs each documented invocation on the real machine, records exactly
what Bambu Studio said (including its `result.json` and debug log), and
remembers the first one that works. Until that has passed on your PC,
treat `run_slice_job` as untested end-to-end.

Whatever happens, this module never reports a slice it can't back up
(Failure Behavior): a zero exit code is not enough — the output file
must exist and contain a real slice result (a positive time
prediction). Whether the *settings asked for* made it into that file is
checked separately and returned as `settings_verified`; a slice whose
settings can't be found is reported as exactly that, never as the
approved job.

Nothing here can start a print. The Bambu Studio CLI slices and writes
files; it has no option to send a job to a printer.
"""

from __future__ import annotations

import json
import os
import shutil
import string
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..profiles import project_3mf
from ..profiles.bambu_values import overlay, validate, values_equal
from ..profiles.preset_library import PresetLibrary
from .studio_config import load_studio_config

ENV_EXECUTABLE = "BAMBU_STUDIO_EXE"
_EXE_NAMES = ("bambu-studio.exe", "BambuStudio.exe")
_INSTALL_DIRS = ("Program Files/Bambu Studio", "Bambu Studio", "Program Files (x86)/Bambu Studio")
_LOG_TAIL_LINES = 60


class StudioRunnerError(RuntimeError):
    """Any failure locating, launching, or verifying the output of Bambu
    Studio. Always carries the real underlying message, plus whatever
    evidence exists in `details` (command, exit code, output tail,
    Bambu Studio's own result.json) — this project never swallows the
    actual CLI error."""

    def __init__(self, message: str, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.details: dict[str, Any] = dict(details or {})


# Flag names confirmed against `--help` from BambuStudio-02.08.03.66 on
# 2026-09-20 (slice / export-3mf / orient / arrange / load-settings /
# load-filaments). `--debug`, `--outputdir` and `--curr-bed-type` are
# from Bambu's Command Line Usage wiki page and were not in that capture.
_KNOWN_FLAGS = {
    "slice": "--slice",
    "load_settings": "--load-settings",
    "load_filaments": "--load-filaments",
    "export_3mf": "--export-3mf",
    "orient": "--orient",
    "arrange": "--arrange",
    "debug": "--debug",
    "outputdir": "--outputdir",
    "bed_type": "--curr-bed-type",
}


@dataclass
class SliceJobSpec:
    """One headless slice. Only `model_path` and `output_3mf_path` are
    required: a Bambu Studio project 3MF carries its own settings, so
    the settings files are optional (docs example 1)."""

    model_path: Path
    output_3mf_path: Path
    machine_settings_path: Path | None = None
    process_settings_path: Path | None = None
    filament_settings_paths: list[Path] = field(default_factory=list)
    plate: int = 0  # 0 = every plate
    orient: bool = False
    arrange: bool = False
    bed_type: str | None = None
    debug_level: int = 2
    use_outputdir: bool = True
    extra_args: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Locating Bambu Studio
# ---------------------------------------------------------------------------


def _windows_drive_roots() -> list[Path]:
    if os.name != "nt":
        return []
    return [Path(f"{letter}:/") for letter in string.ascii_uppercase if Path(f"{letter}:/").exists()]


def candidate_executables(drive_roots: Iterable[Path]) -> list[Path]:
    """Every place a normal Windows install puts the executable, for
    each given drive root. Pure — no filesystem access."""
    return [
        Path(root) / install_dir / exe
        for root in drive_roots
        for install_dir in _INSTALL_DIRS
        for exe in _EXE_NAMES
    ]


def find_bambu_studio_executable(
    explicit_path: str | os.PathLike | None = None,
    *,
    drive_roots: Iterable[Path] | None = None,
    env: Mapping[str, str] | None = None,
    saved_path: str | None = None,
) -> Path:
    """Locates the Bambu Studio executable, in this order:

    1. `explicit_path`, if given (an explicit path that doesn't exist is
       an error, never silently replaced by a guess);
    2. the `BAMBU_STUDIO_EXE` environment variable;
    3. the path saved in `studio_config.json`;
    4. the usual install folders on every drive — not just `C:`. The
       first real machine this ran on had it at
       `G:\\Bambu Studio\\bambu-studio.exe`, which the old `C:`-only
       check could never find;
    5. `bambu-studio` on PATH (Linux/macOS).

    It never searches the whole filesystem; if none of these exist it
    raises and says how to set the path.
    """
    if explicit_path:
        p = Path(explicit_path)
        if not p.is_file():
            raise StudioRunnerError(f"Configured Bambu Studio path does not exist: {p}")
        return p

    env = os.environ if env is None else env
    if saved_path is None:
        saved_path = load_studio_config().get("studio_executable") or ""
    for configured in (env.get(ENV_EXECUTABLE), saved_path):
        if configured and Path(configured).is_file():
            return Path(configured)

    roots = list(drive_roots) if drive_roots is not None else _windows_drive_roots()
    for candidate in candidate_executables(roots):
        if candidate.is_file():
            return candidate

    if os.name != "nt":
        # PATH lookup is for Linux/macOS only: on Windows `which` also
        # searches the current directory, and a program that happens to
        # be called bambu-studio.exe in whatever folder this was started
        # from is not something to run.
        for name in ("bambu-studio", "BambuStudio"):
            found = shutil.which(name)
            if found:
                return Path(found)

    raise StudioRunnerError(
        "Could not find Bambu Studio. Looked in the usual install folders on every drive "
        f"and at ${ENV_EXECUTABLE}. Tell Bambu Companion where it is by running "
        '`python -m bambu_companion.bridge.slice_check --exe "<path to bambu-studio.exe>"` '
        "once, or by setting the BAMBU_STUDIO_EXE environment variable."
    )


# ---------------------------------------------------------------------------
# Building and running the command line
# ---------------------------------------------------------------------------


def build_cli_args(spec: SliceJobSpec) -> list[str]:
    """Pure function: the CLI argument list for a SliceJobSpec (NOT
    including the executable). Options first, input file last — the
    order of every example in Bambu's own documentation.
    """
    args: list[str] = []
    if spec.bed_type:
        args += [_KNOWN_FLAGS["bed_type"], spec.bed_type]

    settings_files = [p for p in (spec.machine_settings_path, spec.process_settings_path) if p]
    if settings_files:
        # ONE argument, semicolon-separated: "machine.json;process.json".
        args += [_KNOWN_FLAGS["load_settings"], ";".join(str(p) for p in settings_files)]
    if spec.filament_settings_paths:
        args += [_KNOWN_FLAGS["load_filaments"], ";".join(str(p) for p in spec.filament_settings_paths)]

    if spec.orient:
        args += [_KNOWN_FLAGS["orient"], "1"]
    if spec.arrange:
        args += [_KNOWN_FLAGS["arrange"], "1"]

    args += [_KNOWN_FLAGS["slice"], str(spec.plate), _KNOWN_FLAGS["debug"], str(spec.debug_level)]

    if spec.use_outputdir:
        # Documented pairing: a directory plus a bare file name. Bambu
        # Studio also writes its machine-readable result.json there.
        args += [
            _KNOWN_FLAGS["outputdir"],
            str(spec.output_3mf_path.parent),
            _KNOWN_FLAGS["export_3mf"],
            spec.output_3mf_path.name,
        ]
    else:
        args += [_KNOWN_FLAGS["export_3mf"], str(spec.output_3mf_path)]

    args.extend(spec.extra_args)
    args.append(str(spec.model_path))
    return args


def _tail(text: str | None, lines: int = _LOG_TAIL_LINES) -> str:
    return "\n".join((text or "").splitlines()[-lines:])


def run_cli(
    executable: Path,
    args: Sequence[str],
    *,
    timeout_s: float = 900.0,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Launches Bambu Studio with `args` and returns what happened:
    {command, returncode, duration_s, stdout_tail, stderr_tail}. Raises
    StudioRunnerError only if it could not be launched or timed out — a
    non-zero exit code is returned, not raised, so callers (and the
    diagnostic) can report it with full context."""
    command = [str(executable), *args]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            # Never let the child share our stdin: when this runs inside
            # the connector, stdin is the protocol stream.
            stdin=subprocess.DEVNULL,
            text=True,
            errors="replace",
            timeout=timeout_s,
            cwd=str(cwd) if cwd else None,
            # No console window flashing up when launched from a
            # windowless host (the connector, pythonw).
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise StudioRunnerError(
            f"Bambu Studio did not finish within {timeout_s:.0f}s.",
            {"command": command, "stdout_tail": _tail(_decode(exc.stdout)), "stderr_tail": _tail(_decode(exc.stderr))},
        ) from exc
    except OSError as exc:
        raise StudioRunnerError(
            f"Failed to launch Bambu Studio at {executable}: {exc}", {"command": command}
        ) from exc
    return {
        "command": command,
        "returncode": proc.returncode,
        "duration_s": round(time.monotonic() - started, 1),
        "stdout_tail": _tail(proc.stdout),
        "stderr_tail": _tail(proc.stderr),
    }


def _decode(data: Any) -> str:
    if data is None:
        return ""
    return data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)


def _read_result_json(directory: Path) -> dict[str, Any] | None:
    """Bambu Studio's own machine-readable verdict, when it wrote one."""
    try:
        data = json.loads((directory / "result.json").read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def run_slice_job(
    spec: SliceJobSpec,
    *,
    executable_path: str | os.PathLike | None = None,
    expected_settings: Mapping[str, Any] | None = None,
    timeout_s: float = 900.0,
    runner: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Runs Bambu Studio headless to slice + export.

    Returns {success: True, output_path, settings_verified,
    unverified_settings, slice_result, run} — or raises
    StudioRunnerError. It never returns success for a non-zero exit, a
    missing output file, or an output with no slice result in it.
    `settings_verified` is False (with the keys listed) if any expected
    setting isn't in the exported file with the expected value; that is
    reported, not hidden, and the caller decides what to do with it.

    `runner` replaces `run_cli` in tests (same signature and return
    shape); production code leaves it alone.
    """
    exe = find_bambu_studio_executable(executable_path)
    if spec.output_3mf_path.exists():
        raise StudioRunnerError(
            f"{spec.output_3mf_path} already exists. Refusing to overwrite it."
        )
    spec.output_3mf_path.parent.mkdir(parents=True, exist_ok=True)

    run = (runner or run_cli)(
        exe, build_cli_args(spec), timeout_s=timeout_s, cwd=spec.output_3mf_path.parent
    )
    run["result_json"] = _read_result_json(spec.output_3mf_path.parent)

    if run["returncode"] != 0:
        raise StudioRunnerError(
            f"Bambu Studio exited with code {run['returncode']}.\n"
            f"{_studio_error_text(run)}",
            run,
        )
    if not spec.output_3mf_path.exists():
        raise StudioRunnerError(
            "Bambu Studio exited with code 0 but did not create the output file "
            f"{spec.output_3mf_path.name}. Treating that as a failure rather than trusting "
            f"the exit code.\n{_studio_error_text(run)}",
            run,
        )

    try:
        slice_result = project_3mf.read_slice_result(spec.output_3mf_path)
    except project_3mf.ProjectFileError as exc:
        raise StudioRunnerError(
            f"Bambu Studio exited with code 0 but its output file can't be read: {exc}", run
        ) from exc
    if not slice_result:
        raise StudioRunnerError(
            f"Bambu Studio wrote {spec.output_3mf_path.name} but it contains no slice result "
            "(no plate with a print-time prediction). Treating that as a failure.",
            run,
        )

    settings_verified, unverified = (True, [])
    if expected_settings:
        settings_verified, unverified = verify_settings_applied(spec.output_3mf_path, expected_settings)

    return {
        "success": True,
        "output_path": str(spec.output_3mf_path),
        "settings_verified": settings_verified,
        "unverified_settings": unverified,
        "slice_result": slice_result,
        "run": run,
    }


def _studio_error_text(run: Mapping[str, Any]) -> str:
    parts = []
    result = run.get("result_json")
    if result:
        parts.append(f"result.json: {json.dumps(result)[:600]}")
    for stream in ("stderr_tail", "stdout_tail"):
        if run.get(stream):
            parts.append(f"{stream}:\n{run[stream]}")
    return "\n".join(parts) or "(Bambu Studio produced no output.)"


def verify_settings_applied(
    exported_3mf_path: str | os.PathLike, expected_settings: Mapping[str, Any]
) -> tuple[bool, list[str]]:
    """Checks that the settings we asked for are really in the exported
    3MF. Needed because `--load-settings` has been reported to fall back
    to defaults silently (the project's Research Findings).

    This reads `Metadata/project_settings.config` — the JSON Bambu
    Studio embeds in every project and sliced export (layout confirmed
    against a real 02.08.04.57 sliced file) — and compares each expected
    value with what is stored, representation-aware (`4` == `"4"`,
    `15` == `"15%"`, `200` == `["200"]`). Returns (all_found, keys that
    are missing or have a different value).
    """
    try:
        stored = project_3mf.read_project_settings(exported_3mf_path)
    except project_3mf.ProjectFileError as exc:
        raise StudioRunnerError(
            f"Could not read settings back from {exported_3mf_path} to verify them: {exc}"
        ) from exc
    unverified = [
        key
        for key, value in expected_settings.items()
        if key not in stored or not values_equal(key, stored[key], value)
    ]
    return (not unverified, unverified)


# ---------------------------------------------------------------------------
# The two ways settings reach the slicer
# ---------------------------------------------------------------------------

STRATEGY_PROJECT = "project"  # settings travel inside a project 3MF
STRATEGY_PRESETS = "presets"  # full machine/process/filament JSON files


def _validated(changes: Mapping[str, Any] | None) -> dict[str, Any]:
    return {key: validate(key, value) for key, value in (changes or {}).items()}


def place_exclusive(source: Path, destination: Path) -> Path:
    """Copies `source` to `destination` WITHOUT ever replacing a file,
    and returns the path actually used.

    The "does it exist?" checks at the start of a slice can be many
    minutes old by the time the result is ready, and `shutil.move`
    replaces whatever is there. So the destination is created with mode
    "x", which fails if the name is taken at that instant; in that case
    the result goes next to it as `<name>_2.3mf`, `_3`… rather than
    being thrown away after a long slice.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    candidate = destination
    n = 2
    while True:
        try:
            out = open(candidate, "xb")
        except FileExistsError:
            candidate = destination.with_name(f"{destination.stem}_{n}{destination.suffix}")
            n += 1
            if n > 1000:
                raise StudioRunnerError(
                    f"Could not find a free file name next to {destination}."
                ) from None
            continue
        try:
            with out, open(source, "rb") as src:
                shutil.copyfileobj(src, out)
        except BaseException:
            try:
                candidate.unlink()  # ours: we created it a moment ago
            except OSError:
                pass
            raise
        return candidate


def _finish(result: dict[str, Any], final_output: Path) -> dict[str, Any]:
    """Puts the sliced file where the caller asked for it (or right
    next to it, if that name was taken in the meantime)."""
    placed = place_exclusive(Path(result["output_path"]), final_output)
    if placed != final_output:
        result["output_renamed"] = (
            f"{final_output.name} appeared while slicing, so the result was saved as "
            f"{placed.name} instead of replacing it."
        )
    result["output_path"] = str(placed)
    return result


def slice_project(
    project_path: str | os.PathLike,
    output_path: str | os.PathLike,
    *,
    changes: Mapping[str, Any] | None = None,
    executable_path: str | os.PathLike | None = None,
    plate: int = 0,
    use_outputdir: bool = True,
    timeout_s: float = 900.0,
    runner: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Slices a Bambu Studio project 3MF using the settings stored in
    it, with `changes` (approved, registry-listed settings) applied to a
    temporary copy first. The project you pass in is never modified.
    """
    project_path, output_path = Path(project_path).resolve(), Path(output_path).resolve()
    typed = _validated(changes)
    if output_path.exists():
        raise StudioRunnerError(f"{output_path} already exists. Refusing to overwrite it.")

    with tempfile.TemporaryDirectory(prefix="bambu_companion_") as tmp:
        work = Path(tmp)
        try:
            job_input = work / "job_input.3mf"
            project_3mf.write_project_copy(project_path, job_input, typed)
        except project_3mf.ProjectFileError as exc:
            raise StudioRunnerError(str(exc)) from exc
        out_dir = work / "out"
        out_dir.mkdir()
        spec = SliceJobSpec(
            model_path=job_input,
            output_3mf_path=out_dir / "sliced.3mf",
            plate=plate,
            use_outputdir=use_outputdir,
        )
        result = run_slice_job(
            spec, executable_path=executable_path, expected_settings=typed, timeout_s=timeout_s, runner=runner
        )
        result["strategy"] = STRATEGY_PROJECT
        return _finish(result, output_path)


def write_full_preset_files(
    library: PresetLibrary,
    directory: Path,
    *,
    machine: str,
    process: str,
    filaments: Sequence[str],
    changes: Mapping[str, Any] | None = None,
) -> tuple[Path, Path, list[Path]]:
    """Resolves the named presets to full configs and writes them into
    `directory` as machine.json / process.json / filament_N.json, with
    `changes` overlaid on the process config only. The presets on disk
    are only read — this is the Temporary Profile Rule for the
    presets strategy."""
    directory.mkdir(parents=True, exist_ok=True)
    machine_cfg = library.load_full("machine", machine)
    process_cfg = overlay(library.load_full("process", process), _validated(changes))

    machine_path = directory / "machine.json"
    process_path = directory / "process.json"
    machine_path.write_text(json.dumps(machine_cfg, indent=2), encoding="utf-8")
    process_path.write_text(json.dumps(process_cfg, indent=2), encoding="utf-8")

    filament_paths = []
    for i, name in enumerate(filaments, start=1):
        path = directory / f"filament_{i}.json"
        path.write_text(json.dumps(library.load_full("filament", name), indent=2), encoding="utf-8")
        filament_paths.append(path)
    return machine_path, process_path, filament_paths


def slice_with_presets(
    model_path: str | os.PathLike,
    output_path: str | os.PathLike,
    *,
    library: PresetLibrary,
    machine: str,
    process: str,
    filaments: Sequence[str],
    changes: Mapping[str, Any] | None = None,
    bed_type: str | None = None,
    executable_path: str | os.PathLike | None = None,
    orient: bool = False,
    arrange: bool = True,
    use_outputdir: bool = True,
    timeout_s: float = 900.0,
    runner: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Slices a bare STL (or a 3MF) with full machine + process +
    filament configs built from named presets, `changes` applied to the
    process config.

    Arranges by default (a bare STL has no plate position of its own)
    but does NOT auto-orient. Bambu's own STL example passes --orient,
    and the first real slices here did too — and on 2026-10-08 that
    stood Matt's deck-box lid (deliberately exported upside down so the
    pocket prints facing the plate) on its side: an 89 mm part came out
    118.8 mm tall with 1,173 layers. Bambu Studio's window keeps a
    file's orientation on import; so does this, unless asked.
    """
    # Absolute: Bambu Studio is started in a scratch folder, so a
    # relative path would point at nothing there.
    model_path, output_path = Path(model_path).resolve(), Path(output_path).resolve()
    if not model_path.is_file():
        raise StudioRunnerError(f"Model file not found: {model_path}")
    if output_path.exists():
        raise StudioRunnerError(f"{output_path} already exists. Refusing to overwrite it.")
    typed = _validated(changes)

    with tempfile.TemporaryDirectory(prefix="bambu_companion_") as tmp:
        work = Path(tmp)
        machine_path, process_path, filament_paths = write_full_preset_files(
            library, work / "settings", machine=machine, process=process, filaments=filaments, changes=typed
        )
        out_dir = work / "out"
        out_dir.mkdir()
        spec = SliceJobSpec(
            model_path=model_path,
            output_3mf_path=out_dir / "sliced.3mf",
            machine_settings_path=machine_path,
            process_settings_path=process_path,
            filament_settings_paths=filament_paths,
            orient=orient,
            arrange=arrange,
            bed_type=bed_type,
            use_outputdir=use_outputdir,
        )
        result = run_slice_job(
            spec, executable_path=executable_path, expected_settings=typed, timeout_s=timeout_s, runner=runner
        )
        result["strategy"] = STRATEGY_PRESETS
        result["presets"] = {"machine": machine, "process": process, "filaments": list(filaments)}
        return _finish(result, output_path)


def write_temp_process_json(settings: dict[str, Any], path: Path) -> None:
    """Writes a settings dict as JSON. Kept for callers that already
    hold a complete process config; to build one from a preset name use
    `write_full_preset_files`, which resolves inheritance so the file is
    a *full* config as the CLI requires."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
