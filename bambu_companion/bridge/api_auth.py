"""Local-only API key for the ChatGPT-facing bridge server (server.py).

Once server.py is reachable through a Cloudflare Tunnel, anyone who
guesses or finds the tunnel's public URL can send it HTTP requests — the
tunnel just gets traffic to your PC, it doesn't decide who's allowed to
use it. This module is what does: every endpoint except a plain health
check requires a matching `X-API-Key` header.

Follows the same local-storage pattern as gui_config.py / printer_config.py
and is deliberately kept OUTSIDE the git repo, under the platform's
per-user app-data directory (APPDATA on Windows) — a secret like this
should never end up committed to GitHub or fought over by OneDrive
syncing the repo folder.

The key itself is generated automatically the first time the server
starts (nothing to invent or type) and then reused on every future
start, so it stays stable for as long as your ChatGPT Custom GPT's
Action config references it. If you ever delete this file, a fresh key
is generated on the next start and you'll need to update the GPT's
Action config to match.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Optional

CONFIG_DIR_NAME = "BambuCompanion"
CONFIG_FILE_NAME = "bridge_config.json"


def default_config_path() -> Path:
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home()
    return root / CONFIG_DIR_NAME / CONFIG_FILE_NAME


def load_api_key(path: Optional[Path] = None) -> Optional[str]:
    """Returns the saved API key, or None if none has been generated yet
    or the file is missing/corrupt. Never raises."""
    path = path or default_config_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    key = data.get("api_key")
    return key if isinstance(key, str) and key else None


def save_api_key(api_key: str, path: Optional[Path] = None) -> None:
    """Writes the API key, creating the app-data folder if needed.
    Swallows write failures rather than raising."""
    path = path or default_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"api_key": api_key}, indent=2), encoding="utf-8")
    except OSError:
        pass


def get_or_create_api_key(path: Optional[Path] = None) -> str:
    """What server.py actually calls on startup: returns the existing
    saved key, or generates a new cryptographically random one (32 bytes,
    URL-safe, via `secrets.token_urlsafe` — not a guessable value) and
    saves it if this is the first run."""
    existing = load_api_key(path)
    if existing:
        return existing
    new_key = secrets.token_urlsafe(32)
    save_api_key(new_key, path)
    return new_key


def matches(candidate: str, expected: str) -> bool:
    """Constant-time comparison so checking the header can't leak the
    real key one byte at a time via response-timing differences. An
    empty `expected` never matches anything (including an empty
    `candidate`) — a misconfigured/blank server key should fail closed,
    not accept every request."""
    if not expected:
        return False
    return secrets.compare_digest(candidate or "", expected)
