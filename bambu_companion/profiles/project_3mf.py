"""Reads and rewrites the settings stored inside a Bambu Studio 3MF.

A Bambu Studio project file (File > Save Project) is a zip. The parts
this module cares about, all confirmed against real files written by
Bambu Studio 02.08.04.57:

- `Metadata/project_settings.config` — one JSON object holding the
  complete, flattened printer + process + filament configuration (593
  keys in the file checked). Values are strings, or lists of strings.
- `Metadata/slice_info.config` — XML; after slicing it holds, per
  plate, the time prediction (seconds), filament weight (g), whether
  support was used, per-filament usage and any slicer warnings.
- `Metadata/plate_N.gcode` — only present in a *sliced* export
  (`*.gcode.3mf`); its header repeats time / layer count / weight.

What this module does with that:

- `describe_project` / `read_project_settings`: read the settings a
  file was (or will be) sliced with — the honest answer to "what are my
  current settings", instead of asking the user to retype them.
- `read_slice_result`: read what the slicer reported.
- `write_project_copy`: the Temporary Profile Rule for project files —
  copy a project to a NEW file with only approved, registry-listed
  settings changed. The source file is never modified, an existing file
  is never overwritten, and no saved Bambu Studio preset is touched:
  the copy opens in Bambu Studio as the same preset "modified".

Pure standard library; no Bambu Studio needed.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Mapping

from .bambu_settings import SETTINGS
from .bambu_values import format_like, to_typed, typed_settings, validate, values_equal

PROJECT_SETTINGS_PART = "Metadata/project_settings.config"
SLICE_INFO_PART = "Metadata/slice_info.config"
_GCODE_PART_RE = re.compile(r"^Metadata/plate_(\d+)\.gcode$", re.IGNORECASE)
_GCODE_HEADER_BYTES = 16 * 1024


class ProjectFileError(ValueError):
    """The file isn't something this module can read or safely rewrite.
    The message is written to be shown to a person as-is."""


def _open(path: str | Path) -> zipfile.ZipFile:
    path = Path(path)
    if not path.exists():
        raise ProjectFileError(f"File not found: {path}")
    if path.is_dir():
        raise ProjectFileError(f"{path} is a folder, not a .3mf file.")
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ProjectFileError(f"{path.name} is not a valid 3MF (zip) file: {exc}") from exc


def _find_part(zf: zipfile.ZipFile, wanted: str) -> str | None:
    """Case-insensitive part lookup (older files use `Project_Settings`)."""
    lowered = wanted.lower()
    for name in zf.namelist():
        if name.lower() == lowered:
            return name
    return None


def _load_settings(zf: zipfile.ZipFile, source_name: str) -> tuple[str, dict[str, Any]]:
    part = _find_part(zf, PROJECT_SETTINGS_PART)
    if part is None:
        raise ProjectFileError(
            f"{source_name} has no embedded Bambu Studio settings "
            f"({PROJECT_SETTINGS_PART}). It is a geometry-only 3MF — open it in Bambu "
            "Studio and use File > Save Project to get a project file with settings."
        )
    try:
        data = json.loads(zf.read(part).decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectFileError(f"{source_name}: {part} is not valid JSON ({exc}).") from exc
    if not isinstance(data, dict):
        raise ProjectFileError(f"{source_name}: {part} is not a JSON object.")
    return part, data


def has_project_settings(path: str | Path) -> bool:
    """True for a Bambu Studio project/sliced 3MF, False for a plain
    geometry-only 3MF or anything that isn't a readable zip."""
    try:
        with _open(path) as zf:
            return _find_part(zf, PROJECT_SETTINGS_PART) is not None
    except ProjectFileError:
        return False


def read_project_settings(path: str | Path) -> dict[str, Any]:
    """The raw settings dict exactly as Bambu Studio stored it."""
    with _open(path) as zf:
        return _load_settings(zf, Path(path).name)[1]


def _first(value: Any) -> Any:
    return value[0] if isinstance(value, list) and value else value


