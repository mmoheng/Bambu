# Bambu Companion

Code for the project described in the "Bambu Companion" memory bank
(Bambu Lab A1 + AMS 2 Pro, ChatGPT-connected print optimization via a
local Windows bridge and Bambu Studio). See `docs/ARCHITECTURE.md` for
how this code maps to that design, and `bambu_companion/bridge/README.md`
for the honest per-file status of everything that still needs your
printer/Bambu Studio/OpenAI setup to finish.

## What's implemented and tested right now

- **Model Analyzer** (`bambu_companion/model_analyzer/`): loads STL/3MF,
  computes bounding box, A1 bed-fit, bed-contact area, overhangs,
  bridges, sampled thin-wall thickness, small (multi-body) features,
  fit-sensitive open holes, candidate resting orientations, and
  manifold/watertight warnings. Pure Python + numpy/scipy/networkx, no
  printer or Bambu Studio needed.
- **Print Optimizer** (`bambu_companion/optimizer/`): turns an analysis +
  chosen goal into a Current → Recommended settings diff, every change
  tied to a specific reason referencing the model's actual geometry
  and/or the goal — never a fixed recipe. Anything it can't confidently
  turn into a setting value (bridge cooling, seam placement, exact
  compensation numbers) is reported as an uncertainty instead of guessed.
- **Temporary Profile logic** (`bambu_companion/profiles/temp_profile.py`):
  pure "apply only the approved changes to a copy of the settings" step —
  the code-level guarantee behind "never overwrite normal presets."
- **Job History** (`bambu_companion/bridge/job_history.py`): a real,
  working JSON-backed store for the memory bank's Job History section.

Run the test suite:

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -t .
```

Try it on a real file (no printer needed):

```bash
python -m bambu_companion.cli path/to/model.stl \
    --goal dimensional_accuracy --material PETG \
    --current wall_loops=2 --current sparse_infill_density=15
