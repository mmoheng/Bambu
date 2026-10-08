"""Launcher for the Bambu Companion Claude connector (local MCP server).

Claude Desktop starts this file; you don't run it yourself. It exists so
the connector's config can point at one fixed file, wherever Claude
Desktop happens to start it from:

    "bambu-companion": {
      "command": "python",
      "args": ["C:\\\\path\\\\to\\\\this\\\\repo\\\\launch_mcp.py"]
    }

See README.md, "Claude connector", for the full setup. No logic lives
here — see bambu_companion/mcp_server.py.
"""

import sys
from pathlib import Path

# Make the repo root importable the same way "python -m
# bambu_companion.mcp_server" would see it, from any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bambu_companion.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
