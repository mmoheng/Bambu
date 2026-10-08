"""Bambu Studio Runner — wraps the Bambu Studio CLI to slice a model with
a temporary settings profile and export the result.

Split deliberately into a pure, fully-testable command-builder
(`build_cli_args`) and an execution wrapper (`run_slice_job`) that
actually shells out. The builder can be unit tested on any machine; the
execution wrapper can only be exercised where Bambu Studio is actually
installed (your Windows PC) — running it here would just fail with
"executable not found", which is expected, not a bug.

IMPORTANT — read before trusting this against a real print:
The CLI flags below (`--slice`, `--load-settings`, `--load-filaments`,
`--export-3mf`, etc.) were confirmed for real on 2026-09-20 against
BambuStudio-02.08.03.66 (`--help` output captured directly from that
install) — `_KNOWN_FLAGS` matches this build. Re-check against your own
`<bambu-studio-exe> --help` if you're on a different version, since this
is a fast-moving fork and flags do change between releases.

UNRESOLVED as of 2026-09-20 — headless slicing currently fails on this
same real install: every attempt (`--slice 0 --export-3mf ...`, with and
without `--orient 1 --arrange 1`, against both a raw STL and a
GUI-saved `.3mf` project with the object already placed) errors with:

    plate 1: Nothing to be sliced, Either the print is empty or no
    object is fully inside the print volume before apply.

even though the tested model (94x79x106mm) is comfortably within the
A1's build volume. This matches a known, closed upstream issue
(bambulab/BambuStudio#5041) whose resolution we could not read (GitHub
blocked the comment thread from this dev environment). NOT YET FIXED —
`run_slice_job` should not be trusted to actually produce output until
this is root-caused for real (it does correctly raise `StudioRunnerError`
rather than report false success when this happens, per the project's
Failure Behavior rule, but "correctly fails" isn't "works"). Leading
theory, untested: none of our attempts included `--load-settings`/
`--load-filaments` — every one of Bambu's own documented *working*
CLI examples does include a real process/filament/machine profile, so
the missing profile may be exactly what's needed. See
`bridge/README.md`'s status table for the fuller writeup and next steps.

There's also a known, reported CLI reliability gap unrelated to the
above: `--load-settings` / `--load-filaments` have been observed to
silently fall back to defaults instead of erroring (see the project's
Research Findings). That's why `run_slice_job` always runs
`verify_settings_applied()` against the exported 3MF afterward instead
of trusting a zero exit code — per the memory bank's Failure Behavior:
"CLI silently ignored settings: verify recommended settings actually
landed in the exported 3MF/gcode metadata before reporting success."
"""

from __future__ import annotations

import json
import re
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class StudioRunnerError(RuntimeError):
    """Raised for any failure in locating, invoking, or verifying the
    output of Bambu Studio. Always carries the real underlying message —
    per Failure Behavior, this project never reports success it can't
    back up, and never swallows the actual CLI error."""


_KNOWN_FLAGS = {
    "slice": "--slice",
    "load_settings": "--load-settings",
    "load_filaments": "--load-filaments",
    "export_3mf": "--export-3mf",
}


@dataclass
class SliceJobSpec:
    model_path: Path
    process_settings_path: Path  # temp process JSON written by the caller
    filament_settings_path: Path
    output_3mf_path: Path
    extra_args: list[str] = field(default_factory=list)


def find_bambu_studio_executable(explicit_path: str | None = None) -> Path:
    """Locates the Bambu Studio executable. Pass `explicit_path` (e.g.
    from a config file) to skip auto-detection entirely — STRONGLY
    recommended in practice, not just as a nicety: confirmed on a real
    machine (2026-09-20) that Bambu Studio is often installed somewhere
    that doesn't match either default candidate below at all (that
    machine had it at `G:\\Bambu Studio\\bambu-studio.exe` — a different
    drive letter and no "Program Files"), so auto-detection failing is
    the common case, not the exception. Always pass the real path if you
    have it.

    Auto-detection only checks the conventional Windows install path;
    it deliberately does NOT search the whole filesystem. If it can't
    find it, it raises rather than guessing.
    """
    if explicit_path:
        p = Path(explicit_path)
        if not p.exists():
            raise StudioRunnerError(f"Configured Bambu Studio path does not exist: {p}")
        return p

    candidates = [
        Path(r"C:\Program Files\Bambu Studio\bambu-studio.exe"),
        Path(r"C:\Program Files\Bambu Studio\BambuStudio.exe"),
    ]
    for c in candidates:
        if c.exists():
            return c

    raise StudioRunnerError(
        "Could not auto-locate the Bambu Studio executable. Pass the path explicitly "
        "(bridge config) — auto-detection only checks the default install path."
    )