```

## Desktop GUI (recommended over the CLI)

`bambu_companion/gui.py` is a Tkinter window that does the same thing as
the CLI above, without a terminal: pick your model file with a file
picker, edit the "current settings" fields to match your real Bambu
Studio profile, click **Analyze**, check off which recommended changes
to accept, and click **Export recommendations...** to save a plain-text
summary and a JSON settings file you can copy into Bambu Studio's
Process panel. It never writes into Bambu Studio automatically — see
`bridge/studio_runner.py`'s status below for why.

Run it:

```bash
python -m bambu_companion.gui
```

or, once Python is set up on your PC, just double-click `launch_gui.pyw`
at the repo root — no terminal needed for everyday use.

Tkinter ships with a normal python.org Windows install, so this
shouldn't need any extra `pip install`. The logic behind the GUI
(`bambu_companion/gui_logic.py`, `bambu_companion/gui_config.py`) is unit
tested in `tests/test_gui_logic.py` / `tests/test_gui_config.py` and has
no tkinter dependency. **The GUI window itself (`gui.py`) is only
syntax-checked in this dev sandbox, never run** — there's no display
here, so tkinter isn't even installed. It has been run for real on
Windows once already (that's how a layout bug in the Reason column got
found and fixed), but every further change to `gui.py` still needs a
real run to confirm; if something looks wrong, that's almost certainly a
`gui.py` layout issue rather than a wrong recommendation, since the
recommendation logic is the same tested code the CLI uses.

A few quality-of-life details worth knowing:

- **It remembers your last session.** Model file, goal, material,
  nozzle, and the current-settings grid are saved after each successful
  Analyze, and restored the next time you open the GUI, along with a
  short "Recent" dropdown of the last 5 files you analyzed. This is
  stored outside the git repo (under `%APPDATA%\BambuCompanion\` on
  Windows) specifically so your personal file paths never end up in your
  GitHub history or fight with OneDrive syncing this folder.
- **It has an app icon** (`bambu_companion/assets/app_icon.ico`) for the
  window/taskbar; if it fails to load for any reason the GUI still opens
  fine with Tk's default icon.
- **Exports can be a complete, directly-importable Bambu Studio
  profile**, not just the dozen keys the optimizer reasons about. In
  Bambu Studio's **Process** panel, right-click your current preset ->
  **Export** to save your real settings as a JSON file, then click
  **Load full Bambu Studio profile...** in the GUI and pick that file.
  The settings grid refreshes from your real profile, and from then on
  **Export recommendations...** writes a third file —
  `<model>_bambu_studio_full_profile.json` — containing every key from
  your real profile with only the approved changes overlaid, ready to
  **Import** straight back into Bambu Studio's Process panel instead of
  retyping values by hand. This is entirely optional; without it, export
  still works exactly as before (a plain-text summary and a small JSON
  with just the tracked settings). The loaded profile's path is
  remembered the same way as your last model file, so it reloads
  automatically next time you open the GUI.

## What still needs your environment

Everything above was built and tested without a printer, without Bambu
Studio installed, and without package-index access (the dev sandbox this
was built in has none of those) — so the pieces that inherently depend on
them are written as completely as possible but not yet exercised for
real:

- **`bridge/studio_runner.py`** — real Bambu Studio CLI command-builder
  (unit tested) + subprocess wrapper (untested end-to-end). Needs Bambu
  Studio installed on your PC.
- **`bridge/printer_status.py`** — real MQTT client for the printer's
  local Developer Mode API. Topic names are a documented best guess (see
  its docstring) that need confirming against your actual A1. Needs
  `pip install paho-mqtt` and a printer with Developer Mode on.
- **`bridge/server.py`** — the HTTP layer ChatGPT would call through a
  tunnel/relay (see the memory bank's "ChatGPT Connectivity" section).
  Skeleton only; needs `pip install -r requirements-bridge.txt` plus the
  tunnel/relay component and an OpenAI developer/connector setup, neither
  of which exists in this repo yet.

See `bambu_companion/bridge/README.md` for the exact gap to close on each
file.

## Project layout

```
bambu_companion/
  schemas.py           # shared dataclasses used everywhere
  model_analyzer/       # STL/3MF loading + geometry analysis (real, tested)
  optimizer/            # goal + geometry -> settings diff (real, tested)
  profiles/              # setting-key registry + temp-profile logic (real, tested)
  bridge/                 # printer status, Bambu Studio runner, job history,
                           # ChatGPT-facing server (mixed — see status table above)
  cli.py                  # local testing entry point, no printer needed
  gui_logic.py             # pure logic behind the GUI (real, tested, no tkinter)
  gui_config.py            # persisted last-used settings/recent files (real, tested, no tkinter)
  gui.py                   # Tkinter desktop GUI (run once for real on Windows; see above)
  assets/app_icon.ico      # window/taskbar icon
launch_gui.pyw            # double-click launcher for gui.py on Windows
tests/                    # unittest suite (stdlib unittest, no pytest needed)
docs/
  ARCHITECTURE.md         # maps this code back to the approved design
```

## Next steps (suggested order)

1. Install Bambu Studio on the target Windows PC, run `--help`, and
   reconcile `studio_runner._KNOWN_FLAGS` against your version.
2. Do one real slice through `studio_runner.run_slice_job` on a simple
   model and check `verify_settings_applied` actually finds your settings
   in the exported 3MF — tighten the heuristic once you've seen a real
   file.
3. Turn on Developer Mode on the A1, capture a real MQTT report payload,
   and reconcile `printer_status.Topics` / `parse_report_payload` against
   it.
4. Install `requirements-bridge.txt`, bring up `bridge/server.py`
   locally, and confirm the `/model/analyze` and `/optimize` endpoints
   work end-to-end against a real file.
5. Build the tunnel/relay component from the memory bank's "ChatGPT
   Connectivity" section, then set up the ChatGPT App/connector itself.
