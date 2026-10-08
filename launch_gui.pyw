"""Double-click launcher for the Bambu Companion GUI on Windows.

The .pyw extension makes Windows run this with pythonw.exe (no console
window pops up behind the GUI). Just double-click this file in File
Explorer once Python is installed and associated with .pyw files
(that's the default on a normal python.org Windows install).

If double-clicking does nothing or shows an error too fast to read, run
it from a command prompt instead so you can see the error:

    py launch_gui.pyw

This file intentionally has no logic of its own — see bambu_companion/gui.py
for the actual application, and bambu_companion/gui_logic.py for the
tested logic behind it.
"""

import sys
from pathlib import Path

# Make sure the repo root (this file's directory) is importable as a
# package root, the same way "python -m bambu_companion.gui" would see it,
# even when launched by double-click from an arbitrary working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bambu_companion.gui import main  # noqa: E402

if __name__ == "__main__":
    main()