def describe_project(path: str | Path) -> dict[str, Any]:
    """A JSON-friendly summary of a project/sliced 3MF: which printer,
    process preset and filaments it uses, the typed value of every
    registry setting it contains, and the slice result if it has one."""
    path = Path(path)
    with _open(path) as zf:
        _, settings = _load_settings(zf, path.name)
        names = zf.namelist()
        sliced_plates = sorted(int(m.group(1)) for n in names if (m := _GCODE_PART_RE.match(n)))
        slice_result = _read_slice_result(zf)

    filament_types = settings.get("filament_type")
    return {
        "file": str(path),
        "studio_version": settings.get("version"),
        "printer": settings.get("printer_settings_id"),
        "printer_model": settings.get("printer_model"),
        "nozzle_diameter_mm": _as_float(_first(settings.get("nozzle_diameter"))),
        "bed_type": settings.get("curr_bed_type"),
        "process_preset": settings.get("print_settings_id"),
        "filament_presets": settings.get("filament_settings_id"),
        "filament_types": filament_types if isinstance(filament_types, list) else [filament_types],
        "changed_from_preset": _changed_keys(settings),
        "settings": typed_settings(settings),
        "missing_registry_keys": sorted(k for k in SETTINGS if k not in settings),
        "contains_gcode_for_plates": sliced_plates,
        "slice_result": slice_result,
    }


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _changed_keys(settings: Mapping[str, Any]) -> list[str]:
    """Process settings Bambu Studio itself recorded as differing from
    the system preset (first entry of `different_settings_to_system`)."""
    diff = settings.get("different_settings_to_system")
    if isinstance(diff, list) and diff and isinstance(diff[0], str):
        return [k for k in diff[0].split(";") if k]
    return []


# ---------------------------------------------------------------------------
# Plates
# ---------------------------------------------------------------------------

MODEL_SETTINGS_PART = "Metadata/model_settings.config"


def plate_object_ids(path: str | Path) -> dict[int, list[str]]:
    """Which objects sit on which plate: {plate number: [object ids]},
    from `Metadata/model_settings.config` (layout confirmed against a
    real project: `<plate><metadata key="plater_id" value="1"/>
    <model_instance><metadata key="object_id" value="2"/>…`). The ids
    are the `objectid`s of the build items in `3D/3dmodel.model`.
    Returns {} for a file with no plate information.
    """
    with _open(path) as zf:
        part = _find_part(zf, MODEL_SETTINGS_PART)
        if part is None:
            return {}
        try:
            root = ET.fromstring(zf.read(part))
        except ET.ParseError:
            return {}
    plates: dict[int, list[str]] = {}
    for plate_el in root.findall("plate"):
        meta = {m.get("key"): m.get("value") for m in plate_el.findall("metadata")}
        number = _as_int(meta.get("plater_id")) or len(plates) + 1
        ids = []
        for instance in plate_el.findall("model_instance"):
            for m in instance.findall("metadata"):
                if m.get("key") == "object_id" and m.get("value"):
                    ids.append(m.get("value"))
        plates[number] = ids
    return plates


# ---------------------------------------------------------------------------
# Slice results
# ---------------------------------------------------------------------------


def read_slice_result(path: str | Path) -> list[dict[str, Any]]:
    """Per-plate slice results, or [] if the file hasn't been sliced."""
    with _open(path) as zf:
        return _read_slice_result(zf)


def _read_slice_result(zf: zipfile.ZipFile) -> list[dict[str, Any]]:
    part = _find_part(zf, SLICE_INFO_PART)
    if part is None:
        return []
    try:
        root = ET.fromstring(zf.read(part))
    except ET.ParseError:
        return []

    plates: list[dict[str, Any]] = []
    for plate_el in root.findall("plate"):
        meta = {m.get("key"): m.get("value") for m in plate_el.findall("metadata")}
        seconds = _as_int(meta.get("prediction"))
        if not seconds or seconds <= 0:
            # Not a slice result: an unsliced project keeps an empty
            # slice_info, and a plate with no positive time prediction
            # was not sliced, whatever else the entry says.
            continue
        index = _as_int(meta.get("index")) or len(plates) + 1
        plate: dict[str, Any] = {
            "plate": index,
            "print_time_s": seconds,
            "print_time": _format_duration(seconds),
            "filament_g": _as_float(meta.get("weight")),
            "support_used": _as_bool(meta.get("support_used")),
            "objects": [o.get("name") for o in plate_el.findall("object")],
            "filaments": [
                {
                    "slot": _as_int(f.get("id")),
                    "type": f.get("type"),
                    "color": f.get("color"),
                    "used_m": _as_float(f.get("used_m")),
                    "used_g": _as_float(f.get("used_g")),
                    "used_for_support": _as_bool(f.get("used_for_support")),
                }
                for f in plate_el.findall("filament")
            ],
            "warnings": sorted({w.get("msg") for w in plate_el.findall("warning") if w.get("msg")}),
            "contains_gcode": _find_part(zf, f"Metadata/plate_{index}.gcode") is not None,
        }
        plate.update(_read_gcode_header(zf, index))
        plates.append(plate)
    return plates


