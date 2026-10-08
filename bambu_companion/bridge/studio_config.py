"""Local, per-machine settings for the Bambu Studio Runner: where
Bambu Studio is installed, which presets to use when a model comes with
none of its own, and which slicing strategy last worked here.

Same pattern and same reasons as `printer_config.py` / `gui_config.py`:
stored under `%APPDATA%\\BambuCompanion\\`, outside the git repo, so
machine-specific paths never end up in GitHub or fight with OneDrive.
Deliberately a separate file from `bridge_config.json` (the API key):
`api_auth.save_api_key` rewrites that file whole.

Nothing secret lives here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..app_dirs import user_data_root

CONFIG_DIR_NAME = "BambuCompanion"
CONFIG_FILE_NAME = "studio_config.json"

# Only used when a job doesn't say which presets to use. The machine and
# filament names were read from a real Bambu Studio 02.08.04.57 project
# file (printer_settings_id / filament_settings_id). The process name is
# Bambu's standard 0.20 mm A1 preset and follows the same naming as the
# "0.28mm Extra Draft @BBL A1" seen in that file, but was not itself in
# it — `list_presets` shows what this machine really has.
_DEFAULTS: dict[str, Any] = {
    "studio_executable": "",
    "default_machine": "Bambu Lab A1 0.4 nozzle",
    "default_process": "0.20mm Standard @BBL A1",
    "default_filament": "Bambu PLA Basic @BBL A1",
    "default_bed_type": "Textured PEI Plate",
    "working_strategy": "",
    "output_dir": "",
}


def config_dir() -> Path:
    return user_data_root() / CONFIG_DIR_NAME


def default_config_path() -> Path:
    return config_dir() / CONFIG_FILE_NAME


def load_studio_config(path: Path | None = None) -> dict[str, Any]:
    """Saved config merged over defaults. Never raises: a missing or
    corrupt file just means defaults."""
    path = path or default_config_path()
    merged = dict(_DEFAULTS)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))  # Notepad adds a BOM
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return merged
    if isinstance(data, dict):
        merged.update({k: v for k, v in data.items() if k in _DEFAULTS and isinstance(v, str)})
    return merged


def update_studio_config(path: Path | None = None, **changes: str) -> dict[str, Any]:
    """Merges `changes` into the saved config and returns the result.
    Unknown keys are rejected rather than silently stored. A failed
    write is swallowed (the returned dict still reflects the request)."""
    unknown = sorted(set(changes) - set(_DEFAULTS))
    if unknown:
        raise KeyError(f"Unknown studio config key(s): {unknown}")
    path = path or default_config_path()
    merged = load_studio_config(path)
    merged.update({k: str(v) for k, v in changes.items()})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    except OSError:
        pass
    return merged
