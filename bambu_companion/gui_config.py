"""Small persisted-preferences store for the desktop GUI.

Kept separate from gui.py (like gui_logic.py) so it's plain, tkinter-free
logic with real tests in tests/test_gui_config.py. All it does is
remember what you last had in the window — last model file, goal,
material, nozzle, the current-settings grid, and a short recent-files
list — so re-opening the GUI doesn't start from a blank form every time.

Deliberately NOT stored inside the git repo: it lives under the
platform's per-user app-data directory (APPDATA on Windows), so your
personal last-used file paths and settings never end up as noise in
your GitHub history or fight with OneDrive syncing the repo folder.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CONFIG_DIR_NAME = "BambuCompanion"
CONFIG_FILE_NAME = "gui_config.json"
MAX_RECENT_FILES = 5

_DEFAULTS: dict[str, Any] = {
    "last_model_path": "",
    "goal_label": "",
    "material": "",
    "nozzle_diameter_mm": "",
    "current_settings": {},
    "recent_files": [],
    "full_profile_path": "",
}


def default_config_path() -> Path:
    """Where the config lives on this machine. APPDATA on Windows (the
    normal place); falls back to the home directory anywhere else, since
    this GUI is Windows-first but the logic should not hard-crash off
    Windows (e.g. running tests in this project's Linux dev sandbox)."""
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home()
    return root / CONFIG_DIR_NAME / CONFIG_FILE_NAME


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Returns the saved config, or the defaults if there's nothing saved
    yet or the file is missing/corrupt. Never raises — a bad or absent
    config file should degrade to "start fresh," not crash the GUI on
    launch."""
    path = path or default_config_path()
    if not path.exists():
        return dict(_DEFAULTS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(_DEFAULTS)
    if not isinstance(data, dict):
        return dict(_DEFAULTS)

    merged = dict(_DEFAULTS)
    merged.update({k: v for k, v in data.items() if k in _DEFAULTS})
    if not isinstance(merged.get("current_settings"), dict):
        merged["current_settings"] = {}
    if not isinstance(merged.get("recent_files"), list):
        merged["recent_files"] = []
    return merged


def save_config(data: dict[str, Any], path: Path | None = None) -> None:
    """Writes the config, creating the app-data folder if needed. Swallows
    write failures (e.g. a locked/permission-denied file) rather than
    crashing the GUI over a non-essential save — worst case, next launch
    just doesn't remember this session."""
    path = path or default_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: data.get(k, _DEFAULTS[k]) for k in _DEFAULTS}
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


def add_recent_file(recent_files: list[str], model_path: str) -> list[str]:
    """Moves model_path to the front of the recent-files list, de-duped,
    capped at MAX_RECENT_FILES. Pure function so it's trivially testable
    and gui.py just calls it before saving."""
    deduped = [p for p in recent_files if p != model_path]
    return [model_path, *deduped][:MAX_RECENT_FILES]
