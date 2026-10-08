"""Finds Bambu Studio's preset files on disk and turns a preset name
into a *full* config — every key present, nothing left to inheritance.

Why: Bambu Studio's command line wants full configs.
`--load-settings "machine.json;process.json"` and `--load-filaments`
are documented as taking "full config files, not the BBL profile
fragments" (Bambu Studio wiki, Command Line Usage). The presets on disk
are fragments: `0.20mm Standard @BBL A1` holds a handful of keys plus
`"inherits": "fdm_process_single_0.20"`, which inherits from
`fdm_process_single_common`, and so on. This module walks that chain.

Where it looks (each is a "root" containing `machine/`, `process/` and
`filament/` folders):

- `<install>/resources/profiles/BBL` — shipped with Bambu Studio;
- `%APPDATA%/BambuStudio/system/BBL` — the copy Bambu Studio keeps
  updated at runtime;
- `%APPDATA%/BambuStudio/user/<id>` — your own saved presets.

If a root has a ready-made `<type>_full/<name>.json` it is used as-is.

HONEST STATUS: the inheritance logic is unit-tested against synthetic
preset trees shaped like Bambu's. It has NOT been run against a real
install from this development environment (no Bambu Studio here) — the
folder layout above is from Bambu Studio's public repository and normal
Windows installs, and `describe()` exists precisely so the first real
run can report what it actually found instead of assuming.

Read-only: nothing in this module writes to any preset folder.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..app_dirs import user_data_root

PRESET_TYPES = ("machine", "process", "filament")
_MAX_INHERIT_DEPTH = 16
# Keys that describe a preset *file's* place in the tree rather than a
# setting; they must not leak from a parent into the resolved result.
_TREE_KEYS = ("inherits", "include", "instantiation", "base_id")


class PresetError(LookupError):
    """A preset couldn't be found or resolved. The message is written to
    be shown to a person as-is."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PresetError(f"Could not read preset file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PresetError(f"Preset file {path} is not a JSON object.")
    return data


def appdata_studio_dir(appdata: str | os.PathLike | None = None) -> Path | None:
    """`%APPDATA%/BambuStudio` (Bambu Studio's own data folder on
    Windows), or None on a system that has no such folder."""
    if appdata is not None:
        return Path(appdata) / "BambuStudio"
    if os.environ.get("APPDATA") or os.name == "nt":
        return user_data_root() / "BambuStudio"
    return None


def discover_roots(
    studio_executable: str | os.PathLike | None = None,
    appdata: str | os.PathLike | None = None,
) -> list[Path]:
    """Existing preset roots, most specific first (user presets win over
    the runtime system copy, which wins over what the installer shipped)."""
    roots: list[Path] = []
    studio_dir = appdata_studio_dir(appdata)
    if studio_dir is not None:
        user_dir = studio_dir / "user"
        if user_dir.is_dir():
            roots.extend(sorted(p for p in user_dir.iterdir() if p.is_dir()))
        roots.append(studio_dir / "system" / "BBL")
    if studio_executable:
        roots.append(Path(studio_executable).parent / "resources" / "profiles" / "BBL")
    return [r for r in roots if r.is_dir() and any((r / t).is_dir() for t in PRESET_TYPES)]


@dataclass
class PresetLibrary:
    """Looks presets up by name across one or more roots."""

    roots: list[Path]
    _index: dict[str, dict[str, Path]] = field(default_factory=dict, repr=False)

    # -- lookup ---------------------------------------------------------

    def _type_dirs(self, preset_type: str) -> Iterable[Path]:
        for root in self.roots:
            for sub in (preset_type, f"{preset_type}/base"):
                d = root / sub
                if d.is_dir():
                    yield d

    def _build_index(self, preset_type: str) -> dict[str, Path]:
        """name -> file, first root wins. Names come from the file name
        (Bambu's convention) and, where it differs, the file's own
        `"name"` field."""
        if preset_type not in self._index:
            index: dict[str, Path] = {}
            for d in self._type_dirs(preset_type):
                for path in sorted(d.glob("*.json")):
                    index.setdefault(path.stem, path)
            self._index[preset_type] = index
        return self._index[preset_type]

    def find(self, preset_type: str, name: str) -> Path | None:
        _check_type(preset_type)
        _check_name(name)
        index = self._build_index(preset_type)
        if name in index:
            return index[name]
        for path in index.values():  # slow path: internal "name" field
            try:
                if _read_json(path).get("name") == name:
                    index[name] = path
                    return path
            except PresetError:
                continue
        return None

    def list_names(self, preset_type: str, contains: str | None = None) -> list[str]:
        """Selectable preset names (the ones Bambu Studio shows in its
        drop-downs), optionally filtered by a case-insensitive substring.
        Internal base presets (`fdm_process_common`, `... @base`) are
        left out."""
        _check_type(preset_type)
        needle = contains.lower() if contains else None
        names = []
        for name, path in self._build_index(preset_type).items():
            if needle and needle not in name.lower():
                continue
            if name.startswith("fdm_") or name.endswith("@base"):
                continue
            try:
                if str(_read_json(path).get("instantiation", "true")).lower() == "false":
                    continue
            except PresetError:
                continue
            names.append(name)
        return sorted(names)

    # -- resolution -----------------------------------------------------

    def load_raw(self, preset_type: str, name: str) -> dict[str, Any]:
        path = self.find(preset_type, name)
        if path is None:
            raise PresetError(
                f"No {preset_type} preset named '{name}' was found under: "
                + (", ".join(str(r) for r in self.roots) or "(no preset folders found)")
            )
        return _read_json(path)

    def load_full(self, preset_type: str, name: str) -> dict[str, Any]:
        """The complete config for a preset: a shipped `<type>_full`
        file if one exists, otherwise the preset with its whole
        `inherits` / `include` chain merged in (parents first, so the
        nearest definition of each key wins)."""
        _check_type(preset_type)
        _check_name(name)
        for root in self.roots:
            full_dir = root / f"{preset_type}_full"
            ready = full_dir / f"{name}.json"
            # Belt and braces on top of _check_name: the file must be
            # directly inside the *_full folder.
            if ready.is_file() and ready.resolve().parent == full_dir.resolve():
                return _read_json(ready)

        merged = self._resolve(preset_type, name, depth=0, seen=())
        leaf = self.load_raw(preset_type, name)
        merged["name"] = leaf.get("name", name)
        merged.setdefault("type", preset_type)
        merged.setdefault("from", "system")
        merged["inherits"] = ""
        for key in ("include", "instantiation"):
            merged.pop(key, None)
        return merged

    def _resolve(self, preset_type: str, name: str, *, depth: int, seen: tuple[str, ...]) -> dict[str, Any]:
        if depth > _MAX_INHERIT_DEPTH or name in seen:
            raise PresetError(
                f"Preset inheritance loop or runaway chain while resolving {preset_type} "
                f"preset: {' -> '.join((*seen, name))}"
            )
        raw = self.load_raw(preset_type, name)
        merged: dict[str, Any] = {}
        parents: list[str] = []
        if raw.get("inherits"):
            parents.append(str(raw["inherits"]))
        include = raw.get("include")
        if isinstance(include, list):
            parents.extend(str(i) for i in include if i)
        for parent in parents:
            merged.update(self._resolve(preset_type, parent, depth=depth + 1, seen=(*seen, name)))
        merged.update({k: v for k, v in raw.items() if k not in _TREE_KEYS})
        return merged

    def describe(self) -> dict[str, Any]:
        """What was actually found on this machine — for diagnostics."""
        out: dict[str, Any] = {"roots": [str(r) for r in self.roots], "counts": {}, "full_dirs": []}
        for preset_type in PRESET_TYPES:
            out["counts"][preset_type] = len(self._build_index(preset_type))
            for root in self.roots:
                if (root / f"{preset_type}_full").is_dir():
                    out["full_dirs"].append(str(root / f"{preset_type}_full"))
        return out


_FORBIDDEN_IN_NAME = ("/", "\\", ":", "\x00")


def _check_name(name: Any) -> str:
    """A preset name is looked up, never opened as a path. Names reach
    this module from outside (a tool argument, the `print_settings_id`
    inside a project file), and `root / f"{name}.json"` with a name
    like `C:\\Users\\me\\x` or `..\\..\\x` would read any JSON on the
    disk — including the printer credentials file — and hand it to the
    slicer as a "preset". So: no separators, no drive colon, no dot
    names."""
    if not isinstance(name, str) or not name.strip():
        raise PresetError("A preset name is required.")
    if any(ch in name for ch in _FORBIDDEN_IN_NAME) or name.strip(". ") == "":
        raise PresetError(
            f"'{name}' is not a preset name. Give the name exactly as Bambu Studio shows it "
            "(see list_presets), not a file path."
        )
    return name


def _check_type(preset_type: str) -> None:
    if preset_type not in PRESET_TYPES:
        raise PresetError(f"Unknown preset type '{preset_type}' (expected one of {PRESET_TYPES}).")
