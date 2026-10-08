"""Where per-user application data lives.

On Windows that is `%APPDATA%` (`C:\\Users\\<you>\\AppData\\Roaming`).
The catch: a program started by another app doesn't always inherit the
full environment — the MCP documentation calls out Claude Desktop
launching servers without `APPDATA` set. If the connector then fell back
to the home folder, it would look for `printer_config.json` in a
different place from where the setup script saved it and report "no
printer configured". So on Windows the standard location is derived from
the home folder when the variable is missing.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


def user_data_root(
    env: Mapping[str, str] | None = None,
    os_name: str | None = None,
    home: Path | None = None,
) -> Path:
    """`%APPDATA%` when set; otherwise the same folder worked out from
    the home directory on Windows, or the home directory elsewhere. The
    arguments exist for tests; real callers pass none."""
    env = os.environ if env is None else env
    base = env.get("APPDATA")
    if base:
        return Path(base)
    home = Path.home() if home is None else home
    if (os.name if os_name is None else os_name) == "nt":
        return home / "AppData" / "Roaming"
    return home
