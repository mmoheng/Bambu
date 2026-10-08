"""Local, per-machine printer connection config — host, serial, and LAN
access code for `printer_status.PrinterStatusClient`.

Deliberately NOT stored inside the git repo, for the same reason as
`gui_config.py`: this holds your printer's LAN access code, which should
never end up committed to GitHub (public or private) or fought over by
OneDrive syncing this project folder. It lives under the platform's
per-user app-data directory instead (APPDATA on Windows).

This is plain local storage, not encryption — anyone with access to your
Windows user account can read this file. That matches the actual
exposure already implied by Developer/LAN Mode itself (the access code
only protects LAN access to the printer), so it isn't a new risk, but
don't treat this file as a secrets vault beyond that.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CONFIG_DIR_NAME = "BambuCompanion"
CONFIG_FILE_NAME = "printer_config.json"

_DEFAULTS: dict[str, Any] = {
    "host": "",
    "serial": "",
    "access_code": "",
}


def default_config_path() -> Path:
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home()
    return root / CONFIG_DIR_NAME / CONFIG_FILE_NAME


def load_printer_config(path: Path | None = None) -> dict[str, Any]:
    """Returns the saved printer config, or empty defaults if there's
    nothing saved yet or the file is missing/corrupt. Never raises."""
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
    return merged


def save_printer_config(host: str, serial: str, access_code: str, path: Path | None = None) -> None:
    """Writes host/serial/access_code, creating the app-data folder if
    needed. Swallows write failures rather than raising — a failed save
    here shouldn't crash whatever's calling it."""
    path = path or default_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"host": host, "serial": serial, "access_code": access_code}
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass
