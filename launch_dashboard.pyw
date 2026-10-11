"""Double-click to open the Bambu Companion dashboard in your browser.

If the Claude connector is running, its dashboard is already serving at
http://127.0.0.1:8421/ and this just opens it; otherwise this starts one
(and keeps running in the background until you log off or end it in
Task Manager — it only listens on this PC).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bambu_companion.dashboard import main  # noqa: E402

raise SystemExit(main())