def _read_gcode_header(zf: zipfile.ZipFile, plate_index: int) -> dict[str, Any]:
    part = _find_part(zf, f"Metadata/plate_{plate_index}.gcode")
    if part is None:
        return {}
    with zf.open(part) as f:
        head = f.read(_GCODE_HEADER_BYTES).decode("utf-8", errors="ignore")
    out: dict[str, Any] = {}
    if m := re.search(r"total layer number:\s*(\d+)", head):
        out["layers"] = int(m.group(1))
    if m := re.search(r"max_z_height:\s*([\d.]+)", head):
        out["max_z_mm"] = float(m.group(1))
    if m := re.search(r"model printing time:\s*([^;\n]+)", head):
        out["model_print_time"] = m.group(1).strip()
    return out


def _as_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return str(value).strip().lower() in ("1", "true")


def _format_duration(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    hours, rem = divmod(int(seconds), 3600)
    minutes = rem // 60
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


# ---------------------------------------------------------------------------
# Writing a temporary-profile copy
# ---------------------------------------------------------------------------


def write_project_copy(
    source: str | Path,
    destination: str | Path,
    changes: Mapping[str, Any],
) -> dict[str, Any]:
    """Writes `destination` as a copy of the project `source` with only
    `changes` (setting key -> value) applied to its embedded settings.

    Guarantees:
    - `source` is opened read-only and never modified;
    - `destination` must not already exist (nothing is overwritten);
    - every key must be in the registry and every value must pass
      `bambu_values.validate` — otherwise nothing is written;
    - every other part of the file (geometry, plate layout, thumbnails)
      is copied byte-for-byte.

    Returns {"written": path, "applied": {key: {"from", "to"}},
    "unchanged": [keys already at the requested value]}.
    """
    source = Path(source)
    destination = Path(destination)
    if destination.suffix.lower() != ".3mf":
        raise ProjectFileError("The output file must end in .3mf.")
    if destination.exists():
        raise ProjectFileError(
            f"{destination} already exists. Refusing to overwrite it — pick a new file name."
        )
    if destination.resolve() == source.resolve():
        raise ProjectFileError("The output file must be different from the source file.")

    typed_changes = {key: validate(key, value) for key, value in changes.items()}

    with _open(source) as zf:
        settings_part, settings = _load_settings(zf, source.name)
        if any(_GCODE_PART_RE.match(n) for n in zf.namelist()):
            raise ProjectFileError(
                f"{source.name} is a sliced export (it contains G-code), not an editable "
                "project. In Bambu Studio use File > Save Project As and give me that file."
            )

        applied: dict[str, dict[str, Any]] = {}
        unchanged: list[str] = []
        new_settings = dict(settings)
        for key, value in typed_changes.items():
            existing = settings.get(key)
            if key in settings and values_equal(key, existing, value):
                unchanged.append(key)
                continue
            new_settings[key] = format_like(key, value, existing, bambu_style=True)
            applied[key] = {"from": to_typed(key, existing) if key in settings else None, "to": value}

        _record_as_modified(new_settings, applied.keys())

        destination.parent.mkdir(parents=True, exist_ok=True)
        # Mode "x" is the real guard against overwriting: it fails if the
        # name is taken at the instant of creation, whatever the check
        # at the top of this function saw. It is opened OUTSIDE the
        # cleanup block below on purpose — if the open fails, the file
        # that is there belongs to someone else and must not be removed.
        try:
            out = zipfile.ZipFile(destination, "x", zipfile.ZIP_DEFLATED)
        except FileExistsError:
            raise ProjectFileError(
                f"{destination} already exists. Refusing to overwrite it — pick a new file name."
            ) from None
        try:
            with out:
                for info in zf.infolist():
                    if info.filename == settings_part:
                        payload = json.dumps(new_settings, indent=4, ensure_ascii=False).encode("utf-8")
                    else:
                        payload = zf.read(info)  # this entry, not "the last one with this name"
                    out.writestr(info, payload, compress_type=info.compress_type)
        except BaseException:
            # Never leave a half-written project behind. Only reached
            # for a file this call created.
            try:
                destination.unlink()
            except OSError:
                pass
            raise

    return {"written": str(destination), "applied": applied, "unchanged": sorted(unchanged)}


def _record_as_modified(settings: dict[str, Any], changed_keys) -> None:
    """Adds the changed keys to the process entry of
    `different_settings_to_system`, the list Bambu Studio keeps of
    settings that differ from the system preset, so the copy opens as
    "<preset> (modified)" with our values instead of quietly snapping
    back to the preset's. Only ever adds to the process entry (index 0);
    if the field is absent or has an unexpected shape it is left alone.
    """
    diff = settings.get("different_settings_to_system")
    if not (isinstance(diff, list) and diff and isinstance(diff[0], str)):
        return
    existing = [k for k in diff[0].split(";") if k]
    merged = sorted(set(existing) | set(changed_keys))
    settings["different_settings_to_system"] = [";".join(merged), *diff[1:]]