def build_cli_args(spec: SliceJobSpec) -> list[str]:
    """Pure function: given a SliceJobSpec, returns the CLI argument list
    (NOT including the executable itself). Fully testable without Bambu
    Studio installed.
    """
    args = [
        str(spec.model_path),
        _KNOWN_FLAGS["load_settings"],
        str(spec.process_settings_path),
        _KNOWN_FLAGS["load_filaments"],
        str(spec.filament_settings_path),
        _KNOWN_FLAGS["slice"],
        "0",  # slice all plates; adjust if/when multi-plate support is added
        _KNOWN_FLAGS["export_3mf"],
        str(spec.output_3mf_path),
    ]
    args.extend(spec.extra_args)
    return args


def run_slice_job(
    spec: SliceJobSpec,
    *,
    executable_path: str | None = None,
    expected_settings: dict[str, Any] | None = None,
    timeout_s: float = 600.0,
) -> dict[str, Any]:
    """Runs Bambu Studio headless to slice+export. Returns a result dict
    with keys: success, stdout, stderr, output_path, settings_verified,
    unverified_settings. Raises StudioRunnerError for anything that means
    the caller should NOT report success (missing exe, non-zero exit,
    missing output file) — this function never returns success=True for
    those cases; it either raises or returns success=True.
    """
    exe = find_bambu_studio_executable(executable_path)
    args = build_cli_args(spec)

    try:
        proc = subprocess.run(
            [str(exe), *args],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise StudioRunnerError(
            f"Bambu Studio CLI did not finish within {timeout_s}s: {exc}"
        ) from exc
    except OSError as exc:
        raise StudioRunnerError(f"Failed to launch Bambu Studio CLI at {exe}: {exc}") from exc

    if proc.returncode != 0:
        raise StudioRunnerError(
            f"Bambu Studio CLI exited with code {proc.returncode}.\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )

    if not spec.output_3mf_path.exists():
        raise StudioRunnerError(
            "Bambu Studio CLI exited successfully (code 0) but the expected output file "
            f"{spec.output_3mf_path} was not created. Treating this as a failure rather than "
            "reporting success — see the project's Failure Behavior rule against trusting "
            "exit code alone."
        )

    settings_verified, unverified = (True, [])
    if expected_settings:
        settings_verified, unverified = verify_settings_applied(
            spec.output_3mf_path, expected_settings
        )

    return {
        "success": True,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "output_path": str(spec.output_3mf_path),
        "settings_verified": settings_verified,
        "unverified_settings": unverified,
    }


def verify_settings_applied(
    exported_3mf_path: Path, expected_settings: dict[str, Any]
) -> tuple[bool, list[str]]:
    """Best-effort check that the settings we asked for actually made it
    into the exported 3MF, in response to the documented CLI reliability
    gap where --load-settings/--load-filaments can silently fall back to
    defaults.

    HEURISTIC, not a certified parse: Bambu Studio embeds process/filament
    settings as metadata inside the exported 3MF (a zip). This scans the
    metadata files for `"key": value` / `key = value` occurrences and flags
    any expected key it can't find matching text for. A key not found here
    is NOT proof the setting failed to apply (metadata layout can vary by
    Bambu Studio version) — but it IS a reason to treat "success" with
    suspicion and check manually rather than trust it blindly. Tighten
    this against a real exported 3MF from your installed version before
    relying on it for unattended use.
    """
    unverified: list[str] = []
    try:
        with zipfile.ZipFile(exported_3mf_path) as zf:
            metadata_text = ""
            for name in zf.namelist():
                if "metadata" in name.lower() or name.lower().endswith((".config", ".json")):
                    try:
                        metadata_text += zf.read(name).decode("utf-8", errors="ignore")
                    except (KeyError, UnicodeDecodeError):
                        continue
    except (zipfile.BadZipFile, FileNotFoundError) as exc:
        raise StudioRunnerError(
            f"Could not open exported 3MF at {exported_3mf_path} to verify settings: {exc}"
        ) from exc

    for key, value in expected_settings.items():
        pattern = re.escape(str(key)) + r'["\']?\s*[:=]\s*["\']?' + re.escape(str(value))
        if not re.search(pattern, metadata_text):
            unverified.append(key)

    return (len(unverified) == 0, unverified)


def write_temp_process_json(settings: dict[str, Any], path: Path) -> None:
    """Writes a temp Bambu Studio process-settings JSON. This is a plain
    key/value dump, not a full valid Bambu Studio process profile (which
    also needs profile inheritance metadata like `inherits`/`from`/
    `instantiation`). In practice you'll want to load your real process
    profile's JSON, overlay only the approved changes from
    profiles.temp_profile.apply_changes, and write the merged result here
    — that keeps every untouched field intact instead of only ever having
    the fields this project explicitly knows about.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
