# Bambu Companion

Code for the project described in the "Bambu Companion" memory bank
(Bambu Lab A1 + AMS 2 Pro, ChatGPT-connected print optimization via a
local Windows bridge and Bambu Studio). See `docs/ARCHITECTURE.md` for
how this code maps to that design, and `bambu_companion/bridge/README.md`
for the honest per-file status of everything that still needs your
printer/Bambu Studio/OpenAI setup to finish.

## Status at a glance (updated 2026-10-07)

| Piece | State |
|---|---|
| Model analyzer, optimizer, temp-profile logic, job history | Working, tested |
| Reading settings / slice results from Bambu Studio project files | Working. Key names and formats were read from real 02.08.04.57 files; the tests use synthetic files in the same shape |
| Writing a project copy with changed settings | Tested by reading the copy back. **A written file has not yet been opened in Bambu Studio** |
| Desktop GUI | Ran on Windows before 2026-10-07. Its full-profile export changed since (values now written in Bambu's own format) and `gui.py` has not been re-run |
| **Claude connector** (local MCP server) | Tested by driving it as a real process over stdin/stdout with a test client. **Not yet run under Claude Desktop** — register it once (below) |
| Printer + AMS status | Works idle; mid-print field names still unconfirmed |
| **Headless slicing** | Rewritten to call Bambu Studio the documented way. **Not yet confirmed on a real install** — run the slice check (below) |
| HTTP bridge for ChatGPT | Written; needs a live run. Its "approve" step still stops before slicing, on purpose, until the slice check passes |
| Tunnel + ChatGPT connector | Not started |

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
- **Bambu value handling** (`bambu_companion/profiles/bambu_values.py`,
  `bambu_settings.py`): Bambu Studio stores `"2"`, `"15%"`, `"1"`,
  `["200"]`; the optimizer thinks in `2`, `15`, `True`, `200`. This
  converts both ways, and the settings registry doubles as the
  allowlist of what this project may ever change (47 ordinary process
  settings — no G-code, no machine limits, no temperatures). Every key
  was read out of a real Bambu Studio 02.08.04.57 project file. Values
  are checked for type and range; four option settings whose full list
  of choices isn't pinned (infill pattern, support style, the two
  support patterns) are only checked to be shaped like an option name.
- **Project files** (`bambu_companion/profiles/project_3mf.py`): reads
  the settings and slice result (time, filament, support used) stored
  inside a Bambu Studio `.3mf`, and writes a *copy* of a project with
  only approved settings changed — the Temporary Profile Rule for
  project files. The source is never modified, and the copy is created
  exclusively, so it can't replace a file that is already there. (One
  bookkeeping field besides the settings is updated: Bambu Studio's own
  list of "settings that differ from the preset", so the copy opens as
  the preset *modified*.)
- **Preset resolver** (`bambu_companion/profiles/preset_library.py`):
  turns a preset name into the full config the Bambu Studio CLI needs
  by walking its `inherits` chain. Tested on synthetic preset trees; not
  yet run against a real install.
- **3MF loading now handles real Bambu Studio projects.** They keep
  their triangles in `3D/Objects/object_N.model`; the loader used to
  skip those and report "no usable mesh geometry" for every real
  project file.

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

## Claude connector

`bambu_companion/mcp_server.py` lets Claude (the desktop app) use this
project directly on your PC: analyze a model, read the settings in a
project file, recommend changes, write a new project file with the
changes you approved, slice, and read printer/AMS status. Claude
Desktop starts it and talks to it over stdin/stdout — no network port,
no tunnel, no API key, and nothing extra to `pip install` (printer
status alone needs `paho-mqtt`, as before).

What it is built not to do: start a print or send G-code (there is no
code that publishes to the printer at all); change a saved preset
(preset folders are only read); replace an existing file (outputs are
created exclusively — if the name is taken the write fails or the next
free name is used); or handle the printer's address, serial or access
code (no tool takes them, and results and error messages are built
without them). Preset names given to a tool are looked up by name and
can't be used as file paths.

Known limits: if Claude Desktop is closed while a slice is running, the
Bambu Studio process is left to finish on its own; multi-plate projects
are analysed one plate at a time (plate 1 unless you say otherwise).

Set it up once:

1. In Claude Desktop open **Settings → Developer → Edit Config**. That
   opens `%APPDATA%\Claude\claude_desktop_config.json`.
2. Add this (use the real path to this repo, with doubled backslashes;
   if the file already has an `mcpServers` block, add the
   `bambu-companion` entry inside it):

   ```json
   {
     "mcpServers": {
       "bambu-companion": {
         "command": "python",
         "args": ["C:\\path\\to\\Bambu\\launch_mcp.py"]
       }
     }
   }
   ```

   If `python` isn't on your PATH, use `"command": "py"`.
3. Quit Claude Desktop completely and start it again. `bambu-companion`
   should appear under connectors with 13 tools.

If it doesn't show up, the connector's own output is in
`%APPDATA%\Claude\logs\mcp-server-bambu-companion.log`. You can also
check it starts by running `python launch_mcp.py` in a terminal: it
should sit silently waiting (Ctrl+C to stop).

For printer status, save the printer's connection details locally once
— typed at a prompt on your PC, never pasted into a chat:

```bash
python -m bambu_companion.bridge.printer_setup
```

## Slice check — do this before trusting headless slicing

Headless slicing failed on 2026-09-20 with "Nothing to be sliced". The
runner has since been rewritten to pass the printer + process configs
the way Bambu's documentation specifies (see
`bridge/studio_runner.py`), but that has **not** been confirmed on a
real install. This finds out:

```bash
python -m bambu_companion.bridge.slice_check
python -m bambu_companion.bridge.slice_check --exe "G:\Bambu Studio\bambu-studio.exe"
```

It slices a generated 20 mm cube with each documented invocation,
prints exactly what Bambu Studio answered for each, saves a report to
`%APPDATA%\BambuCompanion\slice_check_report.txt`, and remembers the
first invocation that works. It can also be run through the connector
(the `slice_check` tool). Nothing is sent to the printer.

## What still needs your environment

Everything above was built and tested without a printer, without Bambu
Studio installed, and without package-index access (the dev sandbox this
was built in has none of those) — so the pieces that inherently depend on
them are written as completely as possible but not yet exercised for
real:

- **`bridge/studio_runner.py`** — builds the documented command line
  (unit tested), runs it, and verifies the result by reading the
  exported file (tested against a stand-in for Bambu Studio). Not yet
  confirmed against the real thing — that's what the slice check is for.
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
  app_dirs.py          # where per-user config lives (%APPDATA%)
  model_analyzer/       # STL/3MF loading + geometry analysis (real, tested)
  optimizer/            # goal + geometry -> settings diff (real, tested)
  profiles/
    bambu_settings.py    # registry/allowlist of changeable settings
    bambu_values.py      # Bambu string values <-> plain values
    project_3mf.py       # read/rewrite settings inside a project .3mf
    preset_library.py    # preset name -> full config (inheritance)
    temp_profile.py      # "apply only approved changes" (pure)
  bridge/
    studio_runner.py     # Bambu Studio CLI: build, run, verify
    slice_check.py       # which CLI invocation works on this PC?
    studio_config.py     # local: Bambu Studio path, default presets
    printer_status.py    # read-only MQTT status
    printer_config.py    # local: printer address/serial/access code
    printer_setup.py     # one-time local prompt to save the above
    job_history.py       # JSON job log
    server.py, api_auth.py   # HTTP bridge for ChatGPT
  service.py             # every operation as plain Python (no framework)
  mcp_server.py          # Claude connector: MCP over stdio, stdlib only
  cli.py                  # local testing entry point, no printer needed
  gui_logic.py, gui_config.py, gui.py   # desktop GUI
  assets/app_icon.ico      # window/taskbar icon
launch_gui.pyw            # double-click launcher for gui.py on Windows
launch_mcp.py             # what Claude Desktop starts for the connector
tests/                    # unittest suite (stdlib unittest, no pytest needed)
docs/
  ARCHITECTURE.md         # maps this code back to the approved design
```

## Next steps (suggested order)

1. Register the Claude connector (above) and run the **slice check**.
   If an invocation works it is remembered and `slice_model` is usable;
   if none does, the report says exactly what Bambu Studio answered for
   each — that is the evidence to work from.
2. Do one real slice of a real project through the connector and
   compare its time/filament figures with slicing the same file in the
   Bambu Studio window.
3. Open a project file written by `apply_settings` in Bambu Studio and
   confirm the changed settings show up as "modified" on the preset —
   this path has been tested by reading the files back, not yet by
   opening one in the GUI.
4. Capture printer status during an active print (the connector's
   `printer_status` lists the field names it received) and reconcile
   the unconfirmed mid-print fields in `printer_status.py`.
5. Once slicing is confirmed: wire `service.slice_model` into the HTTP
   bridge's approve endpoint, install `requirements-bridge.txt`, and
   bring up `bridge/server.py`.
6. Build the tunnel/relay component from the memory bank's "ChatGPT
   Connectivity" section, then set up the ChatGPT App/connector itself.

## Changes to recommendation behaviour (2026-10-07)

These change what the optimizer recommends, so they're listed plainly:

- **Wall loops are never lowered for a thin spot.** The old rule cut
  the whole model's wall count to whatever "fit" its thinnest sampled
  point; on real models that point is a chamfer tip (0.01 mm on a set of
  countersunk discs), which turned a strength-goal part into a
  single-wall part. Bambu Studio already prints fewer loops where they
  don't fit. The rule now only ever *raises* the count, when a real thin
  wall would otherwise have a sparse core.
- **Support is decided by the largest overhanging region, not the total
  area.** Forty 5 mm² drip undersides no longer trigger supports; one
  7,000 mm² pocket ceiling does. Thresholds were calibrated on five real
  deck-box models and are heuristics — the reason text always gives the
  numbers.
- **Support settings are material-aware** (looser top gap for PETG, more
  interface layers under a flat ceiling) and marked as starting points.
- **Nothing is made less safe automatically:** a support threshold that
  already catches the overhang isn't lowered, a wider brim isn't shrunk,
  Bambu's Auto brim is left alone, and supports that look unnecessary
  are flagged for you rather than switched off.
- **Values that already match are no longer reported as changes.**
  A loaded Bambu Studio profile stores `"3"`; the optimizer used to see
  that as different from `3` and list every setting.
